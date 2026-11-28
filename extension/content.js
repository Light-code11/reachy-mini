// Isolated-world script: turns Luma scanner results into /scan events for Reachy.
// DEBUG = true also records page text and raw API bodies to /debug (contains guest data; logs/ only).
const DEBUG = false;
const { parseScanResponse, classifySeenCode, isScannerPage, describeCode } = globalThis.ReachyParse;
const send = (msg) => { try { chrome.runtime.sendMessage(msg); } catch (_) {} };

window.addEventListener("message", (e) => {
  if (e.source !== window || e.data?.source !== "reachy-greeter") return;
  const d = e.data;
  if (d.type === "api") {
    if (DEBUG) send({ type: "debug", text: `API ${d.status} ${d.url}\n${d.body}` });
    const result = parseScanResponse(d.url, d.status, d.body);
    if (result) {
      // Always record the shape of check-in replies (status/message/keys only) so mis-reads can be diagnosed.
      let info = "";
      try {
        const j = JSON.parse(d.body);
        info = `status=${JSON.stringify(j.status)} message=${JSON.stringify(j.message)} keys=${Object.keys(j).join(",")}`;
      } catch (_) { info = `unparsed body (${(d.body || "").length} chars)`; }
      send({ type: "debug", text: `EXPRESS http=${d.status} ${info} -> ${result.kind}` });
      send({ type: "scan", ...result });
    }
  } else if (d.type === "seen") {
    if (DEBUG) send({ type: "debug", text: `SEEN ${d.luma ? "luma" : "other"} ${d.text}` });
    if (!d.luma && classifySeenCode(d.text) === "wrong_code") send({ type: "scan", kind: "wrong_code", name: null });
    else if (!d.luma) send({ type: "debug", text: `SEEN non-ticket code: ${describeCode(d.text)}` });
  } else if (d.type === "debug") {
    send({ type: "debug", text: d.text });
  }
});

// Debug-only page-text recorder (used to discover Luma's result wording).
const recent = new Map();
function record(text) {
  text = (text || "").trim();
  if (!text || text.length > 500) return;
  const now = Date.now();
  if (now - (recent.get(text) || 0) < 2000) return;
  recent.set(text, now);
  send({ type: "debug", text: `TEXT ${text}` });
}

function start() {
  if (DEBUG) {
    new MutationObserver((muts) => {
      for (const m of muts) {
        if (m.type === "characterData") record(m.target.parentElement?.innerText);
        for (const n of m.addedNodes) record(n.nodeType === 1 ? n.innerText : n.textContent);
      }
    }).observe(document.body, { childList: true, subtree: true, characterData: true });
  }
  send({ type: "debug", text: `bridge loaded on ${location.pathname}${location.search}` });
}
// --- Keep Luma's scanner in Express mode -------------------------------------------------------
// After every reload Luma shows a "Scan Options" dialog (Standard / Express). We pick Express, the
// same click a host would make. If the header still doesn't say "Express Mode On", Reachy warns.
const leafWithText = (re) =>
  [...document.querySelectorAll("button, label, [role=radio], [role=button], div, span")]
    .filter((el) => re.test((el.innerText || "").trim()))
    .sort((a, b) => (a.innerText || "").length - (b.innerText || "").length)[0];

function chooseExpress() {
  const text = document.body?.innerText || "";
  if (!/Scan Options/.test(text) || !/Automatically check in/.test(text)) return false;
  const option = leafWithText(/^Express\b[\s\S]*Automatically check in/) || leafWithText(/^Express$/);
  const confirm = [...document.querySelectorAll("button")].find((b) => (b.innerText || "").trim() === "Confirm");
  if (!option || !confirm) return false;
  option.click();
  setTimeout(() => confirm.click(), 250);
  send({ type: "debug", text: "auto-selected Express mode" });
  return true;
}

let offSince = null;
function checkExpress() {
  if (!isScannerPage(location.pathname, location.search)) { offSince = null; return; }
  if (chooseExpress()) return;
  const on = /Express Mode On/.test(document.body?.innerText || "");
  if (on) { offSince = null; return; }
  offSince = offSince ?? Date.now();
  if (Date.now() - offSince > 8000) send({ type: "scan", kind: "express_off", name: null });
}

if (document.body) start(); else document.addEventListener("DOMContentLoaded", start);
setInterval(checkExpress, 2000);
