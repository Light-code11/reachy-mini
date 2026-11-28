from pathlib import Path

import numpy as np
import pytest

from greeter.phone import HINT_LINE, PHONE_CLASS, Box, PhoneLock, aim_step, letterbox, load_detector, phones_from_output

MODEL = Path(__file__).resolve().parent.parent / "models" / "yolo11n.onnx"


def fake_output(boxes, n=50):
    """YOLO-shaped (1, 84, n) array with the given (cx, cy, w, h, phone_score) in input pixels."""
    out = np.zeros((1, 84, n), np.float32)
    for i, (cx, cy, w, h, s) in enumerate(boxes):
        out[0, :4, i] = (cx, cy, w, h)
        out[0, 4 + PHONE_CLASS, i] = s
    return out


def test_letterbox_and_box_mapping_round_trip():
    frame = np.zeros((1080, 1920, 3), np.uint8)
    tensor, scale, px, py = letterbox(frame)
    assert tensor.shape == (1, 3, 640, 640) and scale == pytest.approx(1 / 3) and px == 0 and py == 140
    # a phone at frame (960, 540) of size 300x600 -> input (320, 320) of size 100x200
    [box] = phones_from_output(fake_output([(320, 320, 100, 200, 0.9)]), scale, px, py)
    assert (box.cx, box.cy, box.w, box.h) == pytest.approx((960, 540, 300, 600))


def test_low_scores_and_duplicates_are_dropped():
    out = fake_output([(320, 320, 100, 200, 0.9), (322, 321, 100, 200, 0.8), (100, 100, 50, 50, 0.2)])
    boxes = phones_from_output(out, 1.0, 0, 0)
    assert len(boxes) == 1 and boxes[0].score == pytest.approx(0.9)


def test_aim_turns_toward_the_phone():
    right_low = Box(1500, 700, 100, 200, 0.9)  # image right and below centre
    yaw, pitch = aim_step(right_low, 1920, 1080, 0.0, 0.0)
    assert yaw < 0 and pitch > 0  # turn right (negative yaw), look down (positive pitch)
    assert aim_step(Box(940, 520, 40, 40, 0.9), 1920, 1080, 5.0, 3.0) is None  # already centred
    yaw, _ = aim_step(Box(1900, 540, 10, 10, 0.9), 1920, 1080, -44.0, 0.0)
    assert yaw == -45.0  # clamped


class Rig:
    def __init__(self):
        self.t = 0.0; self.phones = []; self.calls = []; self.last_scan = -1e9
    def lock(self):
        async def point(y, p): self.calls.append(("point", round(y, 1), round(p, 1)))
        async def track(on): self.calls.append(("track", on))
        async def hint(text): self.calls.append(("hint", text))
        return PhoneLock(lambda f: self.phones, lambda: np.zeros((1080, 1920, 3), np.uint8), point, track, hint,
                         lambda: self.last_scan, clock=lambda: self.t, read_head=lambda: (10.0, 0.0))


async def test_lock_follows_phone_hints_once_then_releases():
    rig = Rig(); lock = rig.lock()
    rig.phones = [Box(1400, 500, 120, 240, 0.8)]
    await lock.step()
    assert rig.calls[0] == ("track", False) and rig.calls[1][0] == "point"
    assert rig.calls[1][1] < 10.0  # started from the real head yaw (10) and turned right
    for t in (2.0, 4.0, 6.5, 8.0):
        rig.t = t; await lock.step()
    hints = [c for c in rig.calls if c[0] == "hint"]
    assert hints == [("hint", HINT_LINE)]  # once, after 6 s with no scan
    rig.phones = []; rig.t = 9.0; await lock.step()
    assert ("track", True) not in rig.calls  # brief dropout: stay locked
    rig.t = 10.5; await lock.step()
    assert rig.calls[-1] == ("track", True) and not lock.locked


async def test_no_hint_when_the_code_scans():
    rig = Rig(); lock = rig.lock()
    rig.phones = [Box(900, 500, 120, 240, 0.8)]
    await lock.step()
    rig.t = 3.0; rig.last_scan = 3.0; lock.note_scan(); await lock.step()
    rig.t = 7.0; await lock.step()
    assert not [c for c in rig.calls if c[0] == "hint"]


@pytest.mark.skipif(not MODEL.exists(), reason="run ./scripts/get_phone_model.sh")
def test_real_model_runs_and_sees_no_phone_in_a_blank_frame():
    det = load_detector(MODEL)
    assert det is not None and det.detect(np.full((1080, 1920, 3), 128, np.uint8)) == []


async def test_turns_tracking_back_off_after_a_move_while_locked():
    rig = Rig()
    busy = {"on": False}
    async def point(y, p): rig.calls.append("point")
    async def track(on): rig.calls.append(("track", on))
    async def hint(t): pass
    lock = PhoneLock(lambda f: rig.phones, lambda: np.zeros((1080, 1920, 3), np.uint8), point, track, hint,
                     lambda: rig.last_scan, busy=lambda: busy["on"], clock=lambda: rig.t)
    rig.phones = [Box(1400, 500, 120, 240, 0.8)]
    await lock.step()
    busy["on"] = True; rig.t = 1.0; await lock.step()   # welcome wiggle running: hands off
    busy["on"] = False; rig.t = 2.0; await lock.step()
    assert rig.calls.count(("track", False)) == 2
