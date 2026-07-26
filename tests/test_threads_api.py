import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from auto_torrent.server.jobs.events import EventLog
from auto_torrent.server.threads.api import build_router
from auto_torrent.server.threads.store import ThreadStore
from auto_torrent.server.threads.types import Message, MessageKind, ThreadStatus


@pytest.fixture
def app(redis, monkeypatch):
    from auto_torrent.server.threads import api as api_mod

    async def _no_auth() -> None:
        return None

    monkeypatch.setattr(api_mod, "_require_bearer", _no_auth)

    log = EventLog(redis, prefix="thread")
    store = ThreadStore(redis, log, state_ttl_s=3600)
    turns: list[tuple[str, str, list[dict] | None]] = []

    async def fake_enqueue(thread_id: str, text: str, pending: list[dict] | None) -> None:
        turns.append((thread_id, text, pending))

    app = FastAPI()
    app.include_router(build_router(threads=store, log=log, enqueue_turn=fake_enqueue))
    app.state.turns = turns
    app.state.store = store
    return app


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _new_thread(client) -> str:
    r = await client.post("/chat/threads", json={"profile_id": "p1"})
    assert r.status_code == 201
    return r.json()["id"]


async def test_post_message_appends_and_enqueues(app, client):
    tid = await _new_thread(client)
    r = await client.post(f"/chat/threads/{tid}/messages", json={"text": "dune"})
    assert r.status_code == 200
    assert r.json()["kind"] == "user"
    assert app.state.turns == [(tid, "dune", None)]


async def test_post_message_rejects_empty(client):
    tid = await _new_thread(client)
    r = await client.post(f"/chat/threads/{tid}/messages", json={"text": "   "})
    assert r.status_code == 422


async def test_post_message_conflicts_while_working(app, client):
    """Two turns at once would give the agent a stale history and answer the
    wrong question."""
    tid = await _new_thread(client)
    await app.state.store.set_status(tid, ThreadStatus.working)
    r = await client.post(f"/chat/threads/{tid}/messages", json={"text": "dune"})
    assert r.status_code == 409


async def test_typing_supersedes_an_open_question(app, client):
    tid = await _new_thread(client)
    await app.state.store.set_pending(tid, [{"title": "A", "magnet": "m"}])
    await app.state.store.set_status(tid, ThreadStatus.awaiting_choice)
    r = await client.post(f"/chat/threads/{tid}/messages", json={"text": "no, foundation"})
    assert r.status_code == 200
    assert await app.state.store.take_pending(tid) == []


async def test_get_thread_returns_transcript(client):
    tid = await _new_thread(client)
    await client.post(f"/chat/threads/{tid}/messages", json={"text": "dune"})
    r = await client.get(f"/chat/threads/{tid}")
    assert r.status_code == 200
    body = r.json()
    assert body["thread"]["id"] == tid
    assert [m["text"] for m in body["messages"]] == ["dune"]


async def test_get_missing_thread_404s(client):
    assert (await client.get("/chat/threads/nope")).status_code == 404


async def _open_choice(app, tid: str) -> Message:
    from auto_torrent.server.threads.store import option_from_payload

    await app.state.store.set_pending(
        tid,
        [
            {"title": "Dune", "magnet": "magnet:?xt=a", "narrator": "Simon Vance"},
            {"title": "Dune", "magnet": "magnet:?xt=b", "narrator": "Scott Brick"},
        ],
    )
    msg = await app.state.store.append(
        tid,
        Message.new(
            tid,
            MessageKind.choice,
            options=[
                option_from_payload(0, {"title": "Dune", "narrator": "Simon Vance"}),
                option_from_payload(1, {"title": "Dune", "narrator": "Scott Brick"}),
            ],
        ),
    )
    await app.state.store.set_status(tid, ThreadStatus.awaiting_choice)
    return msg


