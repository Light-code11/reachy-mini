"""Translator mode: a live local web page showing what each person says, always with an English line."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

from aiohttp import web

log = logging.getLogger(__name__)

TRANSLATE_PROMPT = (
    "You label one line of a live interpreted conversation. Reply with JSON only, no prose: "
    '{"language": "<English name of the language the line is in>", "english": "<faithful English translation>"}. '
    "If the line is already English, copy it into \"english\" unchanged."
)


@dataclass
class Line:
    who: str               # "left", "right", "reachy" or "unknown"
    text: str              # what was said, as heard
    english: str | None    # English version (same as text when it's English)
    language: str | None = None
    to: str | None = None  # for Reachy's lines: the side he was speaking to
    at: float = field(default_factory=time.time)


def is_english(language: str | None) -> bool:
    return (language or "").strip().lower() in ("english", "en")


def side_for(language: str | None, left: str | None, right: str | None) -> str:
    """Which person spoke, judged by language. 'unknown' before setup, or if both speak the same language."""
    lang = (language or "").strip().lower()
    l, r = (left or "").strip().lower(), (right or "").strip().lower()
    if not lang or not l or not r or l == r:
        return "unknown"
    if lang == l:
        return "left"
    if lang == r:
        return "right"
    return "unknown"


async def label_line(client: Any, model: str, text: str, timeout_s: float = 6.0) -> tuple[str | None, str | None]:
    """(language, english) for one line; (None, None) if the model is slow or fails."""
    try:
        response = await asyncio.wait_for(
            client.responses.create(model=model, instructions=TRANSLATE_PROMPT, input=text, max_output_tokens=300),
            timeout=timeout_s,
        )
        data = json.loads((getattr(response, "output_text", "") or "").strip().strip("`").removeprefix("json"))
        return (str(data.get("language") or "") or None, str(data.get("english") or "") or None)
    except Exception as e:  # never let the page break the translator
        log.info("transcript translation skipped: %s", e)
        return None, None


class TranscriptBoard:
    """Keeps the conversation and pushes each new line to every open page (server-sent events)."""

    def __init__(self, max_lines: int = 200) -> None:
        self.lines: list[Line] = []
        self.languages: dict[str, str | None] = {"left": None, "right": None}
        self._max = max_lines
        self._subscribers: set[asyncio.Queue[str]] = set()

    def add(self, line: Line) -> None:
        self.lines = (self.lines + [line])[-self._max:]
        self._publish({"type": "line", "line": asdict(line)})

    def set_languages(self, left: str | None, right: str | None) -> None:
        self.languages = {"left": left, "right": right}
        self._publish({"type": "languages", **self.languages})

    def clear(self) -> None:
        self.lines = []
        self._publish({"type": "clear"})

    def snapshot(self) -> dict[str, Any]:
        return {"languages": self.languages, "lines": [asdict(l) for l in self.lines]}

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)

    def _publish(self, msg: dict[str, Any]) -> None:
        data = json.dumps(msg, ensure_ascii=False)
        for q in list(self._subscribers):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass


def _clean_language(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) > 40 or not all(c.isalpha() or c in " -()'" for c in text):
        raise ValueError("bad language")
    return text.title() or None


def make_transcript_app(
    board: TranscriptBoard,
    set_languages: Callable[[str | None, str | None], Awaitable[None]] | None = None,
    reset: Callable[[], Awaitable[None]] | None = None,
) -> web.Application:
    async def post_languages(request: web.Request) -> web.Response:
        if request.content_type != "application/json":  # blocks cross-site form posts
            return web.json_response({"ok": False, "error": "json only"}, status=415)
        if set_languages is None:
            return web.json_response({"ok": False, "error": "not available"}, status=404)
        try:
            body = await request.json()
            left, right = _clean_language(body.get("left")), _clean_language(body.get("right"))
        except (ValueError, AttributeError):
            return web.json_response({"ok": False, "error": "bad languages"}, status=400)
        await set_languages(left, right)
        return web.json_response({"ok": True, "left": left, "right": right})

    async def post_reset(request: web.Request) -> web.Response:
        if request.content_type != "application/json":
            return web.json_response({"ok": False, "error": "json only"}, status=415)
        if reset is None:
            return web.json_response({"ok": False, "error": "not available"}, status=404)
        await reset()
        return web.json_response({"ok": True})

    async def index(_: web.Request) -> web.Response:
        return web.Response(text=PAGE, content_type="text/html", charset="utf-8")

    async def state(_: web.Request) -> web.Response:
        return web.json_response(board.snapshot(), dumps=lambda o: json.dumps(o, ensure_ascii=False))

    async def events(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(request)
        q = board.subscribe()
        try:
            snap = json.dumps({"type": "snapshot", **board.snapshot()}, ensure_ascii=False)
            await resp.write(f"data: {snap}\n\n".encode())
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15)
                    await resp.write(f"data: {data}\n\n".encode())
                except asyncio.TimeoutError:
                    await resp.write(b": keep-alive\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            board.unsubscribe(q)
        return resp

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/state", state)
    app.router.add_get("/events", events)
    app.router.add_post("/languages", post_languages)
    app.router.add_post("/reset", post_reset)
    return app


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reachy Translator</title>
<style>
:root { --bg:#f6f5f2; --card:#fff; --ink:#1d1d1f; --muted:#6b6b70; --left:#2f6fdb; --right:#d0532f; --reachy:#7a7a80; --line:#e4e2dd; }
@media (prefers-color-scheme: dark) { :root { --bg:#131316; --card:#1e1e22; --ink:#f2f2f4; --muted:#9a9aa2; --line:#2c2c32; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:17px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
header { position:sticky; top:0; background:var(--bg); border-bottom:1px solid var(--line); padding:14px 20px; display:flex; gap:16px; align-items:center; z-index:1; }
header h1 { font-size:18px; margin:0; flex:1; }
.tag { font-size:14px; padding:4px 10px; border-radius:999px; color:#fff; }
.tag.left { background:var(--left); } .tag.right { background:var(--right); }
button { font:inherit; font-size:14px; padding:6px 12px; border-radius:8px; border:1px solid var(--line); background:var(--card); color:var(--ink); cursor:pointer; }
form#controls { position:sticky; top:57px; z-index:1; background:var(--bg); border-bottom:1px solid var(--line); padding:12px 20px; display:flex; flex-wrap:wrap; gap:12px; align-items:center; }
form#controls[hidden] { display:none; }
form#controls input { font:inherit; font-size:15px; padding:6px 10px; border-radius:8px; border:1px solid var(--line); background:var(--card); color:var(--ink); width:150px; }
form#controls button[type=submit] { background:var(--left); color:#fff; border-color:var(--left); }
#status { color:var(--muted); font-size:14px; }
#dot { width:9px; height:9px; border-radius:50%; background:#c33; } #dot.on { background:#2a2; }
main { max-width:980px; margin:0 auto; padding:20px 16px 80px; display:flex; flex-direction:column; gap:12px; }
.row { display:flex; } .row.left { justify-content:flex-start; } .row.right { justify-content:flex-end; } .row.reachy, .row.unknown { justify-content:center; }
.bubble { max-width:72%; background:var(--card); border:1px solid var(--line); border-radius:16px; padding:12px 16px; }
.row.left .bubble { border-left:4px solid var(--left); } .row.right .bubble { border-right:4px solid var(--right); }
.row.reachy .bubble { max-width:80%; font-size:15px; color:var(--muted); background:transparent; border-style:dashed; }
.who { font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin-bottom:4px; }
.text { font-size:22px; } .row.reachy .text { font-size:16px; }
.en { margin-top:6px; padding-top:6px; border-top:1px solid var(--line); color:var(--muted); font-size:18px; }
.en::before { content:"EN  "; font-size:11px; font-weight:600; letter-spacing:.08em; }
.empty { text-align:center; color:var(--muted); margin-top:60px; }
@media (max-width:600px) { .bubble { max-width:92%; } .text { font-size:19px; } }
</style></head>
<body>
<header><span id="dot" title="live"></span><h1>Reachy Translator</h1>
<span class="tag left" id="lang-left">Left: …</span><span class="tag right" id="lang-right">Right: …</span>
<button id="edit" type="button">Change languages</button></header>
<form id="controls" hidden>
  <label>Left <input id="in-left" list="langs" autocomplete="off" placeholder="e.g. Japanese"></label>
  <label>Right <input id="in-right" list="langs" autocomplete="off" placeholder="e.g. English"></label>
  <button type="submit">Set languages</button>
  <button type="button" id="reset" class="ghost">New conversation</button>
  <span id="status"></span>
</form>
<datalist id="langs">
  <option>English</option><option>Mandarin</option><option>Cantonese</option><option>Spanish</option><option>Arabic</option>
  <option>Hindi</option><option>Marathi</option><option>Tamil</option><option>Bengali</option><option>Punjabi</option>
  <option>French</option><option>German</option><option>Italian</option><option>Portuguese</option><option>Russian</option>
  <option>Japanese</option><option>Korean</option><option>Vietnamese</option><option>Thai</option><option>Indonesian</option>
  <option>Filipino</option><option>Turkish</option><option>Greek</option><option>Persian</option><option>Urdu</option>
</datalist>
<main id="log"><p class="empty" id="empty">Waiting for the conversation to start…</p></main>
<script>
const log = document.getElementById("log"), dot = document.getElementById("dot");
const esc = s => (s || "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let langs = { left: null, right: null };
function setLangs(l, r) {
  langs = { left: l, right: r };
  document.getElementById("lang-left").textContent = "Left: " + (l || "…");
  document.getElementById("lang-right").textContent = "Right: " + (r || "…");
}
const form = document.getElementById("controls"), status = document.getElementById("status");
const post = (path, body) => fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
document.getElementById("edit").onclick = () => {
  form.hidden = !form.hidden;
  document.getElementById("in-left").value = langs.left || "";
  document.getElementById("in-right").value = langs.right || "";
  status.textContent = "";
};
form.onsubmit = async (e) => {
  e.preventDefault();
  const left = document.getElementById("in-left").value, right = document.getElementById("in-right").value;
  status.textContent = "Setting…";
  const r = await post("/languages", { left, right }).catch(() => null);
  status.textContent = r && r.ok ? "Set. Reachy will confirm out loud." : "Couldn't set that. Use plain language names.";
  if (r && r.ok) setTimeout(() => { form.hidden = true; }, 1200);
};
document.getElementById("reset").onclick = async () => {
  status.textContent = "Starting over…";
  const r = await post("/reset", {}).catch(() => null);
  status.textContent = r && r.ok ? "New conversation. Reachy will ask each person again." : "Couldn't reset.";
};
function add(line) {
  document.getElementById("empty")?.remove();
  const who = line.who === "reachy" ? `Reachy${line.to ? " → " + line.to : ""}` : line.who === "unknown" ? "Someone" : line.who + " person";
  const sameAsEn = !line.english || line.english.trim() === line.text.trim();
  const row = document.createElement("div");
  row.className = "row " + line.who;
  row.innerHTML = `<div class="bubble"><div class="who">${esc(who)}${line.language ? " · " + esc(line.language) : ""}</div>
    <div class="text">${esc(line.text)}</div>${sameAsEn ? "" : `<div class="en">${esc(line.english)}</div>`}</div>`;
  log.appendChild(row);
  window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
}
function connect() {
  const es = new EventSource("/events");
  es.onopen = () => dot.classList.add("on");
  es.onerror = () => dot.classList.remove("on");
  es.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.type === "snapshot") { log.innerHTML = '<p class="empty" id="empty">Waiting for the conversation to start…</p>'; setLangs(m.languages.left, m.languages.right); m.lines.forEach(add); }
    else if (m.type === "line") add(m.line);
    else if (m.type === "languages") setLangs(m.left, m.right);
    else if (m.type === "clear") log.innerHTML = '<p class="empty" id="empty">New conversation…</p>';
  };
}
connect();
</script></body></html>
"""
