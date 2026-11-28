"""Control panel: start/stop Reachy in any mode from a web page. Run with ./scripts/panel.sh (http://127.0.0.1:8790)."""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import signal
from pathlib import Path
from typing import Callable, Sequence

from aiohttp import web

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
MODES = ("greeter", "compliment", "translator")
TRANSCRIPT_URL = "http://127.0.0.1:8780"


def usb_connected() -> bool:
    return bool(glob.glob("/dev/cu.usbmodem*"))


async def app_running() -> bool:
    proc = await asyncio.create_subprocess_exec("pgrep", "-f", "greeter.main", stdout=asyncio.subprocess.DEVNULL)
    return await proc.wait() == 0


class Launcher:
    """Runs scripts/start.sh and scripts/stop.sh, keeping the start script's output for the page."""

    def __init__(
        self,
        start_cmd: Callable[[str], Sequence[str]] = lambda mode: [str(ROOT / "scripts" / "start.sh"), mode],
        stop_cmd: Sequence[str] = (str(ROOT / "scripts" / "stop.sh"),),
        is_running: Callable[[], "asyncio.Future[bool] | object"] = app_running,
        usb: Callable[[], bool] = usb_connected,
    ) -> None:
        self._start_cmd, self._stop_cmd = start_cmd, list(stop_cmd)
        self._is_running, self._usb = is_running, usb
        self.proc: asyncio.subprocess.Process | None = None
        self.mode: str | None = None
        self.output: list[str] = []
        self.busy = False  # starting or stopping

    async def status(self) -> dict:
        starting = self.proc is not None and self.proc.returncode is None
        out = "\n".join(self.output[-40:])
        ready = "Press Ctrl+C here to stop Reachy" in out
        return {
            "running": bool(await self._is_running()),
            "starting": starting and not ready,
            "ready": starting and ready,
            "busy": self.busy,
            "mode": self.mode if starting else None,
            "usb": self._usb(),
            "output": out,
            "transcript_url": TRANSCRIPT_URL if (starting and self.mode == "translator") else None,
        }

    async def start(self, mode: str) -> tuple[bool, str]:
        if mode not in MODES:
            return False, "unknown mode"
        if self.busy:
            return False, "busy, try again in a moment"
        if (self.proc is not None and self.proc.returncode is None) or await self._is_running():
            return False, "Reachy is already running. Stop him first."
        self.mode, self.output = mode, []
        self.proc = await asyncio.create_subprocess_exec(
            *self._start_cmd(mode), cwd=str(ROOT),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # its own process group, so stop can reach the whole tree
        )
        asyncio.get_running_loop().create_task(self._collect(self.proc))
        log.info("panel: started %s", mode)
        return True, "starting"

    async def stop(self) -> tuple[bool, str]:
        if self.busy:
            return False, "busy, try again in a moment"
        self.busy = True
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._stop_cmd, cwd=str(ROOT), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            text = (await proc.communicate())[0].decode(errors="replace").strip()
            if self.proc is not None and self.proc.returncode is None:
                try:
                    os.killpg(self.proc.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
            self.output.append(text)
            log.info("panel: stopped (%s)", text)
            return proc.returncode == 0, text
        finally:
            self.busy = False

    async def _collect(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            line = line.replace("\x1b[32m✔\x1b[0m", "✔").replace("\x1b[31m✘\x1b[0m", "✘").replace("\x1b[33m!\x1b[0m", "!")
            self.output = (self.output + [line])[-200:]
        await proc.wait()


def make_panel_app(launcher: Launcher) -> web.Application:
    def json_only(request: web.Request) -> web.Response | None:
        if request.content_type != "application/json":  # blocks cross-site form posts
            return web.json_response({"ok": False, "error": "json only"}, status=415)
        return None

    async def index(_: web.Request) -> web.Response:
        return web.Response(text=PAGE, content_type="text/html", charset="utf-8")

    async def status(_: web.Request) -> web.Response:
        return web.json_response(await launcher.status(), dumps=lambda o: json.dumps(o, ensure_ascii=False))

    async def start(request: web.Request) -> web.Response:
        if (bad := json_only(request)) is not None:
            return bad
        try:
            mode = str((await request.json()).get("mode", ""))
        except (ValueError, AttributeError):
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        ok, msg = await launcher.start(mode)
        return web.json_response({"ok": ok, "message": msg}, status=200 if ok else 409)

    async def stop(request: web.Request) -> web.Response:
        if (bad := json_only(request)) is not None:
            return bad
        ok, msg = await launcher.stop()
        return web.json_response({"ok": ok, "message": msg})

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/status", status)
    app.router.add_post("/start", start)
    app.router.add_post("/stop", stop)
    return app


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reachy Control</title>
<style>
:root { --bg:#f6f5f2; --card:#fff; --ink:#1d1d1f; --muted:#6b6b70; --line:#e4e2dd; --go:#1f8a4c; --stop:#c0392b; --accent:#2f6fdb; }
@media (prefers-color-scheme: dark) { :root { --bg:#131316; --card:#1e1e22; --ink:#f2f2f4; --muted:#9a9aa2; --line:#2c2c32; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:16px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
main { max-width:760px; margin:0 auto; padding:28px 16px 60px; }
h1 { font-size:24px; margin:0 0 4px; }
.sub { color:var(--muted); margin:0 0 22px; }
.status { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:20px; }
.pill { padding:6px 12px; border-radius:999px; background:var(--card); border:1px solid var(--line); font-size:14px; }
.pill b { font-weight:600; } .ok { color:var(--go); } .bad { color:var(--stop); }
.modes { display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); gap:12px; margin-bottom:16px; }
.mode { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px; display:flex; flex-direction:column; gap:10px; }
.mode h2 { font-size:17px; margin:0; } .mode p { margin:0; color:var(--muted); font-size:14px; flex:1; }
button { font:inherit; font-weight:600; padding:10px 14px; border-radius:10px; border:0; cursor:pointer; color:#fff; background:var(--go); }
button:disabled { opacity:.45; cursor:not-allowed; }
button.stop { background:var(--stop); width:100%; padding:14px; font-size:17px; }
#link { display:block; margin:14px 0; padding:12px 14px; border-radius:10px; background:var(--card); border:1px solid var(--line); color:var(--accent); text-decoration:none; font-weight:600; }
#link[hidden] { display:none; }
pre { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; white-space:pre-wrap; font:13px/1.5 ui-monospace, Menlo, monospace; min-height:120px; margin-top:16px; }
#msg { color:var(--muted); min-height:1.4em; margin-top:10px; }
</style></head>
<body><main>
<h1>Reachy Control</h1>
<p class="sub">Start Reachy in a mode, or stop him before powering off.</p>
<div class="status">
  <span class="pill">USB: <b id="usb">…</b></span>
  <span class="pill">Reachy: <b id="state">…</b></span>
</div>
<div class="modes">
  <div class="mode"><h2>Check-in</h2><p>Welcomes guests from their Luma QR code. Needs the Luma scanner tab open.</p><button data-mode="greeter">Start check-in</button></div>
  <div class="mode"><h2>Compliments</h2><p>Compliments everyone. "Reachy, be quiet" / "wake up" / "do a dance".</p><button data-mode="compliment">Start compliments</button></div>
  <div class="mode"><h2>Translator</h2><p>Translates between two people on his left and right, with a live transcript.</p><button data-mode="translator">Start translator</button></div>
</div>
<button class="stop" id="stop">Stop Reachy</button>
<a id="link" href="http://127.0.0.1:8780" target="_blank" hidden>Open live transcript →</a>
<div id="msg"></div>
<pre id="out">No output yet.</pre>
</main>
<script>
const $ = id => document.getElementById(id);
const post = (path, body) => fetch(path, { method:"POST", headers:{ "Content-Type":"application/json" }, body:JSON.stringify(body || {}) }).then(r => r.json());
const names = { greeter:"check-in", compliment:"compliments", translator:"translator" };
async function refresh() {
  let s; try { s = await (await fetch("/status")).json(); } catch (_) { $("state").textContent = "panel offline"; return; }
  $("usb").textContent = s.usb ? "connected" : "not connected"; $("usb").className = s.usb ? "ok" : "bad";
  const state = s.busy ? "stopping…" : s.starting ? `starting ${names[s.mode] || ""}…` : s.ready ? `running: ${names[s.mode]}` : s.running ? "running" : "stopped";
  $("state").textContent = state; $("state").className = (s.ready || s.running) ? "ok" : "";
  const active = s.running || s.starting || s.ready || s.busy;
  document.querySelectorAll("[data-mode]").forEach(b => b.disabled = active || !s.usb);
  $("stop").disabled = s.busy || !(s.running || s.starting || s.ready);
  $("link").hidden = !s.transcript_url || !s.ready;
  if (s.output) $("out").textContent = s.output;
}
document.querySelectorAll("[data-mode]").forEach(b => b.onclick = async () => {
  $("msg").textContent = `Starting ${names[b.dataset.mode]}… the first start can take a few minutes.`;
  const r = await post("/start", { mode:b.dataset.mode }).catch(() => ({ message:"Panel not responding." }));
  if (!r.ok) $("msg").textContent = r.message; refresh();
});
$("stop").onclick = async () => {
  $("msg").textContent = "Stopping…"; refresh();
  const r = await post("/stop").catch(() => ({ message:"Panel not responding." }));
  $("msg").textContent = r.message || ""; refresh();
};
refresh(); setInterval(refresh, 1500);
</script></body></html>
"""


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    port = int(os.environ.get("PANEL_PORT", "8790"))
    print(f"Reachy control panel on http://127.0.0.1:{port}  (Ctrl+C to close the panel)", flush=True)
    web.run_app(make_panel_app(Launcher()), host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    main()
