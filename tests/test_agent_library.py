"""The agent can see what the family owns, narrators included."""

from types import SimpleNamespace

from auto_torrent.server import agent as agent_module
from auto_torrent.server import audiobookshelf, vision

SETTINGS = SimpleNamespace(abs_library_id="lib", abs_url="http://abs", abs_api_token="t")


def _item(id, title, narrator="", series="", hours=10):
    return {
        "id": id,
        "media": {
            "duration": hours * 3600,
            "metadata": {
                "title": title,
                "authorName": "Robin Hobb",
                "narratorName": narrator,
                "seriesName": series,
            },
        },
    }


SHELF = [
    _item("a1", "The Farseer Trilogy 01 - Assassin's Apprentice", "Paul Boehmer"),
    _item("t1", "The Tawny Man Trilogy 01 - Fool's Errand", "James Langton"),
    _item("x1", "Some Other Book", "Paul Boehmer"),
]


async def test_library_search_returns_the_narrator_and_matches_on_it(monkeypatch):
    """Observed live: with only title and author, the agent told the user the
    library did not record who narrates their Farseer books."""

    async def list_items(self, library_id):
        return SHELF

    monkeypatch.setattr(audiobookshelf.ABSClient, "list_items", list_items)

    farseer = await agent_module._search_library(SETTINGS, "farseer")
    assert farseer[0]["narrator"] == "Paul Boehmer"
    assert farseer[0]["id"] == "a1"
    by_narrator = await agent_module._search_library(SETTINGS, "boehmer")
    assert [r["id"] for r in by_narrator] == ["a1", "x1"]


async def test_inspect_reads_the_cover_only_when_no_narrator_is_recorded(monkeypatch):
    covers: list = []

    async def get_item(self, item_id):
        return _item(item_id, "Farseer Trilogy Omnibus", narrator="" if item_id == "o1" else "Paul Boehmer")

    async def get_cover(self, item_id):
        covers.append(item_id)
        return b"img", "image/jpeg"

    async def read(data, media_type):
        return {"narrator": "Paul Boehmer"}

    monkeypatch.setattr(audiobookshelf.ABSClient, "get_item", get_item)
    monkeypatch.setattr(audiobookshelf.ABSClient, "get_cover", get_cover)
    monkeypatch.setattr(vision, "read_cover_image", read)

    omnibus = await agent_module._inspect_book(SETTINGS, "o1")
    assert omnibus["cover_says"] == {"narrator": "Paul Boehmer"}
    known = await agent_module._inspect_book(SETTINGS, "a1")
    assert "cover_says" not in known
    assert covers == ["o1"]
