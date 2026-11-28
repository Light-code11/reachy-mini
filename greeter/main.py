"""Reachy greeter: python -m greeter.main"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from pathlib import Path

import cv2
import httpx
from openai import AsyncOpenAI
from reachy_mini import ReachyMini
from reachy_mini.vision.face_detector import FaceDetector

from greeter.arrival import ArrivalPrompter
from greeter.checkin import CheckinGate, ScanResult
from greeter.complimenter import DANCE_TOOL, INSTRUCTIONS as COMPLIMENTER_INSTRUCTIONS, WAKE_LINE, Complimenter
from greeter.compliment import crop_below_face, rate_outfit, suggest_compliment
from greeter.config import Settings, load_settings
from greeter.event_info import build_instructions, fetch_event_text
from greeter.greeter import Greeter
from greeter.phone import PhoneLock, head_yaw_pitch, load_detector
from greeter.robot import Moves
from greeter.server import make_app, start_server
from greeter.speech import SpeechQueue
from greeter.tools import TOOLS, make_tool_handler, rate_fit
from greeter.transcript import Line, TranscriptBoard, is_english, label_line, make_transcript_app, side_for
from greeter.translator import SETUP_PROMPT, TRANSLATOR_TOOLS, TranslatorState, build_translator_instructions, make_translator_handler
from greeter.voice import RealtimeVoice

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("greeter")


def load_dotenv(path: Path) -> dict[str, str]:
    env = dict(os.environ)
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


async def face_loop(settings: Settings, greeter: Greeter, presence: dict) -> None:
    url = f"{settings.daemon_url}/api/media/tracking/face"
    async with httpx.AsyncClient(timeout=1.0) as client:
        while True:
            try:
                data = (await client.get(url)).json()
                present = bool(data.get("face_target", {}).get("detected"))
            except (httpx.HTTPError, ValueError):
                present = False
            presence["face"] = present
            await greeter.handle_face(present, time.monotonic())
            await asyncio.sleep(0.2)


async def scan_loop(queue: asyncio.Queue[ScanResult], greeter: Greeter, on_scan=None) -> None:
    while True:
        result = await queue.get()
        if on_scan:
            on_scan()
        await greeter.handle_scan(result)


async def run_translator(settings: Settings) -> None:
    """Translator mode: no check-ins or compliments; relay between the people on Reachy's left and right."""
    with ReachyMini() as mini:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(f"{settings.daemon_url}/api/media/tracking/enable")
        detector = FaceDetector()

        def sees_face_ahead() -> bool:
            """A face in the middle 80% of the frame, so the other person at the very edge doesn't count."""
            frame = mini.media.get_frame()  # in memory only, never saved
            if frame is None:
                log.info("look: no camera frame")
                return False
            width = frame.shape[1]
            faces = detector.detect(frame)
            log.info("look: %d face(s) at x=%s of %dx%d", len(faces),
                     [int(f.bbox[0] + f.bbox[2] / 2) for f in faces], width, frame.shape[0])
            return any(0.1 * width < f.bbox[0] + f.bbox[2] / 2 < 0.9 * width for f in faces)

        moves = Moves(mini, settings.daemon_url, sees_face=sees_face_ahead)
        state = TranslatorState()
        voice_ref: list[RealtimeVoice] = []

        async def act_sad() -> None:
            await moves.sad()

        board = TranscriptBoard()
        openai_client = AsyncOpenAI(api_key=settings.openai_api_key)

        def update_instructions(text: str) -> None:
            voice_ref[0].update_instructions(text)
            if (state.left, state.right) != (board.languages["left"], board.languages["right"]):
                if not state.left and not state.right:
                    board.clear()  # "new conversation"
                board.set_languages(state.left, state.right)

        handler = make_translator_handler(state, moves, act_sad, update_instructions)
        voice = RealtimeVoice(settings, mini, build_translator_instructions(state), TRANSLATOR_TOOLS, handler,
                              half_duplex=True, max_output_tokens=4096,  # audio counts too
                              noise_reduction="near_field", vad_threshold=0.7)  # ignore the room, hear the two people
        voice_ref.append(voice)

        in_order = asyncio.Lock()  # lines appear in the order they were said (waiters are first come, first served)

        async def show(text: str, reachy: bool) -> None:
            text = text.strip()
            if len(text.strip(" .,!?。、")) < 2:
                return  # noise like "Hm."
            to = moves._side if reachy else None  # he turns to the listener before speaking
            async with in_order:
                await _show(text, reachy, to)

        async def _show(text: str, reachy: bool, to: str | None) -> None:
            language, english = await label_line(openai_client, settings.compliment_model, text)
            if is_english(language):
                english = text
            who = "reachy" if reachy else side_for(language, state.left, state.right)
            board.add(Line(who=who, text=text, english=english, language=language, to=to))

        voice.on_transcript = lambda text: asyncio.get_running_loop().create_task(show(text, reachy=False))
        voice.on_reply_transcript = lambda text: asyncio.get_running_loop().create_task(show(text, reachy=True))
        async def set_languages_from_page(left: str | None, right: str | None) -> None:
            state.left, state.right = left, right
            log.info("translator: languages set on the page: left %s, right %s", left, right)
            update_instructions(build_translator_instructions(state))
            if state.ready:
                await voice.prompt(
                    f"The host just set the languages on screen: left person {left}, right person {right}. "
                    "Briefly tell each person, in their own language, that you'll translate for them now, then carry on."
                )

        async def reset_from_page() -> None:
            state.left = state.right = None
            moves.forget_sides()
            log.info("translator: reset from the page")
            update_instructions(build_translator_instructions(state))
            await voice.prompt(SETUP_PROMPT)

        page = await start_server(make_transcript_app(board, set_languages_from_page, reset_from_page),
                                  settings.transcript_port)
        log.info("transcript page on http://127.0.0.1:%d", settings.transcript_port)

        async def start_setup() -> None:
            for _ in range(50):  # the camera takes a few seconds to start; don't sweep blind
                if await asyncio.to_thread(mini.media.get_frame) is not None:
                    break
                await asyncio.sleep(0.2)
            if not state.ready:  # after a reconnect with languages known, just carry on translating
                await voice.prompt(SETUP_PROMPT)

        voice.on_connected = start_setup
        voice.set_listening(True)
        log.info("translator ready")
        try:
            await voice.run()
        finally:
            await page.cleanup()
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    await client.post(f"{settings.daemon_url}/api/media/tracking/disable")
            except httpx.HTTPError:
                log.warning("could not disable head tracking on exit")


