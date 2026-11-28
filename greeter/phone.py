"""Check-in mode: spot a phone held up to Reachy (YOLO11n), keep it centred in his camera, and nudge the guest.

Why: small QR codes (Apple Wallet passes) and very bright screens are hard to read. Reachy's camera sees most
detail in the middle of the frame, and a steady, centred phone is far less blurry than one at the edge.
Download the model once with ./scripts/get_phone_model.sh. Without it, this feature quietly stays off.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np

log = logging.getLogger(__name__)

PHONE_CLASS = 67          # COCO "cell phone"
INPUT = 640
MIN_SCORE = 0.35
FX_1080P = 821.5          # camera focal length in pixels at 1920x1080 (Pollen's calibration)
GAIN = 0.6                # move this share of the way to centre each step (smooth, no overshoot)
DEADBAND_DEG = 2.0        # close enough: don't twitch
MAX_YAW, MAX_PITCH = 45.0, 25.0
HINT_AFTER_S = 6.0        # phone held up this long with no scan: give a tip
HINT_EVERY_S = 30.0
RELEASE_AFTER_S = 2.0     # phone gone this long: back to following faces

HINT_LINE = ("Hold your phone a little closer to my camera, and keep it still. "
             "If your screen is really bright, turn the brightness down a touch.")


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    w: float
    h: float
    score: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


def letterbox(frame: np.ndarray, size: int = INPUT) -> tuple[np.ndarray, float, float, float]:
    """Resize keeping aspect ratio and pad to size x size. Returns (NCHW float input, scale, pad_x, pad_y)."""
    import cv2

    h, w = frame.shape[:2]
    scale = min(size / w, size / h)
    nw, nh = round(w * scale), round(h * scale)
    canvas = np.full((size, size, 3), 114, np.uint8)
    px, py = (size - nw) // 2, (size - nh) // 2
    canvas[py:py + nh, px:px + nw] = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
    rgb = canvas[:, :, ::-1].astype(np.float32) / 255.0  # camera frames are BGR
    return np.ascontiguousarray(rgb.transpose(2, 0, 1)[None]), scale, px, py


def phones_from_output(out: np.ndarray, scale: float, px: float, py: float, min_score: float = MIN_SCORE) -> list[Box]:
    """YOLO11 output (1, 84, N): rows 0-3 = cx, cy, w, h in input pixels; rows 4.. = class scores."""
    pred = out[0]
    scores = pred[4 + PHONE_CLASS]
    keep = np.where(scores >= min_score)[0]
    boxes = []
    for i in keep[np.argsort(-scores[keep])]:
        cx, cy, w, h = pred[0, i], pred[1, i], pred[2, i], pred[3, i]
        box = Box((cx - w / 2 - px) / scale, (cy - h / 2 - py) / scale, w / scale, h / scale, float(scores[i]))
        if all(_iou(box, b) < 0.5 for b in boxes):  # simple non-max suppression
            boxes.append(box)
    return boxes


def _iou(a: Box, b: Box) -> float:
    ix = max(0.0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    iy = max(0.0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    inter = ix * iy
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


class PhoneDetector:
    def __init__(self, model_path: Path) -> None:
        import onnxruntime as ort

        self._session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self._input = self._session.get_inputs()[0].name

    def detect(self, frame: np.ndarray) -> list[Box]:
        tensor, scale, px, py = letterbox(frame)
        out = self._session.run(None, {self._input: tensor})[0]
        return phones_from_output(out, scale, px, py)


def aim_step(box: Box, frame_w: int, frame_h: int, yaw: float, pitch: float) -> tuple[float, float] | None:
    """New (yaw, pitch) in degrees that moves the phone toward the middle of the frame, or None if it's centred.

    Reachy's yaw is positive to his left; the camera faces outward, so a phone on the image's right means turn right
    (yaw down). Pitch is positive looking down, so a phone low in the image means pitch up.
    """
    fx = FX_1080P * frame_w / 1920
    dyaw = -math.degrees(math.atan((box.cx - frame_w / 2) / fx))
    dpitch = math.degrees(math.atan((box.cy - frame_h / 2) / fx))
    if abs(dyaw) < DEADBAND_DEG and abs(dpitch) < DEADBAND_DEG:
        return None
    new_yaw = max(-MAX_YAW, min(MAX_YAW, yaw + GAIN * dyaw))
    new_pitch = max(-MAX_PITCH, min(MAX_PITCH, pitch + GAIN * dpitch))
    return new_yaw, new_pitch


class PhoneLock:
    """While a phone is held up: stop face tracking, keep the phone centred, and give a tip if nothing scans."""

    def __init__(
        self,
        detect: Callable[[np.ndarray], list[Box]],
        get_frame: Callable[[], np.ndarray | None],
        point_head: Callable[[float, float], Awaitable[None]],
        set_tracking: Callable[[bool], Awaitable[None]],
        say_hint: Callable[[str], Awaitable[None]],
        last_scan_at: Callable[[], float],
        busy: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic,
        read_head: Callable[[], tuple[float, float]] = lambda: (0.0, 0.0),
    ) -> None:
        self._detect, self._get_frame = detect, get_frame
        self._point_head, self._set_tracking, self._say_hint = point_head, set_tracking, say_hint
        self._last_scan_at, self._busy, self._clock = last_scan_at, busy, clock
        self._read_head = read_head  # (yaw, pitch) in degrees right now, so locking on doesn't jerk the head
        self.locked = False
        self._yaw = self._pitch = 0.0
        self._seen_since: float | None = None
        self._last_seen = -math.inf
        self._last_hint = -math.inf
        self._was_busy = False

    async def step(self) -> None:
        now = self._clock()
        if self._busy():
            self._was_busy = True
            return
        if self._was_busy:
            self._was_busy = False
            if self.locked:  # a welcome/sad move just finished and switched face tracking back on
                await self._set_tracking(False)
        frame = await asyncio.to_thread(self._get_frame)
        phones = await asyncio.to_thread(self._detect, frame) if frame is not None else []
        if phones:
            phone = max(phones, key=lambda b: b.w * b.h)  # the closest one
            self._last_seen = now
            if not self.locked:
                self.locked = True
                self._seen_since = now
                try:
                    self._yaw, self._pitch = await asyncio.to_thread(self._read_head)
                except Exception:
                    self._yaw = self._pitch = 0.0
                await self._set_tracking(False)
                log.info("phone: locked on (score %.2f)", phone.score)
            target = aim_step(phone, frame.shape[1], frame.shape[0], self._yaw, self._pitch)
            if target is not None:
                self._yaw, self._pitch = target
                await self._point_head(*target)
            held = now - self._seen_since if self._seen_since is not None else 0.0
            if held >= HINT_AFTER_S and now - self._last_scan_at() >= HINT_AFTER_S and now - self._last_hint >= HINT_EVERY_S:
                self._last_hint = now
                log.info("phone: held up %.0f s with no scan; giving a tip", held)
                await self._say_hint(HINT_LINE)
        elif self.locked and now - self._last_seen >= RELEASE_AFTER_S:
            self.locked = False
            self._seen_since = None
            await self._set_tracking(True)
            log.info("phone: gone; back to following faces")

    def note_scan(self) -> None:
        """A code was read: restart the tip timer, so a successful guest never hears it."""
        self._seen_since = self._clock()

    async def run(self, every_s: float = 0.2) -> None:
        while True:
            try:
                await self.step()
            except Exception:
                log.exception("phone lock step failed")
            await asyncio.sleep(every_s)


def head_yaw_pitch(mini: Any) -> tuple[float, float]:
    """Reachy's current head yaw and pitch in degrees (same convention as create_head_pose)."""
    from reachy_mini.utils.rotation import Rotation as R

    _, pitch, yaw = R.from_matrix(np.asarray(mini.get_current_head_pose())[:3, :3]).as_euler("xyz", degrees=True)
    return float(yaw), float(pitch)


def load_detector(path: Path) -> PhoneDetector | None:
    if not path.exists():
        log.info("phone lock off: %s missing (run ./scripts/get_phone_model.sh)", path)
        return None
    try:
        return PhoneDetector(path)
    except Exception:
        log.exception("phone lock off: couldn't load %s", path)
        return None
