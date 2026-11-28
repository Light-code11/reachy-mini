"""OpenAI realtime voice for Reachy: mic -> model -> speaker, with exact forced lines and barge-in."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

import numpy as np
from openai import AsyncOpenAI

from greeter.audio import float_to_pcm16, pcm16_to_float, resample, to_mono
from greeter.config import Settings

log = logging.getLogger(__name__)

MODEL_RATE = 24000
FORCED_PREFIX = "Say exactly this, brightly and warmly, and nothing else: "

ToolHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def build_session_config(
    settings: Settings,
    instructions: str,
    tools: list[dict],
    max_output_tokens: int = 200,
    noise_reduction: str | None = None,  # "near_field": ignore distant voices (translator)
    vad_threshold: float | None = None,  # higher = needs louder, closer speech to count as a turn
    auto_reply: bool = True,  # False: still transcribe what people say, but never answer (quiet mode)
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "type": "realtime",
        "instructions": instructions,
        "output_modalities": ["audio"],
        "max_output_tokens": max_output_tokens,
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": MODEL_RATE},
                "transcription": {"model": "gpt-4o-transcribe"},
                "turn_detection": {
                    "type": "server_vad",
                    "interrupt_response": auto_reply,
                    "create_response": auto_reply,
                    "silence_duration_ms": 600,
                },
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": MODEL_RATE},
                "voice": settings.voice,
            },
        },
        "tools": tools,
    }
    if noise_reduction:
        config["audio"]["input"]["noise_reduction"] = {"type": noise_reduction}
    if vad_threshold is not None:
        config["audio"]["input"]["turn_detection"]["threshold"] = vad_threshold
    if tools:
        config["tool_choice"] = "auto"
    return config


def forced_response(text: str, tag: str) -> dict[str, Any]:
    return {
        "conversation": "none",
        "instructions": FORCED_PREFIX + text,
        "metadata": {"tag": tag},
        "output_modalities": ["audio"],
        "input": [],
    }


class RealtimeVoice:
    """Owns the realtime session. Mic audio is only sent while ``set_listening(True)``."""

    def __init__(
        self,
        settings: Settings,
        mini: Any,
        instructions: str,
        tools: list[dict] | None = None,
        tool_handler: ToolHandler | None = None,
        half_duplex: bool = False,
        max_output_tokens: int = 200,
        noise_reduction: str | None = None,
        vad_threshold: float | None = None,
    ) -> None:
        self._settings = settings
        self._mini = mini
        self._instructions = instructions
        self._tools = tools or []
        self._tool_handler = tool_handler
        self._half_duplex = half_duplex  # don't send mic audio while Reachy is talking (no hearing himself)
        self._max_output_tokens = max_output_tokens
        self._noise_reduction = noise_reduction
        self._vad_threshold = vad_threshold
        self.on_connected: Callable[[], Awaitable[None]] | None = None
        self._conn: Any = None
        self._connected = asyncio.Event()
        self._listening = False
        self._forced_active = False
        self._forced_waiters: dict[str, asyncio.Future] = {}
        self._discarded: set[str] = set()
        self._current_response: str | None = None
        self._playback_end = 0.0
        self._last_activity = -1e9
        self.on_guest_speech: Callable[[], None] | None = None
        self.on_transcript: Callable[[str], None] | None = None
        self.on_reply_transcript: Callable[[str], None] | None = None  # what Reachy said (not forced lines)
        self._muted = False
        self._in_rate = mini.media.get_input_audio_samplerate()
        self._out_rate = mini.media.get_output_audio_samplerate()

    # ---- public API -------------------------------------------------------------------

    def set_listening(self, enabled: bool) -> None:
        self._listening = enabled

    def note_activity(self, now: float | None = None) -> None:
        self._last_activity = time.monotonic() if now is None else now

    def active_within(self, seconds: float, now: float | None = None) -> bool:
        """True if a guest spoke or Reachy was answering within the last ``seconds``."""
        now = time.monotonic() if now is None else now
        return now - self._last_activity < seconds

    def speaking(self, now: float | None = None, grace_s: float = 1.0) -> bool:
        """True while Reachy's own audio is playing (plus a short grace), whatever the room is doing."""
        now = time.monotonic() if now is None else now
        return now < self._playback_end + grace_s

    def mic_blocked(self, now: float | None = None) -> bool:
        """Half-duplex: True while Reachy's own audio is (or just was) playing."""
        now = time.monotonic() if now is None else now
        return self._half_duplex and now < self._playback_end + 0.3

    def set_muted(self, muted: bool) -> None:
        """Quiet mode: cut off what he's saying and play none of his replies (he keeps listening for "wake up")."""
        self._muted = muted
        if muted and self._conn is not None:
            asyncio.get_running_loop().create_task(self._interrupt())

    def _session_config(self) -> dict[str, Any]:
        return build_session_config(self._settings, self._instructions, self._tools, self._max_output_tokens,
                                    self._noise_reduction, self._vad_threshold)

    def update_instructions(self, text: str) -> None:
        """Replace the session instructions (now, and on every reconnect)."""
        self._instructions = text
        if self._conn is not None:
            asyncio.get_running_loop().create_task(
                self._conn.session.update(session={"type": "realtime", "instructions": text})
            )

    async def prompt(self, text: str) -> None:
        """Nudge the model with a system note and ask it to respond (e.g. to start a setup flow)."""
        await self._connected.wait()
        await self._conn.conversation.item.create(
            item={"type": "message", "role": "system", "content": [{"type": "input_text", "text": text}]}
        )
        await self._conn.response.create()

    async def say_exact(self, text: str, timeout_s: float = 20.0, connect_timeout_s: float = 5.0) -> None:
        """Speak ``text`` verbatim (out-of-band), interrupting any chat, and wait until it has played."""
        try:
            await asyncio.wait_for(self._connected.wait(), connect_timeout_s)
        except asyncio.TimeoutError:
            log.warning("voice not connected; skipped line: %r", text)
            return
        await self._interrupt()
        tag = uuid.uuid4().hex
        done = asyncio.get_running_loop().create_future()
        self._forced_waiters[tag] = done
        self._forced_active = True
        try:
            await self._conn.response.create(response=forced_response(text, tag))
            await asyncio.wait_for(done, timeout_s)
            await self._wait_playback()
        except asyncio.TimeoutError:
            log.warning("forced line timed out: %r", text)
        finally:
            self._forced_waiters.pop(tag, None)
            self._forced_active = False

    async def run(self) -> None:
        """Connect and keep the session alive, reconnecting with backoff."""
        client = AsyncOpenAI(api_key=self._settings.openai_api_key)
        backoff = 1.0
        while True:
            try:
                async with client.realtime.connect(model=self._settings.realtime_model) as conn:
                    self._conn = conn
                    await conn.session.update(session=self._session_config())
                    self._connected.set()
                    log.info("realtime connected (%s)", self._settings.realtime_model)
                    backoff = 1.0
                    if self.on_connected:
                        asyncio.create_task(self.on_connected())
                    await self._run_session()
                    log.warning("realtime session closed; reconnecting")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("realtime session dropped; reconnecting in %.0fs", backoff)
            finally:
                self._connected.clear()
                self._conn = None
                for fut in self._forced_waiters.values():
                    if not fut.done():
                        fut.set_result(None)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    # ---- internals --------------------------------------------------------------------

    async def _run_session(self) -> None:
        """Run mic + event loops; when either ends or fails, stop the other (never leave one orphaned)."""
        tasks = [asyncio.create_task(self._mic_loop()), asyncio.create_task(self._event_loop())]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for t in done:
            if not t.cancelled() and t.exception():
                raise t.exception()

    async def _mic_loop(self) -> None:
        media = self._mini.media
        media.start_recording()
        stats = {"got": 0, "empty": 0, "sent": 0, "peak": 0.0}
        last_report = time.monotonic()
        while True:
            if time.monotonic() - last_report >= 15.0:
                log.info("mic: %d chunks, %d empty, %d sent, peak %.3f, listening=%s",
                         stats["got"], stats["empty"], stats["sent"], stats["peak"], self._listening)
                stats = {"got": 0, "empty": 0, "sent": 0, "peak": 0.0}
                last_report = time.monotonic()
            sample = await asyncio.to_thread(media.get_audio_sample)
            if sample is None or len(sample) == 0:
                stats["empty"] += 1
                await asyncio.sleep(0.01)
                continue
            stats["got"] += 1
            stats["peak"] = max(stats["peak"], float(np.max(np.abs(sample))))
            if not self._listening or self._forced_active or self.mic_blocked():
                continue
            stats["sent"] += 1
            pcm = float_to_pcm16(resample(to_mono(sample), self._in_rate, MODEL_RATE))
            await self._conn.input_audio_buffer.append(audio=base64.b64encode(pcm).decode("ascii"))

    async def _event_loop(self) -> None:
        self._mini.media.start_playing()
        async for event in self._conn:
            t = event.type
            if t == "input_audio_buffer.speech_started":
                log.info("voice: guest started speaking")
                if self.on_guest_speech and not self._forced_active:
                    self.on_guest_speech()
            elif t == "conversation.item.input_audio_transcription.completed":
                log.info("voice: heard %r", getattr(event, "transcript", ""))
                if self.on_transcript:
                    self.on_transcript(getattr(event, "transcript", "") or "")
            if t == "input_audio_buffer.speech_started" and not self._forced_active:
                self.note_activity()
                self._flush_playback()
            elif t == "response.created":
                self._current_response = event.response.id
                if self._muted and not self._forced_active:
                    log.info("voice: quiet, not replying")
                    asyncio.create_task(self._interrupt())
            elif t == "response.output_audio.delta":
                if event.response_id in self._discarded or (self._muted and not self._forced_active):
                    continue
                if not self._forced_active:
                    self.note_activity()
                self._play(base64.b64decode(event.delta))
            elif t == "response.output_audio_transcript.done":
                if self.on_reply_transcript and not self._forced_active and event.response_id not in self._discarded:
                    self.on_reply_transcript(getattr(event, "transcript", "") or "")
            elif t == "response.done":
                self._on_response_done(event.response)
            elif t == "response.function_call_arguments.done":
                asyncio.create_task(self._run_tool(event.call_id, event.name, event.arguments))
            elif t == "error":
                log.warning("realtime error: %s", getattr(event, "error", event))

    def _on_response_done(self, response: Any) -> None:
        if self._current_response == response.id:
            self._current_response = None
        tag = (getattr(response, "metadata", None) or {}).get("tag")
        fut = self._forced_waiters.get(tag) if tag else None
        if fut and not fut.done():
            fut.set_result(None)

    async def _run_tool(self, call_id: str, name: str, arguments: str) -> None:
        try:
            args = json.loads(arguments or "{}")
            result = await self._tool_handler(name, args) if self._tool_handler else {"error": "no tools"}
        except Exception as e:
            log.exception("tool %s failed (arguments %r)", name, (arguments or "")[:200])
            result = {"error": str(e)}
        await self._conn.conversation.item.create(
            item={"type": "function_call_output", "call_id": call_id, "output": json.dumps(result)}
        )
        await self._conn.response.create()

    async def _interrupt(self) -> None:
        if self._current_response:
            self._discarded.add(self._current_response)
            try:
                await self._conn.response.cancel(response_id=self._current_response)
            except Exception:
                pass
            self._current_response = None
        self._flush_playback()

    def _flush_playback(self) -> None:
        try:
            self._mini.media.audio.clear_player()
        except Exception:
            log.debug("clear_player unavailable", exc_info=True)
        self._playback_end = time.monotonic()

    def _play(self, pcm: bytes) -> None:
        samples = resample(pcm16_to_float(pcm), MODEL_RATE, self._out_rate)
        self._mini.media.push_audio_sample(np.asarray(samples, dtype=np.float32))
        now = time.monotonic()
        self._playback_end = max(self._playback_end, now) + len(samples) / self._out_rate

    async def _wait_playback(self) -> None:
        remaining = self._playback_end - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining + 0.2)
