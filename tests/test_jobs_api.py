import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from auto_torrent.server.jobs.api import build_router
from auto_torrent.server.jobs.store import JobStore
from auto_torrent.server.jobs.events import EventLog
from auto_torrent.server.jobs.types import CreateJobRequest


@pytest.fixture
def app(redis, monkeypatch):
    # Stub the bearer check so tests don't need the real token.
    from auto_torrent.server.jobs import api as api_mod

    async def _no_auth() -> None:
        return None

    monkeypatch.setattr(api_mod, "_require_bearer", _no_auth)

    log = EventLog(redis)
    store = JobStore(redis, log, state_ttl_s=3600, dedup_ttl_s=600)

    app = FastAPI()
    app.include_router(build_router(store=store, log=log))
    app.state.store = store  # tests reach in to mutate Redis-backed state
    return app


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _create(app, query: str = "dune", profile_id: str = "p1") -> str:
    """Seed a job the way the thread worker does. The API no longer creates them."""
    job, _ = await app.state.store.create(CreateJobRequest(profile_id=profile_id, query=query))
    return job.id


async def test_post_is_gone(client):
    r = await client.post("/chat/jobs", json={"profile_id": "p1", "query": "dune"})
    assert r.status_code == 405


async def test_get_job_returns_state(app, client):
    job_id = await _create(app)

    r = await client.get(f"/chat/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["id"] == job_id


async def test_get_job_404_for_missing(client):
    r = await client.get("/chat/jobs/nope")
    assert r.status_code == 404


async def test_list_filters_by_profile(app, client):
    await _create(app, "a", "p1")
    await _create(app, "b", "p1")
    await _create(app, "c", "p2")

    r = await client.get("/chat/jobs?profile_id=p1")
    assert r.status_code == 200
    queries = [j["query"] for j in r.json()]
    assert sorted(queries) == ["a", "b"]


async def test_sse_streams_events_then_keepalive(app, client, redis):
    # Use the real EventLog to publish a few events, then read via the route.
    from auto_torrent.server.jobs.events import EventLog

    log = EventLog(redis)
    job_id = await _create(app)

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


async def test_delete_kills_subprocess_and_cleans(app, client, monkeypatch, tmp_path):
    """When a job has a download_id registered, DELETE calls the unified
    kill+clean helper which kills the running subprocess and removes the
    partial landing dir + state file."""
    from auto_torrent.server.jobs import api as api_mod

    landing = tmp_path / "downloads" / "Some Book"
    landing.mkdir(parents=True)
    monkeypatch.setattr(
        api_mod, "_read_state",
        lambda did: {"id": did, "pid": 99999, "path": str(landing)},
    )
    killed: list[dict] = []

    async def _fake_kill_and_clean(state: dict) -> None:
        killed.append(state)

    monkeypatch.setattr(api_mod, "_kill_download_and_clean", _fake_kill_and_clean)

    # Seed the job + register a download_id (simulates the worker's
    # post-commit registration step).
    job_id = await _create(app)
    await app.state.store.set_download_id(job_id, "abc12345")

    r = await client.delete(f"/chat/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "status": "cancelled"}

    # kill+clean fired exactly once with the resolved state dict.
    assert len(killed) == 1
    assert killed[0]["id"] == "abc12345"
    assert killed[0]["pid"] == 99999
    assert killed[0]["path"] == str(landing)


async def test_delete_without_download_id_does_not_call_killer(app, client, monkeypatch):
    """A cancel issued before the agent committed has no download_id — must
    NOT call the kill helper (would log noise + race the open state file)."""
    from auto_torrent.server.jobs import api as api_mod

    calls: list[str] = []

    async def _record(did: str) -> None:
        calls.append(did)

    monkeypatch.setattr(api_mod, "_kill_subprocess_and_clean", _record)

    job_id = await _create(app)

    r = await client.delete(f"/chat/jobs/{job_id}")
    assert r.status_code == 200
    assert calls == []  # download_id is None — kill helper never invoked


async def test_delete_marks_cancelled(app, client):
    job_id = await _create(app)

    r = await client.delete(f"/chat/jobs/{job_id}")
    assert r.status_code == 200

    g = await client.get(f"/chat/jobs/{job_id}")
    assert g.json()["status"] == "cancelled"


# --- U1: cancel publishes exactly one frame --------------------------------


async def test_delete_publishes_exactly_one_cancelled_event(app, client, redis):
    """The handler used to publish `cancelled` itself right after
    update_status. Now the store publishes on the transition — a second frame
    from the handler would be dropped by the stream terminator anyway, but the
    duplicate is the bug."""
    job_id = await _create(app)

    await client.delete(f"/chat/jobs/{job_id}")

    entries = await redis.xrange(f"job:{job_id}:events")
    assert [f["type"] for _id, f in entries] == ["cancelled"]


async def test_second_delete_publishes_no_further_event(app, client, redis):
    job_id = await _create(app)

    await client.delete(f"/chat/jobs/{job_id}")
    r2 = await client.delete(f"/chat/jobs/{job_id}")
    assert r2.status_code == 200  # idempotent, not a 404

    entries = await redis.xrange(f"job:{job_id}:events")
    assert [f["type"] for _id, f in entries] == ["cancelled"]


async def test_stream_terminates_on_the_store_published_event(app, client, redis):
    """End-to-end on the contract U1 exists for: a failed job's terminal frame
    reaches a subscriber and closes the stream."""
    job_id = await _create(app)

    from auto_torrent.server.jobs.types import JobStatus

    await app.state.store.update_status(job_id, JobStatus.failed, error="no seeders")

    frames = []
    async with client.stream("GET", f"/chat/jobs/{job_id}/events") as r:
        assert r.status_code == 200
        async for line in r.aiter_lines():
            frames.append(line)
            if line.startswith("data:") and "no seeders" in line:
                break

    assert any(line == "event: error" for line in frames)
