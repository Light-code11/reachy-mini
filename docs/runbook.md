# Reachy greeter — event-day runbook

Event: Codex Community Meetup | Melbourne (Reachy says "OpenAI Dev Day"), Sat 21 Nov 2026, doors 10:00.
**Switching events:** set `EVENT_URL` (the public Luma event link) and `EVENT_NAME` (spoken as "MLAI welcomes you to the {EVENT_NAME}!") in `~/Projects/reachy-mini/.env`, open that event's scanner (`https://luma.com/check-in/<evt-id>/scan`), and restart the app. With neither set, Reachy uses the Dev Day event.

Everything runs on the host MacBook with Reachy plugged in by USB **and** its own power adapter.

## Control panel (buttons instead of commands)

**Known issue: don't use the panel's Start buttons yet.** Starting from the panel crashes the robot software (a GStreamer plugin error). Start with `./scripts/start.sh` as usual; the panel's Stop button and status are fine.

Run `cd ~/Projects/reachy-mini && ./scripts/panel.sh` once and leave the window open. The panel opens at http://127.0.0.1:8790. It shows whether Reachy's USB is connected and whether he's running. It has a Start button for each mode (check-in, compliments, translator), a Stop button, the startup checks as they happen, and a link to the live transcript in translator mode. Closing the panel doesn't stop Reachy; use its Stop button or `./scripts/stop.sh`.

## Quick start (does steps 2, 3 and 5 below, and checks each one)

```
cd ~/Projects/reachy-mini && ./scripts/start.sh
```
It checks the USB, mic, motors and camera, sets the volume (`REACHY_VOLUME` in `.env`, default 60), and starts Reachy's app. It stops with a clear message if anything's wrong. Leave the window open. **Ctrl+C** in it stops everything. From any other terminal, `./scripts/stop.sh` does the same. Run one of them before you power Reachy off or unplug him. Then do step 4 (scanner tab) and step 6 (test scan).

## Setup (allow 15 minutes, finish by 9:30)

1. **Power on Reachy.** Plug in the power adapter, switch it on, and connect the USB cable to the Mac. Bring a **phone hotspot** in case the venue Wi-Fi fails.
2. **Start the daemon** in a terminal and leave it open:
   ```
   cd ~/Projects/reachy-mini && .venv/bin/reachy-mini-daemon -p /dev/cu.usbmodem5AF71339921 --headless
   ```
   The first start after a reboot can take **2–4 minutes**. Check it with
   `curl -s localhost:8000/api/daemon/status`, which should show `"state":"running"`.
   If you see "No motors detected", the power is off. Switch it on and restart the daemon.
3. **Set the volume:**
   `curl -X POST localhost:8000/api/volume/set -H 'Content-Type: application/json' -d '{"volume":60}'`
4. **Open the scanner in Chrome:** `https://luma.com/check-in/evt-ZLcgMdLTqHF8bK3/scan`
   - Camera: **Reachy Mini Camera**.
   - Check that **Reachy Greeter Bridge** is enabled in `chrome://extensions`.
   - If the code changed since the last event, click the reload arrow on Reachy Greeter Bridge in `chrome://extensions` first, so the scanner uses the latest version.
   - The extension picks **Express** in the Scan Options popup by itself. The header must say **"Express Mode On"**. If it doesn't, choose Express yourself.
5. **Start Reachy's app** in a second terminal:
   ```
   cd ~/Projects/reachy-mini && .venv/bin/python -m greeter.main
   ```
   It's ready when you see `realtime connected (gpt-realtime)`.
6. **Test scan:** scan the test guest **Michael**. You should hear "Welcome, Michael!…". Then **undo Michael's check-in** in the guest list.
7. Place the "hold your QR here" marker about an arm's length in front of Reachy.

## During the event

- **Reachy says "the Luma scanner isn't in Express mode":** the scanner tab was reloaded. Choose Express in the popup.
- **A ticket won't scan:** ask the guest to turn their phone brightness up, open the **ticket** (with their name under the QR code), not the event page, and hold it still at about arm's length. Otherwise, check them in from the guest list.
- **Stop Reachy:** press Ctrl+C in the app terminal, or run `pkill -f greeter.main`. This also stops the head following faces.
- **Restart Reachy:** run the step 5 command again.

## Compliment mode

Reachy compliments each new person who walks up about a second after he sees them, and gives a fresh, different compliment every 15 seconds while anyone stays in view. He names something specific (for example "Love that black shirt!") and avoids repeating his last 10 lines. There are no check-ins and no QR scanning in this mode.

1. **Start:** `./scripts/start.sh compliment`. Stop with `./scripts/stop.sh` (or Ctrl+C) as usual.
2. **Silence him:** say "Reachy, be quiet" (or "shut up", "stop talking", "hush"). He stops mid-sentence and says nothing at all, not even replies, until you say **"Reachy, wake up"**. He keeps following faces while he's quiet.
3. **Dance:** say "Reachy, do a dance" (or "dance for us"). He picks a random dance from Pollen's dances library (never the same one twice in a row) and dances for about 6 seconds. This works in quiet mode too, since dancing is silent.
4. People can still chat with him, ask him to rate their fit, and he droops if someone's mean.
5. `logs/greeter.log` shows `quiet mode ON` / `quiet mode OFF` when he heard you.

