# Reachy Mini event robot

Apps for a [Reachy Mini Lite](https://huggingface.co/blog/reachy-mini) desktop robot (Pollen Robotics / Hugging Face) at live events. It runs on a Mac with Reachy plugged in by USB. Voice runs on OpenAI's realtime model, and outfit compliments use an OpenAI vision model.

There are three modes:

| Mode | Start with | What Reachy does |
|---|---|---|
| **Greeter** | `./scripts/start.sh` | Checks guests in from their Luma ticket QR code, welcomes them by first name, gives the odd outfit compliment and answers questions about the event. |
| **Compliment** | `./scripts/start.sh compliment` | Compliments everyone who walks up, with a specific detail ("Love that black jacket!"). |
| **Translator** | `./scripts/start.sh translator` | Relays a conversation between two people on his left and right, each in their own language. A live transcript, always with English, is at http://127.0.0.1:8780. |

**Control panel (in progress).** `./scripts/panel.sh` opens buttons at http://127.0.0.1:8790 to start any mode, stop Reachy, see the startup checks and open the live transcript. Known issue: starting from the panel currently crashes the robot software (a GStreamer plugin error), so start Reachy with `./scripts/start.sh` for now. The panel's Stop button and status work.

Stop any mode with **Ctrl+C** in the start window, or `./scripts/stop.sh` from any terminal. Always stop him before you power Reachy off or unplug him.

## Setup (once)

You need macOS, Python 3.12, `ffmpeg` (`brew install ffmpeg`), a Reachy Mini Lite and an OpenAI API key.

```bash
git clone https://github.com/Light-code11/reachy-mini.git && cd reachy-mini
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then put your OPENAI_API_KEY in .env
./scripts/get_phone_model.sh   # optional: YOLO11n (~11 MB) so Reachy keeps a held-up phone centred at check-in
```

`.env` is git-ignored. Never commit it.

## Run

1. Plug in Reachy's power adapter, switch him on and connect his USB cable to the Mac.
2. Run `./scripts/start.sh [compliment | translator]`.

   The script checks each part in turn and stops with a plain-English fix if something's wrong:
   - USB
   - microphone (if it's silent, unplug the USB for 5 seconds and run the script again)
   - motors
   - camera
   - speaker volume (`REACHY_VOLUME` in `.env`)
   - voice connection

   The first start after a reboot can take a few minutes.
3. Run `./scripts/stop.sh` (or press Ctrl+C) when you're done.

Logs go to `logs/greeter.log` (the app) and `daemon.log` (the robot software).

## Voice commands

| Say | Mode | Effect |
|---|---|---|
| "Reachy, be quiet" / "shut up" / "stop talking" | compliment | He stops talking at once and stays silent until woken. |
| "Reachy, wake up" | compliment | He talks and compliments again. |
| "Reachy, do a dance" / "dance for us" | compliment | He does a random dance from Pollen's [dances library](https://github.com/pollen-robotics/reachy_mini_dances_library) for about 6 seconds. |
| "Rate my fit" | greeter, compliment | He scores the outfit out of 10. |
| "Reachy, new conversation" | translator | He forgets both languages and asks again. |

## Greeter mode: Luma check-in

Greeter mode reads check-ins from Luma's own scanner page through a small Chrome extension, **Reachy Greeter Bridge**. It doesn't use the Luma API, and it never sees your Luma login.

**Full setup and troubleshooting: [docs/chrome-extension.md](docs/chrome-extension.md).** In short:

1. In `chrome://extensions`, turn on **Developer mode**, click **Load unpacked** and pick the repo's `extension/` folder.
2. Run `./scripts/start.sh`, then open `https://luma.com/check-in/<your-event-id>/scan` in Chrome.
3. Choose the **Reachy Mini Camera** and check the header says **"Express Mode On"**.
4. After pulling new code, click the reload arrow on the extension in `chrome://extensions`, then reload the scanner tab.

Set `EVENT_URL` (the public Luma event page) and `EVENT_NAME` in `.env`. Reachy reads the event page so he can answer questions about it. Put extra facts, such as Wi-Fi or where the toilets are, in `data/event_extra.md`.

## Privacy

- Camera frames stay in memory and are never saved.
- Only a crop from the shoulders down goes to the vision model. Other people's faces are blacked out first.
- Voice audio streams to OpenAI's realtime API.
- `logs/` (which holds transcripts and guest names) is git-ignored.

## Development

```bash
.venv/bin/python -m pytest -q          # Python tests
node --test extension/parse.test.mjs    # extension tests
```

| Path | What's there |
|---|---|
| `greeter/main.py` | Entry point and the three modes |
| `greeter/robot.py` | Moves, searching for people, turning, dancing |
| `greeter/voice.py` | OpenAI realtime session |
| `greeter/complimenter.py` | Compliment mode and the quiet/wake/dance commands |
| `greeter/translator.py` | Translator mode |
| `docs/runbook.md` | Event-day checklist and troubleshooting |
| `docs/chrome-extension.md` | Installing and checking the Luma scanner extension |
| `extension/` | The Chrome extension (Reachy Greeter Bridge) |
