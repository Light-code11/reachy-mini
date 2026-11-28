"""Short expressive moves. Daemon head tracking is paused during each move so they don't fight."""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from typing import Any, Callable

import httpx
import numpy as np
from reachy_mini.utils import create_head_pose

log = logging.getLogger(__name__)

# Where Reachy looks when hunting for someone: body turns, head level (degrees, 0 = straight ahead).
SEARCH_YAWS_DEG = [-40, -80, -40, 0, 40, 80, 40]
# Translator mode (positive yaw = Reachy's left): sweep outward until he sees a face, remember that spot.
SIDE_SIGN = {"left": 1.0, "right": -1.0}
SIDE_SWEEP_DEG = [30, 45, 60, 75, 90, 105, 120]  # body can go to 160
SIDE_DEFAULT_DEG = 90  # where he settles if he finds nobody
LOOKS_PER_STOP = 3     # camera checks at each stop (one frame can miss a face)
DANCE_MIN_S = 6.0      # one dance from Pollen's library, repeated until it lasts at least this long


def _pose(pitch=0.0, roll=0.0, yaw=0.0):
    return create_head_pose(pitch=pitch, roll=roll, yaw=yaw, degrees=True)


class Moves:
    def __init__(self, mini: Any, daemon_url: str, sees_face: Callable[[], bool] | None = None) -> None:
        self._mini = mini
        self._daemon = daemon_url.rstrip("/")
        self.sees_face = sees_face
        self._side: str | None = None
        self._spots: dict[str, float] = {}  # side -> body yaw (deg) where that person was last found

    async def welcome(self) -> None:
        """Antennas up with a wiggle and a small happy nod (~1.4 s)."""
        await self._sequence([
            (dict(head=_pose(pitch=-8), antennas=np.deg2rad([40, -40])), 0.35),
            (dict(antennas=np.deg2rad([15, -15])), 0.2),
            (dict(head=_pose(pitch=6), antennas=np.deg2rad([40, -40])), 0.35),
            (dict(head=_pose(), antennas=[0.0, 0.0]), 0.5),
        ])

    async def front_desk(self) -> None:
        """Gentle sympathetic tilt with antennas drooping (~1.3 s)."""
        await self._sequence([
            (dict(head=_pose(roll=12, pitch=4), antennas=np.deg2rad([-30, 30])), 0.6),
            (dict(head=_pose(), antennas=[0.0, 0.0]), 0.7),
        ])

    async def sad(self) -> None:
        """Head drops, antennas droop, a slow little shake, then a hopeful recovery (~4 s)."""
        await self._sequence([
            (dict(head=_pose(pitch=22), antennas=np.deg2rad([-70, 70])), 1.0),
            (dict(head=_pose(pitch=24, roll=-6)), 0.6),
            (dict(head=_pose(pitch=24, roll=6)), 0.8),
            (dict(head=_pose(pitch=20)), 0.6),
            (dict(head=_pose(), antennas=[0.0, 0.0]), 1.0),
        ])

    async def perk_up(self) -> None:
        """Small curious antenna lift (~0.8 s)."""
        await self._sequence([
            (dict(antennas=np.deg2rad([25, -25])), 0.3),
            (dict(antennas=[0.0, 0.0]), 0.5),
        ])

    async def search(self, found: Callable[[], bool]) -> bool:
        """Turn side to side until ``found()`` sees a face; then hand back to face tracking. ~6 s at most."""
        await self._tracking(False)
        try:
            for yaw in SEARCH_YAWS_DEG:
                await asyncio.to_thread(
                    self._goto, head=_pose(yaw=yaw), antennas=np.deg2rad([20, -20]),
                    body_yaw=float(np.deg2rad(yaw)), duration=0.7,
                )
                if await asyncio.to_thread(found):
                    log.info("search: found someone at %d deg", yaw)
                    return True  # stay facing them; tracking takes over
            await asyncio.to_thread(self._goto, head=_pose(), antennas=[0.0, 0.0], body_yaw=0.0, duration=0.8)
            log.info("search: nobody found")
            return False
        finally:
            await self._tracking(True)

    async def turn_to(self, side: str) -> None:
        """Translator mode: face the person on his left or right.

        Goes straight to where that person was last found; if they're not there (or it's the first time),
        sweeps outward on that side until he sees a face. Head tracking takes over once he's facing them.
        """
        if side == self._side:
            return
        await self._tracking(False)
        turned = False
        try:
            await self._face_side(side)
            turned = True
        finally:
            self._side = side if turned else None  # after a failed turn, turn again next time
            await self._tracking(True)

    async def _face_side(self, side: str) -> None:
        sign = SIDE_SIGN[side]
        spot = self._spots.get(side)
        if spot is not None:
            await self._turn_body(spot)
            if self.sees_face is None or await self._look():
                log.info("turned to %s (%.0f deg)", side, spot)
                return
            log.info("%s person moved from %.0f deg; looking again", side, spot)
        if self.sees_face is None:
            await self._turn_body(sign * SIDE_DEFAULT_DEG)
            return
        for deg in SIDE_SWEEP_DEG:
            yaw = sign * deg
            await self._turn_body(yaw, duration=0.4)
            if await self._look():
                self._spots[side] = yaw
                log.info("turned to %s: found someone at %.0f deg", side, yaw)
                return
        self._spots.pop(side, None)
        await self._turn_body(sign * SIDE_DEFAULT_DEG)
        log.info("turned to %s: nobody found, waiting at %d deg", side, sign * SIDE_DEFAULT_DEG)

    async def dance(self, name: str | None = None) -> str:
        """A random dance from Pollen's reachy_mini_dances_library (never the same one twice in a row)."""
        from reachy_mini_dances_library import DanceMove
        from reachy_mini_dances_library.collection.dance import AVAILABLE_MOVES

        if name is None:
            choices = [n for n in AVAILABLE_MOVES if n != getattr(self, "_last_dance", None)]
            name = random.choice(choices)
        self._last_dance = name
        move = DanceMove(name)
        reps = max(1, math.ceil(DANCE_MIN_S / move.duration))
        log.info("dance: %s x%d (%.1f s)", name, reps, reps * move.duration)
        await self._tracking(False)
        try:
            for i in range(reps):
                await asyncio.to_thread(self._mini.play_move, move, initial_goto_duration=0.5 if i == 0 else 0.0,
                                        sound=False)
            await asyncio.to_thread(self._goto, head=_pose(), antennas=[0.0, 0.0], body_yaw=0.0,
                                    duration=0.6)
        finally:
            await self._tracking(True)
        return name

    def _goto(self, **kwargs) -> None:
        """goto_target, retried once: the robot software sometimes rejects a move ("time value is out of range")."""
        try:
            self._mini.goto_target(**kwargs)
        except Exception as e:
            log.warning("move rejected (%s); retrying once", e)
            time.sleep(0.1)
            self._mini.goto_target(**kwargs)

    async def point_head(self, yaw_deg: float, pitch_deg: float, duration: float = 0.25) -> None:
        """Aim the head (phone lock). Body stays put; head tracking should already be off."""
        await asyncio.to_thread(self._goto, head=_pose(yaw=yaw_deg, pitch=pitch_deg), duration=duration)

    async def set_tracking(self, enabled: bool) -> None:
        await self._tracking(enabled)

    def forget_sides(self) -> None:
        """New conversation: forget where the two people were standing."""
        self._spots.clear()
        self._side = None

    async def _turn_body(self, yaw_deg: float, duration: float = 0.6) -> None:
        # Head pose is in the room's frame, not the body's: turn it too, or the camera keeps facing forward.
        await asyncio.to_thread(
            self._goto, head=_pose(yaw=yaw_deg), antennas=[0.0, 0.0],
            body_yaw=float(np.deg2rad(yaw_deg)), duration=duration,
        )
        await asyncio.sleep(0.4)  # let him settle and the camera catch up before looking

    async def _look(self) -> bool:
        for _ in range(LOOKS_PER_STOP):
            if await asyncio.to_thread(self.sees_face):
                return True
            await asyncio.sleep(0.1)
        return False

    async def _sequence(self, steps: list[tuple[dict, float]]) -> None:
        await self._tracking(False)
        try:
            for kwargs, duration in steps:
                await asyncio.to_thread(self._goto, duration=duration, **kwargs)
        finally:
            await self._tracking(True)

    async def _tracking(self, enabled: bool) -> None:
        path = "enable" if enabled else "disable"
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                await client.post(f"{self._daemon}/api/media/tracking/{path}")
        except httpx.HTTPError as e:
            log.warning("could not %s head tracking: %s", path, e)
