import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from auto_torrent.server.jobs.api import build_router
from auto_torrent.server.jobs.store import JobStore
from auto_torrent.server.jobs.events import EventLog


@pytest.fixture
def app(redis, monkeypatch):
    # Stub the bearer check so tests don't need the real token.
    from auto_torrent.server.jobs import api as api_mod

    async def _no_auth() -> None:
        return None

    monkeypatch.setattr(api_mod, "_require_bearer", _no_auth)

    store = JobStore(redis, state_ttl_s=3600, dedup_ttl_s=600)
    log = EventLog(redis)
    enqueued: list[str] = []

    async def fake_enqueue(job_id: str) -> None:
        enqueued.append(job_id)

    app = FastAPI()
    app.include_router(build_router(store=store, log=log, enqueue=fake_enqueue))
    app.state.enqueued = enqueued
    return app


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_post_creates_job_and_enqueues(app, client):
    r = await client.post("/chat/jobs", json={"profile_id": "p1", "query": "dune"})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending"
    assert app.state.enqueued == [body["id"]]


async def test_post_is_idempotent(app, client):
    a = await client.post("/chat/jobs", json={"profile_id": "p1", "query": "dune"})
    b = await client.post("/chat/jobs", json={"profile_id": "p1", "query": "DUNE  "})
    assert a.json()["id"] == b.json()["id"]
    # Second request returns 200 (not 201) and does NOT re-enqueue.
    assert b.status_code == 200
    assert app.state.enqueued == [a.json()["id"]]


async def test_post_rejects_empty_query(client):
    r = await client.post("/chat/jobs", json={"profile_id": "p1", "query": "   "})
    assert r.status_code == 422


async def test_get_job_returns_state(client):
    create = await client.post("/chat/jobs", json={"profile_id": "p1", "query": "dune"})
    job_id = create.json()["id"]

    r = await client.get(f"/chat/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["id"] == job_id


async def test_get_job_404_for_missing(client):
    r = await client.get("/chat/jobs/nope")
    assert r.status_code == 404


async def test_list_filters_by_profile(client):
    await client.post("/chat/jobs", json={"profile_id": "p1", "query": "a"})
    await client.post("/chat/jobs", json={"profile_id": "p1", "query": "b"})
    await client.post("/chat/jobs", json={"profile_id": "p2", "query": "c"})

    r = await client.get("/chat/jobs?profile_id=p1")
    assert r.status_code == 200
    queries = [j["query"] for j in r.json()]
    assert sorted(queries) == ["a", "b"]


async def test_sse_streams_events_then_keepalive(app, client, redis):
    # Use the real EventLog to publish a few events, then read via the route.
    from auto_torrent.server.jobs.events import EventLog

    log = EventLog(redis)
    create = await client.post("/chat/jobs", json={"profile_id": "p1", "query": "dune"})
    job_id = create.json()["id"]

    await log.publish(job_id, "progress", {"text": "hello"})
    await log.publish(job_id, "completed", {"title": "Dune"})

    async with client.stream("GET", f"/chat/jobs/{job_id}/events") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        seen_types: list[str] = []
        async for line in r.aiter_lines():
            if line.startswith("event: "):
                seen_types.append(line[len("event: "):])
            if "completed" in seen_types:
                break
        assert "progress" in seen_types and "completed" in seen_types