def make_compliment_fn(mini, detector, openai_client, settings: Settings):
    async def get_compliment(avoid: list[str] | None = None) -> str | None:
        frame = mini.media.get_frame()  # in memory only, never saved
        if frame is None:
            log.info("compliment: no camera frame")
            return None
        crop = crop_below_face(frame, await asyncio.to_thread(detector.detect, frame))
        if crop is None:
            log.info("compliment: no face found or too little clothing in view; nothing sent")
            return None
        ok, jpeg = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return await suggest_compliment(openai_client, settings.compliment_model, jpeg.tobytes(), avoid=avoid) if ok else None
    return get_compliment


async def run_complimenter(settings: Settings) -> None:
    """Compliment mode: compliment everyone who walks up; 'be quiet' silences him until 'wake up'."""
    with ReachyMini() as mini:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(f"{settings.daemon_url}/api/media/tracking/enable")
        detector = FaceDetector()
        openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
        moves = Moves(mini, settings.daemon_url)
        comp_ref: list[Complimenter] = []

        async def rate_current_fit() -> dict:
            return await rate_fit(mini.media.get_frame, detector.detect,
                                  lambda jpeg: rate_outfit(openai_client, settings.compliment_model, jpeg))

        async def act_sad() -> None:
            await comp_ref[0].act_sad()

        tools = [t for t in TOOLS if t["name"] in ("rate_outfit", "act_sad")] + [DANCE_TOOL]
        base_handler = make_tool_handler(rate_current_fit, act_sad)

        async def handle_tool(name: str, args: dict) -> dict:
            if name == "dance":
                started = comp_ref[0].start_dance("voice model asked")
                return {"ok": True, "say": "You're dancing now! One short, excited line." if started
                        else "You're already dancing. Keep it short."}
            return await base_handler(name, args)

        voice = RealtimeVoice(settings, mini, COMPLIMENTER_INSTRUCTIONS, tools, handle_tool,
                              noise_reduction="near_field", vad_threshold=0.7)  # a busy room: only hear people close to him
        comp = Complimenter(None, moves, make_compliment_fn(mini, detector, openai_client, settings))
        speech = SpeechQueue(lambda text: comp.say(text, voice.say_exact))
        comp.speech = speech  # the queue speaks through comp.say, so it is made after comp
        comp_ref.append(comp)

        def heard(transcript: str) -> None:
            command = comp.hear(transcript)
            if command == "quiet":
                voice.set_muted(True)
            elif command == "wake":
                voice.set_muted(False)
                asyncio.get_running_loop().create_task(speech.put(WAKE_LINE))

        voice.on_transcript = heard
        voice.set_listening(True)

        async def faces() -> None:
            last_log = 0.0
            while True:
                frame = await asyncio.to_thread(mini.media.get_frame)  # in memory only, never saved
                found = await asyncio.to_thread(detector.detect, frame) if frame is not None else []
                now = time.monotonic()
                if now - last_log >= 10.0:
                    log.info("faces: %s", "no camera frame" if frame is None else len(found))
                    last_log = now
                await comp.handle_face(bool(found), now, can_prompt=not voice.speaking())  # room chatter doesn't block
                await asyncio.sleep(0.25)

        log.info("complimenter ready")
        try:
            await asyncio.gather(voice.run(), speech.run(), faces())
        finally:
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    await client.post(f"{settings.daemon_url}/api/media/tracking/disable")
            except httpx.HTTPError:
                log.warning("could not disable head tracking on exit")


