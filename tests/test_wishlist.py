import pytest

from auto_torrent.server import wishlist
from auto_torrent.server.jobs.events import EventLog
from auto_torrent.server.threads.store import ThreadStore
from auto_torrent.server.threads.types import MessageKind, ThreadStatus
from auto_torrent.types import SearchResult


def wish(title, author=""):
    return {"title": title, "author": author}


class TestMatches:
    @pytest.mark.parametrize("title,author,post", [
        ("Songbird", "Peter Grainger", "Songbird (Kings Lake Investigation #1 - Peter Grainger"),
        ("songbird", "", "Songbird (Kings Lake Investigation #1 - Peter Grainger"),
        ("Songbird Kings Lake", "", "Songbird (Kings Lake Investigation #1 - Peter Grainger"),
        ("The Late Lord Thorpe", "Peter Grainger", "The Late Lord Thorpe - DC Smith series bk11 - Peter Grainger"),
        ("Late Lord Thorpe", "Grainger", "The Late Lord Thorpe - DC Smith series bk11 - Peter Grainger"),
        ("Fool's Errand", "Robin Hobb", "Fool’s Errand (Tawny Man, #1) (2013 Audible Release) - Robin"),
        ("fools errand", "", "Fool’s Errand (Tawny Man, #1) (2013 Audible Release) - Robin"),
        ("Les Misérables", "Victor Hugo", "Les Miserables - Victor Hugo"),
        ("Dune", "Frank Herbert", "Dune: Book One - Frank Herbert"),
    ])
    def test_finds_the_book(self, title, author, post):
        assert wishlist.matches(wish(title, author), post)

    @pytest.mark.parametrize("title,author,post", [
        # The mistake this exists to prevent.
        ("Some sort of justice", "", "Ancillary Justice (Imperial Radch #1) - Ann Leckie"),
        # A word of the post's own title is missing from the wish.
        ("Justice", "", "Ancillary Justice (Imperial Radch #1) - Ann Leckie"),
        ("Dune", "Frank Herbert", "Dune Messiah - Frank Herbert"),
        ("Songbird", "", "The Songbird's Call - Jane Doe"),
        # A word of the wish is missing from the post.
        ("Dune Messiah", "", "Dune - Frank Herbert"),
        ("Fool's Errand", "", "Fool’s Fate (Tawny Man, #3) - Robin Hobb"),
        # Same title, different author.
        ("Songbird", "Peter Grainger", "Songbird - Colleen Hoover"),
        ("Fool's Errand", "Robin Hobb", "Fool’s Errand - Jane"),
        # Words are whole words, not prefixes.
        ("Song", "", "Songbird (Kings Lake Investigation #1 - Peter Grainger"),
        ("", "", "Songbird - Peter Grainger"),
    ])
    def test_ignores_a_different_book(self, title, author, post):
        assert not wishlist.matches(wish(title, author), post)

    def test_post_without_an_author_skips_the_author_check(self):
        assert wishlist.matches(wish("Songbird", "Peter Grainger"), "Songbird")


class TestStore:
    async def test_add_list_remove(self, redis):
        added = await wishlist.add(redis, "p1", " Songbird ", "Peter Grainger", "the first one")
        assert added["title"] == "Songbird"
        assert await wishlist.list_for(redis, "p1") == [added]
        assert await wishlist.list_for(redis, "p2") == []

        assert await wishlist.remove(redis, "p1", added["id"]) is True
        assert await wishlist.remove(redis, "p1", added["id"]) is False
        assert await wishlist.list_for(redis, "p1") == []

    async def test_keeps_wishes_for_months(self, redis):
        await wishlist.add(redis, "p1", "Songbird")
        assert await redis.ttl("wishlist:p1") > 170 * 24 * 3600


def post(title):
    return SearchResult(title=title, link=f"https://audiobookbay.lu/abss/{abs(hash(title))}/")


SONGBIRD = post("Songbird (Kings Lake Investigation #1 - Peter Grainger")
OTHER = post("Ancillary Justice (Imperial Radch #1) - Ann Leckie")


