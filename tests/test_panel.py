import asyncio
import sys

from greeter.panel import Launcher, make_panel_app


def launcher(running=False, usb=True):
    async def is_running(): return running
    script = "import time; print('Starting'); print('Press Ctrl+C here to stop Reachy'); time.sleep(5)"
    return Launcher(start_cmd=lambda mode: [sys.executable, "-u", "-c", script],
                    stop_cmd=[sys.executable, "-c", "print('Reachy is stopped.')"],
                    is_running=is_running, usb=lambda: usb)


async def test_start_shows_output_and_transcript_link_then_stop(aiohttp_client):
    l = launcher()
    client = await aiohttp_client(make_panel_app(l))
    assert (await client.post("/start", json={"mode": "translator"})).status == 200
    for _ in range(50):
        s = await (await client.get("/status")).json()
        if s["ready"]:
            break
        await asyncio.sleep(0.05)
    assert s["ready"] and s["mode"] == "translator" and s["transcript_url"].endswith(":8780")
    assert "Starting" in s["output"]
    assert (await client.post("/start", json={"mode": "greeter"})).status == 409  # already running
    r = await (await client.post("/stop", json={})).json()
    assert r["ok"] and "stopped" in r["message"]
    await asyncio.wait_for(l.proc.wait(), 5)  # stop also ends the start script


async def test_refuses_bad_mode_form_posts_and_double_start(aiohttp_client):
    client = await aiohttp_client(make_panel_app(launcher(running=True)))
    assert (await client.post("/start", json={"mode": "party"})).status == 409
    assert (await client.post("/start", data="mode=greeter")).status == 415
    assert (await client.post("/start", json={"mode": "greeter"})).status == 409  # app already running
    page = await client.get("/")
    assert "Start translator" in await page.text()
