import json
from types import SimpleNamespace

from greeter.transcript import Line, TranscriptBoard, is_english, label_line, make_transcript_app, side_for


def test_side_is_judged_by_language():
    assert side_for("Japanese", "Japanese", "English") == "left"
    assert side_for("english", "Japanese", "English") == "right"
    assert side_for("French", "Japanese", "English") == "unknown"
    assert side_for("English", None, "English") == "unknown"       # before setup
    assert side_for("English", "English", "English") == "unknown"  # same language: can't tell


def test_is_english():
    assert is_english("English") and is_english("en") and not is_english("Spanish") and not is_english(None)


class _Responses:
    def __init__(self, text=None, exc=None): self.text, self.exc = text, exc
    async def create(self, **kw):
        if self.exc: raise self.exc
        return SimpleNamespace(output_text=self.text)


async def test_label_line_parses_json_and_survives_failures():
    ok = SimpleNamespace(responses=_Responses('```json\n{"language": "Japanese", "english": "Hello"}\n```'))
    assert await label_line(ok, "m", "こんにちは") == ("Japanese", "Hello")
    bad = SimpleNamespace(responses=_Responses(exc=RuntimeError("down")))
    assert await label_line(bad, "m", "hola") == (None, None)


async def test_board_pushes_lines_languages_and_clear():
    board = TranscriptBoard(max_lines=2)
    q = board.subscribe()
    board.set_languages("Japanese", "English")
    for i in range(3):
        board.add(Line(who="left", text=f"t{i}", english=f"e{i}"))
    board.clear()
    msgs = [json.loads(q.get_nowait()) for _ in range(q.qsize())]
    assert [m["type"] for m in msgs] == ["languages", "line", "line", "line", "clear"]
    assert msgs[1]["line"]["text"] == "t0" and board.lines == []


async def test_page_and_live_events(aiohttp_client):
    board = TranscriptBoard()
    board.set_languages("Spanish", "English")
    board.add(Line(who="left", text="¿Dónde está el baño?", english="Where is the bathroom?", language="Spanish"))
    client = await aiohttp_client(make_transcript_app(board))
    page = await client.get("/")
    assert page.status == 200 and "Reachy Translator" in await page.text()
    state = await (await client.get("/state")).json()
    assert state["languages"] == {"left": "Spanish", "right": "English"}
    resp = await client.get("/events")
    first = (await resp.content.readline()).decode()
    snap = json.loads(first.removeprefix("data: "))
    assert snap["type"] == "snapshot" and snap["lines"][0]["english"] == "Where is the bathroom?"
    await resp.content.readline()  # blank separator
    board.add(Line(who="reachy", text="The lady asks…", english="The lady asks…", to="right"))
    live = json.loads((await resp.content.readline()).decode().removeprefix("data: "))
    assert live["type"] == "line" and live["line"]["to"] == "right"
    resp.close()


async def test_page_can_set_languages_and_reset(aiohttp_client):
    calls = []
    async def set_langs(l, r): calls.append(("set", l, r))
    async def reset(): calls.append(("reset",))
    client = await aiohttp_client(make_transcript_app(TranscriptBoard(), set_langs, reset))
    ok = await client.post("/languages", json={"left": "japanese", "right": "English"})
    assert ok.status == 200 and calls[-1] == ("set", "Japanese", "English")
    assert (await client.post("/languages", json={"left": "<script>", "right": "x"})).status == 400
    assert (await client.post("/languages", data="left=Spanish")).status == 415  # no cross-site form posts
    assert (await client.post("/reset", json={})).status == 200 and calls[-1] == ("reset",)