async def amain() -> None:
    settings = load_settings(load_dotenv(ROOT / ".env"))
    if settings.mode == "translator":
        await run_translator(settings)
        return
    if settings.mode == "compliment":
        await run_complimenter(settings)
        return
    event_text = await fetch_event_text(settings.event_url, ROOT / "data" / "event_info.md")
    extra = (ROOT / "data" / "event_extra.md")
    instructions = build_instructions(event_text, extra.read_text() if extra.exists() else "", settings.event_name)

    with ReachyMini() as mini:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(f"{settings.daemon_url}/api/media/tracking/enable")

        detector = FaceDetector()
        openai_client = AsyncOpenAI(api_key=settings.openai_api_key)

        async def get_compliment() -> str | None:
            frame = mini.media.get_frame()  # in memory only, never saved
            if frame is None:
                log.info("compliment: no camera frame")
                return None
            crop = crop_below_face(frame, await asyncio.to_thread(detector.detect, frame))
            if crop is None:
                log.info("compliment: no face found or too little clothing in view; nothing sent")
                return None
            ok, jpeg = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 80])
            return await suggest_compliment(openai_client, settings.compliment_model, jpeg.tobytes()) if ok else None

        async def rate_current_fit() -> dict:
            out = await rate_fit(
                mini.media.get_frame,  # in memory only, never saved
                detector.detect,
                lambda jpeg: rate_outfit(openai_client, settings.compliment_model, jpeg),
            )
            log.info("rate_outfit -> %s", "rating" if "rating" in out else out["say"][:40])
            return out

        greeter_ref: list[Greeter] = []

        async def act_sad() -> None:
            await greeter_ref[0].act_sad()

        voice = RealtimeVoice(settings, mini, instructions, TOOLS, make_tool_handler(rate_current_fit, act_sad, lambda: look_at_me()))
        speech = SpeechQueue(voice.say_exact)
        moves = Moves(mini, settings.daemon_url)
        greeter = Greeter(speech, moves, CheckinGate(), ArrivalPrompter(), get_compliment,
                          event_name=settings.event_name)
        greeter_ref.append(greeter)

        def sees_face() -> bool:
            frame = mini.media.get_frame()  # in memory only, never saved
            return frame is not None and bool(detector.detect(frame))

        async def look_at_me() -> bool:
            return await greeter.find_person(sees_face, time.monotonic())

        presence = {"face": False}

        # A phone held up (YOLO): stop following faces and keep the phone centred, so small or bright codes read.
        phone_detector = load_detector(ROOT / "models" / "yolo11n.onnx")
        phone_lock = None
        if phone_detector is not None:
            phone_lock = PhoneLock(
                phone_detector.detect,
                mini.media.get_frame,  # in memory only, never saved
                moves.point_head,
                moves.set_tracking,
                lambda text: speech.put(text, max_age_s=8.0),
                lambda: greeter.last_scan_at,
                busy=lambda: greeter.moving,
                read_head=lambda: head_yaw_pitch(mini),
            )
            log.info("phone lock on")

        def heard_someone() -> None:
            if phone_lock is not None and phone_lock.locked:
                return  # someone's holding up their phone: don't turn away to look for a voice
            if not presence["face"] and not greeter.moving:  # a voice, but nobody in view: go find them
                asyncio.get_running_loop().create_task(greeter.find_person(sees_face, time.monotonic(), auto=True))

        voice.set_listening(True)  # always listen, even when nobody is in view
        voice.on_guest_speech = heard_someone
        greeter.conversation_active = lambda: voice.active_within(10.0)

        scans: asyncio.Queue[ScanResult] = asyncio.Queue()
        runner = await start_server(make_app(scans, ROOT / "logs" / "scanner-debug.log"), settings.server_port)
        log.info("greeter ready on http://127.0.0.1:%d", settings.server_port)
        try:
            await asyncio.gather(
                voice.run(), speech.run(), face_loop(settings, greeter, presence),
                scan_loop(scans, greeter, on_scan=phone_lock.note_scan if phone_lock else None),
                *([phone_lock.run()] if phone_lock else []),
            )
        finally:
            await runner.cleanup()
            # Leave the robot still: stop the daemon's head tracking we turned on at startup.
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    await client.post(f"{settings.daemon_url}/api/media/tracking/disable")
            except httpx.HTTPError:
                log.warning("could not disable head tracking on exit")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for noisy in ("httpx", "zeroconf", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Treat `pkill` (SIGTERM) like Ctrl+C so cleanup runs and head tracking is switched off.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("greeter stopped")


if __name__ == "__main__":
    main()
