# Chrome extension: Reachy Greeter Bridge

Greeter mode needs this extension. It runs on Luma's own check-in scanner page and tells Reachy each scan result (checked in, already checked in, not found), so he can greet the guest by first name.

- It doesn't use the Luma API, and it never reads or sends your Luma login.
- It only talks to Reachy's app on this Mac, at `http://127.0.0.1:8765`.
- It only runs on `https://luma.com/check-in/*` pages.

## Install (once per Mac)

1. Clone this repo to the Mac that runs Reachy. The extension is the `extension/` folder.
2. In Chrome, go to `chrome://extensions`.
3. Turn on **Developer mode** (top right).
4. Click **Load unpacked** and choose the repo's `extension/` folder (the one with `manifest.json` in it).
5. **Reachy Greeter Bridge** now appears in the list. Make sure its switch is on.
6. Optional: click the puzzle-piece icon in Chrome's toolbar and pin it, so you can see at a glance that it's loaded.

## Before each event

1. Start Reachy first: `./scripts/start.sh`. Wait for `Reachy is ready for: …`.
2. Open the event's scanner in Chrome:
   `https://luma.com/check-in/<event-id>/scan`
   Find the event ID (`evt-…`) in the event's Luma link. You need to be signed in to Luma as a host or check-in manager for that event.
3. When Chrome asks for camera access, allow it. In the scanner's camera menu, choose **Reachy Mini Camera**.
4. Check the scanner's header says **"Express Mode On"**. The extension picks Express in the Scan Options popup by itself. If the header doesn't say it, choose Express yourself. Without Express, Luma waits for a host to confirm each scan, and Reachy can't welcome anyone.
5. Do a test scan with any ticket for the event. Reachy should say "Welcome, <name>!", or "Welcome back" if that ticket is already checked in.

## After updating the code

Chrome keeps running the old extension code until you reload it:

1. In `chrome://extensions`, click the reload arrow (↻) on **Reachy Greeter Bridge**.
2. Reload the scanner tab.

## Check it's working

`logs/scanner-debug.log` gets a line for each step:

```
bridge loaded on /check-in/evt-…/scan              extension is running on the scanner
camera pipeline on: … camera 3840x2160, Luma sees 1920x1080   it's using Reachy's camera in 4K
read ticket via full view / 4K center view          which view read the code
auto-selected Express mode                         Express is on
EXPRESS http=200 status="success" … -> success     a guest was checked in
```

`curl -s localhost:8765/health` should return `{"ok": true}` while Reachy's app is running.

## Troubleshooting

| Problem | Fix |
|---|---|
| Luma checks people in but Reachy says nothing | Make sure Reachy's app is running (the health check above). Then reload the extension and the scanner tab. |
| Reachy says "the Luma scanner isn't in Express mode" | The scanner tab was reloaded and lost Express. Choose Express in the Scan Options popup. |
| No `bridge loaded` line in the log | The extension is off, or it was loaded from the wrong folder. Check `chrome://extensions`: the folder must be the one with `manifest.json` in it. |
| Scanning feels slow | Reload the extension (it may be running old code). Ask the guest to open the ticket, hold the phone still about 15–20 cm from his camera, and make sure the screen isn't dimmed (but not at full brightness either). |
| Camera is black or busy | Close other apps using the camera, then reload the scanner tab. |
| Small codes (Apple Wallet passes) won't scan | The extension asks the camera for 4K and checks the middle of the frame at full detail. Check the log says `camera 3840x2160`. If it says `4K camera not available`, it's running at normal resolution. In check-in mode Reachy also turns his head to keep a held-up phone in the middle (needs `./scripts/get_phone_model.sh` once). Ask the guest to hold the phone 15–20 cm from his camera, or open the ticket on the Luma page, where the code is bigger. |
| Bright light-mode screens won't scan | Ask the guest to turn their screen brightness down a little. Reachy suggests this himself if a phone is held up for 6 seconds with nothing scanning. |
| Want to turn 4K off | In the scanner tab's DevTools console, run `localStorage.reachyHiRes = "0"` and reload. |
| Codes far away won't scan | In the scanner tab's DevTools console, run `localStorage.reachyZoom = "2"` and reload. This zooms the camera 2×. Set it back to `"1"` to turn zoom off. |