## Translator mode

Reachy can also work as a polite interpreter between two people who don't share a language. None of the greeter features run in this mode: no check-ins, compliments or fit ratings, and no scanner tab needed.

1. **Start:** `./scripts/start.sh translator`. Stop with `./scripts/stop.sh` (or Ctrl+C) as usual. Plain `./scripts/start.sh` is the normal greeter again.
   **Live transcript:** open http://127.0.0.1:8780 on the Mac (or put it on a screen). It shows each person's words on their side, always with an English line underneath, plus what Reachy relayed. Languages show in the header. It clears on "new conversation".
2. **Stand** one person on Reachy's **left**, the other on his **right**, both about an arm's length away.
3. **Setup:** he turns left and asks that person which language they want, repeats it back ("Spanish, is that right?") and waits for a yes. Then he does the same on the right. If both choose the same language, he tells them they can talk directly and stays quiet unless spoken to.
4. **Talk normally, one at a time.** After each person speaks, he turns to the other person and relays it in their language, for example "The gentleman is asking you…". Pause after each sentence or two so he can keep up.
5. **New people:** say "Reachy, new conversation" and he runs setup again.

Notes:
- He can't be interrupted mid-sentence in this mode. His mic is off while he's talking, so he doesn't translate himself. Wait for him to finish.
- He filters out distant voices, so speak clearly from about an arm's length. In a very loud room, step closer.
- He decides "gentleman" or "lady" from the voice, and says "your guest" when unsure.
- The first time he turns to each side, he sweeps outward (30° up to 120°) until he sees a face and remembers that spot. After that he goes straight back to it, and looks again if the person has moved. If he finds nobody he waits at 90°, so stand a little closer or more in front of him.
- `logs/greeter.log` shows `translator: left speaks …` lines once setup has worked.

## Troubleshooting

| Symptom | Check |
|---|---|
| Luma checks people in but Reachy says nothing | Is the app running? `curl -s localhost:8765/health` should return `{"ok": true}`. Is the extension enabled? Reload it in `chrome://extensions`, then reload the scanner tab. |
| No voice at all | Check `OPENAI_API_KEY` in `~/Projects/reachy-mini/.env`, look for `realtime connected` in the app terminal, and check the volume (step 3). |
| Head stuck or not moving, "Motor communication error" | Check the power. Then restart the daemon (step 2) and the app (step 5). |
| Reachy only talks when someone scans: he ignores people walking up, won't chat, and gives no compliments | The daemon didn't connect to the camera when it started. Check with `grep -c "cannot reach the camera" ~/Projects/reachy-mini/daemon.log`: any number above 0 means the camera feed is missing. Also, at startup the app log shows `Using WebRTC backend` instead of `Using LOCAL backend`. Fix: stop the app, restart the daemon (step 2), start the app (step 5), then reload the scanner tab and check Express Mode is on. |
| Reachy checks people in but ignores chat and "rate my fit", even while he's looking at you | His mic may have gone silent. In `logs/greeter.log`, every 15 s there's a line like `mic: ... peak 0.000`: a peak of 0.000 means the mic is sending pure silence (normal speech gives about 0.1 or more). Fix: stop the app, **unplug Reachy's USB cable for 5 seconds and plug it back in**, then restart the daemon (step 2), set the volume (step 3), start the app (step 5), and reload the scanner tab. |
| Scanning feels slow, or Reachy turns away while a guest holds up their QR | The extension may be running old code. Open `chrome://extensions`, click the reload arrow on **Reachy Greeter Bridge**, then reload the scanner tab (do this after every update). It checks the camera for a code about 20 times a second. Reachy also won't turn to look for a voice within 8 s of seeing a face (a raised phone often hides it) or 15 s of a scan. If he still turns away, ask the guest to stay quiet for a second while scanning, or use "look at me" only after the scan. Also check the usual: phone brightness up, ticket open, held still at arm's length. |
| Head keeps following faces after stopping | `curl -X POST localhost:8000/api/media/tracking/disable` |
| Camera busy or black in Chrome | Close other apps using the camera and reload the scanner tab. Chrome and the daemon can share the camera. |

## What Reachy says

| Situation | Line |
|---|---|
| New check-in | "Welcome, {First}! MLAI welcomes you to the OpenAI Dev Day!" followed by a compliment: a unique detail if he can spot one, otherwise the colour or style ("That black shirt looks great on you!"), or "And can I just say, you're looking great today!" if he can't see the outfit |
| Re-scan 10–60 s after the welcome | "You're all checked in, {First}! Take a seat whenever you're ready." |
| Already checked in (later) | "Welcome back, {First}! You've already checked in, so take a seat whenever you're ready." |
| Event-page QR instead of the ticket | "Ooh, that's the event link! Open your ticket instead, the one with your name under the QR code." |
| Any other failure | "Hmm, I couldn't check you in. Please find a host at the front desk and they'll sort you out!" |
| Someone waiting without scanning (max once a minute) | "Hi! If you haven't checked in yet, just show me the QR code on your Luma ticket." |