async def test_choose_resolves_echoes_and_enqueues_only_the_pick(app, client):
    tid = await _new_thread(client)
    msg = await _open_choice(app, tid)

    r = await client.post(
        f"/chat/threads/{tid}/choose", json={"message_id": msg.id, "option_index": 1}
    )
    assert r.status_code == 200
    assert r.json()["chosen_index"] == 1

    # The transcript reads as a conversation: the tap becomes a user line.
    texts = [m["text"] for m in (await client.get(f"/chat/threads/{tid}")).json()["messages"]]
    assert any("Dune" in t for t in texts)

    # Only the chosen option goes back — handing the agent the whole list again
    # would let it re-decide something the user already decided.
    assert len(app.state.turns) == 1
    _tid, _text, pending = app.state.turns[0]
    assert pending is not None and len(pending) == 1
    assert pending[0]["magnet"] == "magnet:?xt=b"


async def test_double_tap_does_not_start_two_downloads(app, client):
    tid = await _new_thread(client)
    msg = await _open_choice(app, tid)
    a = await client.post(
        f"/chat/threads/{tid}/choose", json={"message_id": msg.id, "option_index": 0}
    )
    b = await client.post(
        f"/chat/threads/{tid}/choose", json={"message_id": msg.id, "option_index": 0}
    )
    assert a.status_code == 200
    assert b.status_code == 409
    assert len(app.state.turns) == 1


async def test_choose_unknown_index_409s(app, client):
    tid = await _new_thread(client)
    msg = await _open_choice(app, tid)
    r = await client.post(
        f"/chat/threads/{tid}/choose", json={"message_id": msg.id, "option_index": 9}
    )
    assert r.status_code == 409
    assert app.state.turns == []


async def test_choose_after_options_expired_410s(app, client):
    tid = await _new_thread(client)
    msg = await _open_choice(app, tid)
    await app.state.store.clear_pending(tid)  # 30-min TTL lapsed
    r = await client.post(
        f"/chat/threads/{tid}/choose", json={"message_id": msg.id, "option_index": 0}
    )
    assert r.status_code == 410
    assert app.state.turns == []


async def test_list_threads_is_profile_scoped(client):
    a = await _new_thread(client)
    r = await client.post("/chat/threads", json={"profile_id": "p2"})
    other = r.json()["id"]
    ids = [t["id"] for t in (await client.get("/chat/threads?profile_id=p1")).json()]
    assert a in ids and other not in ids


async def _open_book_choice(app, tid: str) -> Message:
    """A "which book?" question — suggestions the agent named without searching,
    so no magnets."""
    from auto_torrent.server.threads.store import option_from_payload

    await app.state.store.set_pending(
        tid,
        [
            {"title": "The Way of Kings", "author": "Brandon Sanderson"},
            {"title": "The Lies of Locke Lamora", "author": "Scott Lynch"},
        ],
    )
    msg = await app.state.store.append(
        tid,
        Message.new(
            tid,
            MessageKind.choice,
            text="A few that scratch the same itch — which?",
            options=[
                option_from_payload(0, {"title": "The Way of Kings", "author": "Brandon Sanderson"}),
                option_from_payload(1, {"title": "The Lies of Locke Lamora", "author": "Scott Lynch"}),
            ],
        ),
    )
    await app.state.store.set_status(tid, ThreadStatus.awaiting_choice)
    return msg


async def test_choosing_a_suggested_book_starts_a_fresh_search(app, client):
    """There is no magnet to commit — the agent named books, not torrents — so
    the next turn has to search for the one they picked."""
    tid = await _new_thread(client)
    msg = await _open_book_choice(app, tid)

    r = await client.post(
        f"/chat/threads/{tid}/choose", json={"message_id": msg.id, "option_index": 1}
    )
    assert r.status_code == 200

    _tid, text, pending = app.state.turns[0]
    assert pending is None  # fresh search, not a commit
    assert "Lies of Locke Lamora" in text
    assert "Scott Lynch" in text


async def test_choosing_an_edition_commits_without_researching(app, client):
    """The opposite case: the magnet is known, so the turn should commit it
    rather than search again and risk landing on a different copy."""
    tid = await _new_thread(client)
    msg = await _open_choice(app, tid)

    r = await client.post(
        f"/chat/threads/{tid}/choose", json={"message_id": msg.id, "option_index": 0}
    )
    assert r.status_code == 200

    _tid, _text, pending = app.state.turns[0]
    assert pending is not None and pending[0]["magnet"] == "magnet:?xt=a"