class TestCheckNewPosts:
    @pytest.fixture
    def threads(self, redis):
        return ThreadStore(redis, EventLog(redis, prefix="thread"), state_ttl_s=3600)

    @pytest.fixture
    def enqueued(self):
        return []

    @pytest.fixture
    def enqueue(self, enqueued):
        async def enqueue_turn(thread_id, text, pending):
            enqueued.append((thread_id, text, pending))
        return enqueue_turn

    async def test_a_match_starts_a_turn_on_the_latest_thread(self, redis, threads, enqueue, enqueued):
        await threads.create("p1")
        latest = await threads.create("p1")
        w = await wishlist.add(redis, "p1", "Songbird", "Peter Grainger")

        assert await wishlist.check_new_posts(redis, threads, enqueue, [OTHER, SONGBIRD]) == 1

        [(thread_id, text, pending)] = enqueued
        assert thread_id == latest.id and pending is None
        assert text.startswith("(watchlist) Songbird by Peter Grainger just appeared")
        assert SONGBIRD.title in text
        assert (await threads.get(latest.id)).status is ThreadStatus.working
        [note] = await threads.messages(latest.id)
        assert note.kind is MessageKind.assistant and SONGBIRD.title in note.text
        assert await wishlist.list_for(redis, "p1") == []
        assert w["id"]

    async def test_no_match_keeps_the_wish(self, redis, threads, enqueue, enqueued):
        await threads.create("p1")
        await wishlist.add(redis, "p1", "Some sort of justice")
        assert await wishlist.check_new_posts(redis, threads, enqueue, [OTHER]) == 0
        assert enqueued == []
        assert len(await wishlist.list_for(redis, "p1")) == 1

    async def test_no_thread_keeps_the_wish(self, redis, threads, enqueue, enqueued):
        await wishlist.add(redis, "p1", "Songbird")
        assert await wishlist.check_new_posts(redis, threads, enqueue, [SONGBIRD]) == 0
        assert len(await wishlist.list_for(redis, "p1")) == 1

    async def test_busy_thread_keeps_the_wish_for_tomorrow(self, redis, threads, enqueue, enqueued):
        thread = await threads.create("p1")
        await threads.set_status(thread.id, ThreadStatus.working)
        await wishlist.add(redis, "p1", "Songbird")
        assert await wishlist.check_new_posts(redis, threads, enqueue, [SONGBIRD]) == 0
        assert enqueued == []
        assert len(await wishlist.list_for(redis, "p1")) == 1

    async def test_an_open_question_is_superseded(self, redis, threads, enqueue, enqueued):
        thread = await threads.create("p1")
        await threads.set_pending(thread.id, [{"magnet": "x"}])
        await threads.set_status(thread.id, ThreadStatus.awaiting_choice)
        await wishlist.add(redis, "p1", "Songbird")
        assert await wishlist.check_new_posts(redis, threads, enqueue, [SONGBIRD]) == 1
        assert await threads.take_pending(thread.id) == []

    async def test_a_failure_keeps_the_wish_and_does_not_raise(self, redis, threads, enqueued):
        await threads.create("p1")
        await wishlist.add(redis, "p1", "Songbird")

        async def broken(thread_id, text, pending):
            raise ConnectionError("redis went away")

        assert await wishlist.check_new_posts(redis, threads, broken, [SONGBIRD]) == 0
        assert len(await wishlist.list_for(redis, "p1")) == 1

    async def test_checks_every_profile(self, redis, threads, enqueue, enqueued):
        a = await threads.create("a")
        b = await threads.create("b")
        await wishlist.add(redis, "a", "Songbird")
        await wishlist.add(redis, "b", "Ancillary Justice", "Ann Leckie")
        assert await wishlist.check_new_posts(redis, threads, enqueue, [SONGBIRD, OTHER]) == 2
        assert {t for t, _, _ in enqueued} == {a.id, b.id}
