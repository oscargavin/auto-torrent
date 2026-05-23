"""TDD for recommendation generation + assembly (server/recommend.py).

generate() calls Claude via claude_agent_sdk structured output (mocked here).
build_recommendations() caches per (profile + history), generates, hydrates each
recommendation (dropping misses = hallucination filter), dedupes, and attaches
the LLM's one-line reason. The SDK and the hydrate() network call are mocked.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from auto_torrent.server.recommend import (
    Rec,
    RecCache,
    _history_key,
    build_recommendations,
    card_to_dict,
    generate,
)
from auto_torrent.types import BookCard

R = "auto_torrent.server.recommend"

CARD_PHM = BookCard(
    title="Project Hail Mary",
    author="Andy Weir",
    asin="B08GB2RLKM",
    narrators=("Ray Porter",),
    description="Sci-fi.",
    cover_url="https://m.media-amazon.com/images/I/x._SL500_.jpg",
    genres=("Science Fiction & Fantasy",),
    runtime_min=970,
    year=2021,
    source="audible",
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def fake_query(structured, subtype="success"):
    """Stand in for claude_agent_sdk.query: an async generator yielding one
    duck-typed ResultMessage (only it carries `structured_output`)."""

    async def _q(*_args, **_kwargs):
        yield SimpleNamespace(subtype=subtype, structured_output=structured)

    return _q


# --- generate ------------------------------------------------------------

class TestGenerate:
    @pytest.mark.anyio
    async def test_parses_structured_recommendations(self):
        payload = {
            "recommendations": [
                {"title": "Project Hail Mary", "author": "Andy Weir"},
                {"title": "Recursion", "author": "Blake Crouch"},
            ]
        }
        with patch(f"{R}.query", fake_query(payload)):
            recs = await generate([{"title": "The Martian", "author": "Andy Weir"}])
        assert [r.title for r in recs] == ["Project Hail Mary", "Recursion"]
        assert recs[0].author == "Andy Weir"

    @pytest.mark.anyio
    async def test_error_subtype_returns_empty(self):
        with patch(f"{R}.query", fake_query(None, subtype="error_during_execution")):
            recs = await generate([{"title": "X", "author": "Y"}])
        assert recs == []

    @pytest.mark.anyio
    async def test_skips_system_message_without_structured_output(self):
        """A non-result message (no structured_output attr) must be ignored, not crash."""

        async def _q(*_a, **_k):
            yield SimpleNamespace(subtype="init")  # SystemMessage-like, no structured_output
            yield SimpleNamespace(
                subtype="success",
                structured_output={"recommendations": [{"title": "T", "author": "A"}]},
            )

        with patch(f"{R}.query", _q):
            recs = await generate([{"title": "X", "author": "Y"}])
        assert [r.title for r in recs] == ["T"]


# --- build_recommendations ----------------------------------------------

class TestBuildRecommendations:
    @pytest.mark.anyio
    async def test_drops_hydration_misses_and_includes_description(self):
        recs = [
            Rec(title="Project Hail Mary", author="Andy Weir"),
            Rec(title="Fake Hallucinated Book", author="Nobody"),
        ]

        async def fake_gen(*_a, **_k):
            return recs

        def fake_hydrate(title, _author, _region="uk"):
            return CARD_PHM if title == "Project Hail Mary" else None

        with patch(f"{R}.generate", fake_gen), patch(f"{R}.hydrate", fake_hydrate):
            items = await build_recommendations(
                "p1", [{"title": "The Martian", "author": "Andy Weir"}], cache=None
            )
        assert len(items) == 1
        assert items[0]["title"] == "Project Hail Mary"
        assert items[0]["description"] == "Sci-fi."  # the book's own blurb, not an LLM reason
        assert "reason" not in items[0]
        assert items[0]["narrators"] == ["Ray Porter"]  # tuple → JSON-ready list

    @pytest.mark.anyio
    async def test_preserves_order(self):
        recs = [
            Rec(title="Second", author="A"),
            Rec(title="First", author="B"),
        ]

        async def fake_gen(*_a, **_k):
            return recs

        def fake_hydrate(title, _author, _region="uk"):
            return BookCard(title=title, author="A")

        with patch(f"{R}.generate", fake_gen), patch(f"{R}.hydrate", fake_hydrate):
            items = await build_recommendations("p1", [], cache=None)
        assert [i["title"] for i in items] == ["Second", "First"]

    @pytest.mark.anyio
    async def test_dedupes_same_book(self):
        recs = [
            Rec(title="Project Hail Mary", author="Andy Weir"),
            Rec(title="project hail mary!", author="andy weir"),
        ]

        async def fake_gen(*_a, **_k):
            return recs

        def fake_hydrate(_t, _a, _region="uk"):
            return CARD_PHM

        with patch(f"{R}.generate", fake_gen), patch(f"{R}.hydrate", fake_hydrate):
            items = await build_recommendations("p1", [], cache=None)
        assert len(items) == 1

    @pytest.mark.anyio
    async def test_cache_hit_skips_generation(self, tmp_path):
        cache = RecCache(tmp_path / "rec.json")
        finished = [{"title": "The Martian", "author": "Andy Weir"}]
        cache.set(_history_key("p1", finished, []), [{"title": "Cached", "reason": "x"}])

        called = {"gen": False}

        async def fake_gen(*_a, **_k):
            called["gen"] = True
            return []

        with patch(f"{R}.generate", fake_gen):
            items = await build_recommendations("p1", finished, cache=cache)
        assert called["gen"] is False
        assert items[0]["title"] == "Cached"

    @pytest.mark.anyio
    async def test_refresh_bypasses_and_updates_cache(self, tmp_path):
        cache = RecCache(tmp_path / "rec.json")
        finished = [{"title": "The Martian", "author": "Andy Weir"}]
        key = _history_key("p1", finished, [])
        cache.set(key, [{"title": "Stale", "reason": "x"}])

        async def fake_gen(*_a, **_k):
            return [Rec(title="Project Hail Mary", author="Andy Weir")]

        with patch(f"{R}.generate", fake_gen), patch(f"{R}.hydrate", lambda *a, **k: CARD_PHM):
            items = await build_recommendations("p1", finished, refresh=True, cache=cache)
        assert items[0]["title"] == "Project Hail Mary"
        assert cache.get(key)[0]["title"] == "Project Hail Mary"  # cache rewritten


# --- card_to_dict --------------------------------------------------------

class TestCardToDict:
    def test_serialises_tuples_to_lists(self):
        d = card_to_dict(CARD_PHM)
        assert d["narrators"] == ["Ray Porter"]
        assert d["genres"] == ["Science Fiction & Fantasy"]
        assert d["description"] == "Sci-fi."
        assert d["asin"] == "B08GB2RLKM"
        assert "reason" not in d
        import json

        json.dumps(d)  # must be JSON-serialisable


# --- RecCache ------------------------------------------------------------

class TestRecCache:
    def test_set_get_roundtrip(self, tmp_path):
        c = RecCache(tmp_path / "r.json")
        c.set("k", [{"a": 1}])
        assert c.get("k") == [{"a": 1}]

    def test_missing_key_returns_none(self, tmp_path):
        assert RecCache(tmp_path / "r.json").get("nope") is None

    def test_expired_returns_none(self, tmp_path):
        c = RecCache(tmp_path / "r.json", ttl_s=0)
        c.set("k", [1])
        time.sleep(0.01)
        assert c.get("k") is None

    def test_corrupt_file_is_safe(self, tmp_path):
        p = tmp_path / "r.json"
        p.write_text("{not json")
        assert RecCache(p).get("k") is None


# --- _history_key --------------------------------------------------------

class TestHistoryKey:
    def test_order_independent(self):
        a = _history_key("p", [{"title": "A", "author": "x"}, {"title": "B", "author": "y"}], [])
        b = _history_key("p", [{"title": "B", "author": "y"}, {"title": "A", "author": "x"}], [])
        assert a == b

    def test_changes_when_book_added(self):
        a = _history_key("p", [{"title": "A", "author": "x"}], [])
        b = _history_key("p", [{"title": "A", "author": "x"}, {"title": "B", "author": "y"}], [])
        assert a != b

    def test_profile_scoped(self):
        a = _history_key("p1", [{"title": "A", "author": "x"}], [])
        b = _history_key("p2", [{"title": "A", "author": "x"}], [])
        assert a != b
