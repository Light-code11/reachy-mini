// Runs in the page's own JS world: observes Luma API responses and (optionally) zooms the camera.
(() => {
  const SOURCE = "reachy-greeter";
  const post = (msg) => window.postMessage({ source: SOURCE, ...msg }, "*");

  // 1) Observe responses from Luma's API (read-only; the page still gets the original response).
  const origFetch = window.fetch;
  window.fetch = async function (...args) {
    const res = await origFetch.apply(this, args);
    try {
      const url = typeof args[0] === "string" ? args[0] : args[0]?.url || String(args[0]);
      if (/^https:\/\/api\.luma\.com\//.test(url)) {
        res.clone().text().then((body) =>
          post({ type: "api", url, status: res.status, body: body.slice(0, 20000) })
        ).catch(() => {});
      }
    } catch (_) {}
    return res;
  };

  // 2) Camera pipeline for the scanner: optional digital zoom + "clean re-render".
  //    Chrome's BarcodeDetector reads each frame (handles dark-mode/inverted codes). When it sees a
  //    Luma check-in code, we briefly draw a crisp black-on-white copy of that same code so Luma's own
  //    scanner reads it and performs its normal check-in. Settings (then reload):
  //      localStorage.reachyZoom = "2"      digital zoom factor (default 1 = off)
  //      localStorage.reachyEnhance = "0"   disable the clean re-render (default on)
  const ls = (k, d) => { try { return localStorage.getItem(k) ?? d; } catch (_) { return d; } };
  const zoom = Math.max(1, parseFloat(ls("reachyZoom", "1")) || 1);
  const enhance = ls("reachyEnhance", "1") !== "0" && "BarcodeDetector" in window && typeof qrcode === "function";
  if ((zoom <= 1 && !enhance) || !navigator.mediaDevices?.getUserMedia) {
    post({ type: "debug", text: `camera pipeline off (zoom=${zoom}, BarcodeDetector=${"BarcodeDetector" in window})` });
    return;
  }

  const LUMA_CODE = /^https:\/\/(www\.)?(luma\.com|lu\.ma)\/check-in\//;
  const SHOW_MS = 1500;      // how long the clean copy is shown
  const REPEAT_MS = 20000;   // don't re-show the same code within this window (limits automatic re-reads)
  const DETECT_EVERY_MS = 50;   // check for a code ~20x a second
  // Ask the camera for 4K (Reachy's camera does 3840x2160). Luma still gets a normal ~1080p picture, but every
  // other check reads the middle of the frame at full 4K detail, so small codes (Apple Wallet passes) have
  // twice the pixels. Turn off with localStorage.reachyHiRes = "0". If 4K isn't available, nothing changes.
  const HI_RES = ls("reachyHiRes", "1") !== "0";
  const OUT_MAX_W = 1920;
  function withHighRes(constraints) {
    if (!HI_RES || !constraints || !constraints.video) return null;
    const video = constraints.video === true ? {} : { ...constraints.video };
    video.width = { ideal: 3840 }; video.height = { ideal: 2160 };
    return { ...constraints, video };
  }

  function drawCleanCode(ctx, text, w, h) {
    const qr = qrcode(0, "M");
    qr.addData(text);
    qr.make();
    const n = qr.getModuleCount();
    const quiet = 4;
    const size = Math.floor(Math.min(w, h) * 0.7);
    const cell = Math.floor(size / (n + 2 * quiet));
    const total = cell * (n + 2 * quiet);
    const x0 = Math.floor((w - total) / 2), y0 = Math.floor((h - total) / 2);
    ctx.fillStyle = "#fff";
    ctx.fillRect(x0, y0, total, total);
    ctx.fillStyle = "#000";
    for (let r = 0; r < n; r++)
      for (let c = 0; c < n; c++)
        if (qr.isDark(r, c)) ctx.fillRect(x0 + (c + quiet) * cell, y0 + (r + quiet) * cell, cell, cell);
  }

  const origGUM = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  navigator.mediaDevices.getUserMedia = async (constraints) => {
    let real = null;
    const hi = withHighRes(constraints);
    if (hi) {
      try { real = await origGUM(hi); }
      catch (err) { post({ type: "debug", text: `4K camera not available (${err && err.name}); using normal resolution` }); }
    }
    if (!real) real = await origGUM(constraints);
    try {
      return await buildPipeline(real);
    } catch (err) {
      // Never break Luma's scanner: fall back to the plain camera.
      post({ type: "debug", text: `camera pipeline failed, using plain camera: ${err}` });
      return real;
    }
  };

  async function buildPipeline(real) {
    const track = real.getVideoTracks()[0];
    if (!track) return real;
    const { width: vw = 1920, height: vh = 1080 } = track.getSettings();
    // What Luma sees: at most 1920 wide (a 4K canvas at 30 fps would make the scanner page sluggish).
    const width = Math.min(vw, OUT_MAX_W), height = Math.round(vh * width / vw);
    const video = document.createElement("video");
    video.muted = true; video.playsInline = true; video.srcObject = real;
    await video.play();
    const canvas = document.createElement("canvas");
    canvas.width = width; canvas.height = height;
    const ctx = canvas.getContext("2d");
    const sw = vw / zoom, sh = vh / zoom, sx = (vw - sw) / 2, sy = (vh - sh) / 2;

    const detector = enhance ? new BarcodeDetector({ formats: ["qr_code"] }) : null;
    let detecting = false, lastDetect = 0, pass = 0;
    // "center" view: the middle of the camera frame at full sensor detail (only when the camera gave us more
    // pixels than Luma's picture). Alternates with the normal "full" view.
    const hasDetail = vw >= width * 1.5;
    const view = document.createElement("canvas");
    view.width = width; view.height = height;
    const vctx = view.getContext("2d", { willReadFrequently: true });
    const nextView = () => {
      if (!hasDetail || pass++ % 2 === 0) return { name: "full", source: canvas };
      vctx.drawImage(video, (vw - width) / 2, (vh - height) / 2, width, height, 0, 0, width, height);
      return { name: "4K center", source: view };
    };
    let overlay = null;              // {text, until}
    const shownAt = new Map();       // code -> last time shown

    const draw = () => {
      try { drawFrame(); } catch (err) { /* keep the feed alive */ }
      finally { video.requestVideoFrameCallback(draw); }
    };
    const drawFrame = () => {
      const now = performance.now();
      ctx.drawImage(video, sx, sy, sw, sh, 0, 0, width, height);
      if (overlay && now < overlay.until) {
        drawCleanCode(ctx, overlay.text, width, height);
      } else {
        overlay = null;
        if (detector && !detecting && now - lastDetect > DETECT_EVERY_MS) {
          detecting = true; lastDetect = now;
          const v = nextView();
          detector.detect(v.source).then((codes) => {
            for (const c of codes) {
              const text = c.rawValue || "";
              const t = performance.now();
              if (!LUMA_CODE.test(text)) {
                if (text && t - (shownAt.get(text) || -Infinity) > 5000) {
                  shownAt.set(text, t);
                  post({ type: "seen", text: text.slice(0, 200), luma: false });
                }
                continue;
              }
              if (t - (shownAt.get(text) || -Infinity) < REPEAT_MS) continue;
              shownAt.set(text, t);
              overlay = { text, until: t + SHOW_MS };
              post({ type: "seen", text: text.replace(/pk=.*/, "pk=…"), luma: true });
              post({ type: "debug", text: `read ticket via ${v.name} view` });
              break;
            }
          }).catch(() => {}).finally(() => { detecting = false; });
        }
      }
    };
    video.requestVideoFrameCallback(draw);

    const out = canvas.captureStream(30);
    const outTrack = out.getVideoTracks()[0];
    outTrack.getSettings = () => ({ ...track.getSettings(), width, height });  // Luma's picture size
    // Camera controls (torch, focus, constraints) go to the real camera track.
    outTrack.getCapabilities = track.getCapabilities.bind(track);
    outTrack.getConstraints = track.getConstraints.bind(track);
    outTrack.applyConstraints = track.applyConstraints.bind(track);
    for (const a of real.getAudioTracks()) out.addTrack(a);
    const origStop = outTrack.stop.bind(outTrack);
    outTrack.stop = () => { origStop(); track.stop(); };
    post({ type: "debug", text: `camera pipeline on: zoom x${zoom}, enhance=${enhance}, ${track.label} camera ${vw}x${vh}, Luma sees ${width}x${height}` });
    return out;
  }
})();
