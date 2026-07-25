"""Cover art for books ABS couldn't match itself.

Measured on the live library: 4 of 30 items had no artwork. Three resolved
straight away via Audible; the fourth ("Kitchen Confidential - Adventures in
the Culinary Underbelly" by "Anthony Bourdain/Anthony Bourdain") needed the
title and author cleaning up first, which is what most of this module is.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_torrent.server import covers


class TestCleanAuthor:
    def test_collapses_a_duplicated_author(self):
        # ABS concatenates duplicate author tags with "/". This exact value is
        # from the live library and matched nothing.
        assert covers.clean_author("Anthony Bourdain/Anthony Bourdain") == "Anthony Bourdain"

    def test_is_case_insensitive_about_the_duplicate(self):
        assert covers.clean_author("Neil Gaiman/neil gaiman") == "Neil Gaiman"

    def test_keeps_genuine_co_authors(self):
        assert covers.clean_author("Neil Gaiman/Terry Pratchett") == "Neil Gaiman, Terry Pratchett"

    @pytest.mark.parametrize("value", ["", "   ", "//"])
    def test_empty_stays_empty(self, value):
        assert covers.clean_author(value) == ""

    def test_leaves_a_plain_author_alone(self):
        assert covers.clean_author("Walter Isaacson") == "Walter Isaacson"


class TestTitleVariants:
    def test_offers_the_title_without_its_subtitle(self):
        # The full shelf title finds nothing; the bare title finds it at once.
        assert covers.title_variants(
            "Kitchen Confidential - Adventures in the Culinary Underbelly"
        ) == [
            "Kitchen Confidential - Adventures in the Culinary Underbelly",
            "Kitchen Confidential",
        ]

    @pytest.mark.parametrize("sep", [" - ", " – ", " — ", " : "])
    def test_recognises_each_subtitle_separator(self, sep):
        assert covers.title_variants(f"Book{sep}Subtitle")[-1] == "Book"

    def test_a_title_with_no_subtitle_yields_one_attempt(self):
        assert covers.title_variants("Steve Jobs") == ["Steve Jobs"]

    def test_does_not_split_a_hyphenated_word(self):
        # "Spider-Man" is one word; only a spaced separator marks a subtitle.
        assert covers.title_variants("Spider-Man") == ["Spider-Man"]

    def test_empty_title_yields_nothing_to_try(self):
        assert covers.title_variants("  ") == []


class TestFindCoverUrl:
    def test_returns_the_first_cover_found(self):
        with patch.object(covers, "hydrate", lambda t, a: _card("http://img/1.jpg")):
            assert covers.find_cover_url("Steve Jobs", "Walter Isaacson") == "http://img/1.jpg"

    def test_falls_back_to_the_shorter_title(self):
        tried = []

        def fake(title, author):
            tried.append(title)
            return _card("http://img/2.jpg") if title == "Kitchen Confidential" else _card(None)

        with patch.object(covers, "hydrate", fake):
            url = covers.find_cover_url("Kitchen Confidential - Adventures", "Anthony Bourdain")

        assert url == "http://img/2.jpg"
        assert tried == ["Kitchen Confidential - Adventures", "Kitchen Confidential"]

    def test_passes_the_cleaned_author_through(self):
        seen = {}

        def fake(title, author):
            seen["author"] = author
            return _card("http://img/3.jpg")

        with patch.object(covers, "hydrate", fake):
            covers.find_cover_url("Kitchen Confidential", "Anthony Bourdain/Anthony Bourdain")

        assert seen["author"] == "Anthony Bourdain"

    def test_returns_none_when_nothing_has_a_cover(self):
        with patch.object(covers, "hydrate", lambda t, a: _card(None)):
            assert covers.find_cover_url("Obscure", "Nobody") is None

    def test_a_lookup_raising_does_not_propagate(self):
        # Artwork must never be able to break the caller — this runs inside an
        # import that has already downloaded gigabytes.
        def boom(title, author):
            raise RuntimeError("provider down")

        with patch.object(covers, "hydrate", boom):
            assert covers.find_cover_url("Steve Jobs", "Walter Isaacson") is None

    def test_a_failure_on_the_first_variant_still_tries_the_second(self):
        def fake(title, author):
            if "-" in title:
                raise RuntimeError("boom")
            return _card("http://img/4.jpg")

        with patch.object(covers, "hydrate", fake):
            assert covers.find_cover_url("A - B", "Someone") == "http://img/4.jpg"


class TestCoverlessItems:
    def test_selects_only_items_with_no_cover_path(self):
        items = [
            {"id": "1", "media": {"coverPath": "/metadata/1/cover.jpg"}},
            {"id": "2", "media": {"coverPath": None}},
            {"id": "3", "media": {}},
            {"id": "4"},
        ]
        assert [i["id"] for i in covers.coverless_items(items)] == ["2", "3", "4"]

    def test_reads_title_and_author_from_the_item(self):
        item = {"media": {"metadata": {"title": "Steve Jobs", "authorName": "Walter Isaacson"}}}
        assert covers.item_title_author(item) == ("Steve Jobs", "Walter Isaacson")

    def test_missing_metadata_yields_empty_strings_not_none(self):
        assert covers.item_title_author({}) == ("", "")


class TestEnsureLocalCover:
    def test_skips_a_folder_that_already_has_artwork(self, tmp_path: Path):
        (tmp_path / "cover.png").write_bytes(b"x" * 2000)
        called = []
        with patch.object(covers, "find_cover_url", lambda *a: called.append(1)):
            assert covers.ensure_local_cover(tmp_path, "T", "A") is None
        # The download shipped its own art; don't go looking or overwrite it.
        assert called == []

    def test_writes_cover_jpg_when_there_is_none(self, tmp_path: Path):
        with (
            patch.object(covers, "find_cover_url", lambda *a: "http://img/c.jpg"),
            patch.object(covers.requests, "get", lambda *a, **k: _Resp(b"y" * 5000)),
        ):
            result = covers.ensure_local_cover(tmp_path, "Steve Jobs", "Walter Isaacson")
        assert result == tmp_path / "cover.jpg"
        assert (tmp_path / "cover.jpg").read_bytes() == b"y" * 5000

    def test_rejects_a_response_too_small_to_be_artwork(self, tmp_path: Path):
        # An error page or a 1px placeholder would otherwise become the cover.
        with (
            patch.object(covers, "find_cover_url", lambda *a: "http://img/c.jpg"),
            patch.object(covers.requests, "get", lambda *a, **k: _Resp(b"nope")),
        ):
            assert covers.ensure_local_cover(tmp_path, "T", "A") is None
        assert not (tmp_path / "cover.jpg").exists()

    def test_no_cover_found_writes_nothing(self, tmp_path: Path):
        with patch.object(covers, "find_cover_url", lambda *a: None):
            assert covers.ensure_local_cover(tmp_path, "T", "A") is None
        assert list(tmp_path.iterdir()) == []


class TestBackfill:
    @pytest.mark.anyio
    async def test_sets_a_cover_for_each_item_missing_one(self):
        client = _FakeABS(
            [
                {"id": "1", "media": {"coverPath": "/has/one.jpg"}},
                {"id": "2", "media": {"metadata": {"title": "Steve Jobs", "authorName": "W I"}}},
            ]
        )
        with patch.object(covers, "find_cover_url", lambda t, a: "http://img/x.jpg"):
            report = await covers.backfill_covers(client, "lib1")

        assert client.set == [("2", "http://img/x.jpg")]
        assert report["scanned"] == 2 and report["missing"] == 1
        assert report["fixed"] == ["Steve Jobs"]

    @pytest.mark.anyio
    async def test_dry_run_changes_nothing(self):
        client = _FakeABS([{"id": "2", "media": {"metadata": {"title": "Steve Jobs"}}}])
        with patch.object(covers, "find_cover_url", lambda t, a: "http://img/x.jpg"):
            report = await covers.backfill_covers(client, "lib1", dry_run=True)
        assert client.set == []
        assert report["fixed"] == ["Steve Jobs"] and report["dry_run"] is True

    @pytest.mark.anyio
    async def test_reports_what_it_could_not_resolve(self):
        client = _FakeABS([{"id": "2", "media": {"metadata": {"title": "Obscure"}}}])
        with patch.object(covers, "find_cover_url", lambda t, a: None):
            report = await covers.backfill_covers(client, "lib1")
        assert report["unresolved"] == ["Obscure"] and report["fixed"] == []

    @pytest.mark.anyio
    async def test_one_item_failing_does_not_stop_the_rest(self):
        client = _FakeABS(
            [
                {"id": "bad", "media": {"metadata": {"title": "Bad"}}},
                {"id": "good", "media": {"metadata": {"title": "Good"}}},
            ],
            fail_on="bad",
        )
        with patch.object(covers, "find_cover_url", lambda t, a: "http://img/x.jpg"):
            report = await covers.backfill_covers(client, "lib1")
        assert report["fixed"] == ["Good"]
        assert report["unresolved"] == ["Bad"]


# --- helpers ----------------------------------------------------------------


class _card:
    def __init__(self, cover_url):
        self.cover_url = cover_url


class _Resp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


class _FakeABS:
    def __init__(self, items, fail_on=None):
        self.items = items
        self.set: list[tuple[str, str]] = []
        self.fail_on = fail_on

    async def list_items(self, _library_id, limit=500):
        return self.items

    async def set_cover(self, item_id, url):
        if item_id == self.fail_on:
            raise RuntimeError("ABS rejected it")
        self.set.append((item_id, url))
