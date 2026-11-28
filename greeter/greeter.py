"""What Reachy does for each scan result and each face update."""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Awaitable, Callable

from greeter.arrival import ArrivalPrompter
from greeter.checkin import CheckinGate, ScanResult
from greeter.lines import EXPRESS_OFF_LINE, FALLBACK_COMPLIMENT, FRONT_DESK_LINE, QR_PROMPT_LINE, WRONG_CODE_LINE, all_set_line, welcome_back_line, welcome_line

log = logging.getLogger(__name__)

NAMED_LINE_MAX_AGE_S = 10.0   # never greet someone by name after they've likely walked off
GENERIC_LINE_MAX_AGE_S = 15.0
AUTO_SEARCH_COOLDOWN_S = 20.0  # heard someone but can't see them: look around at most this often
AUTO_SEARCH_AFTER_FACE_S = 8.0  # a face just vanished (phone held up over it?): stay put, don't turn from the QR
AUTO_SEARCH_AFTER_SCAN_S = 15.0  # someone is at the scanner: stay put


class Greeter:
    def __init__(
        self,
        speech: Any,
        moves: Any,
        gate: CheckinGate,
        arrival: ArrivalPrompter,
        get_compliment: Callable[[], Awaitable[str | None]],
        compliment_window_s: float = 6.0,
        event_name: str = "OpenAI Dev Day",
    ) -> None:
        self._speech = speech
        self._moves = moves
        self._gate = gate
        self._arrival = arrival
        self._get_compliment = get_compliment
        self._compliment_window_s = compliment_window_s
        self._event_name = event_name
        self._tasks: set[asyncio.Task] = set()
        self.moving = False
        self._move_lock = asyncio.Lock()
        self._welcome_seq = 0
        self.conversation_active: Callable[[], bool] = lambda: False
        self._express_warned_at = -math.inf
        self._last_auto_search = -math.inf
        self._last_face_at = -math.inf
        self._last_scan_at = -math.inf
        self.on_qr_prompt: Callable[[float], None] | None = None

    @property
    def last_scan_at(self) -> float:
        return self._last_scan_at

    async def handle_scan(self, r: ScanResult) -> None:
        if r.kind == "express_off":
            if r.at - self._express_warned_at >= 60.0:
                self._express_warned_at = r.at
                await self._speech.put(EXPRESS_OFF_LINE)
            return
        self._arrival.note_scan(r.at)
        self._last_scan_at = r.at
        decision = self._gate.decide(r)
        log.info("scan %s %r -> %s", r.kind, r.name, decision)
        if decision == "welcome":
            self._welcome_seq += 1
            await self._speech.put(welcome_line(r.name, self._event_name), max_age_s=NAMED_LINE_MAX_AGE_S)  # queued before its compliment
            self._spawn(self._compliment_after_welcome(self._welcome_seq))
            self._spawn(self._move(self._moves.welcome))
        elif decision == "welcome_back":
            await self._speech.put(welcome_back_line(r.name), max_age_s=NAMED_LINE_MAX_AGE_S)
            self._spawn(self._move(self._moves.welcome))
        elif decision == "all_set":
            await self._speech.put(all_set_line(r.name), max_age_s=NAMED_LINE_MAX_AGE_S)
        elif decision == "front_desk":
            await self._speech.put(FRONT_DESK_LINE, max_age_s=GENERIC_LINE_MAX_AGE_S)
            self._spawn(self._move(self._moves.front_desk))
        elif decision == "wrong_code":
            await self._speech.put(WRONG_CODE_LINE, max_age_s=GENERIC_LINE_MAX_AGE_S)

    async def handle_face(self, present: bool, now: float) -> None:
        if present:
            self._last_face_at = now
        if self.moving:
            return
        if self._arrival.update(present, now, can_prompt=not self.conversation_active()):
            await self._speech.put(QR_PROMPT_LINE, max_age_s=GENERIC_LINE_MAX_AGE_S)
            self._spawn(self._move(self._moves.perk_up))
            if self.on_qr_prompt:
                self.on_qr_prompt(now)

    async def find_person(self, found: Callable[[], bool], now: float, auto: bool = False) -> bool:
        """Look around for a face. ``auto`` searches (heard a voice, saw nobody) are rate-limited."""
        if auto and (
            now - self._last_auto_search < AUTO_SEARCH_COOLDOWN_S
            or now - self._last_face_at < AUTO_SEARCH_AFTER_FACE_S
            or now - self._last_scan_at < AUTO_SEARCH_AFTER_SCAN_S
        ):
            return False
        if auto:
            self._last_auto_search = now
        result = False

        async def run() -> None:
            nonlocal result
            result = await self._moves.search(found)

        await self._move(run)
        return result

    async def act_sad(self) -> None:
        """Droop sadly (someone was mean). Runs in the background so Reachy can answer straight away."""
        log.info("acting sad")
        self._spawn(self._move(self._moves.sad))

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _compliment_after_welcome(self, seq: int) -> None:
        try:
            text = await asyncio.wait_for(self._get_compliment(), self._compliment_window_s)
        except asyncio.TimeoutError:
            log.info("compliment too slow; using the generic one")
            text = None
        if seq != self._welcome_seq:
            log.info("compliment dropped: another guest was welcomed since")
            return
        await self._speech.put(text or FALLBACK_COMPLIMENT, max_age_s=NAMED_LINE_MAX_AGE_S)

    async def _move(self, fn: Callable[[], Awaitable[None]]) -> None:
        async with self._move_lock:  # one move at a time; tracking resumes only after the last
            self.moving = True
            try:
                await fn()
            except Exception:
                log.exception("move failed")
            finally:
                self.moving = False

    def _spawn(self, coro: Awaitable[None]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task
