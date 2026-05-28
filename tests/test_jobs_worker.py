from unittest.mock import AsyncMock, patch

import pytest

from auto_torrent.server.jobs.events import EventLog
from auto_torrent.server.jobs.store import JobStore
from auto_torrent.server.jobs.types import CreateJobRequest, JobStatus
from auto_torrent.server.jobs.worker import run_chat_job


@pytest.fixture
def store(redis):
    return JobStore(redis, state_ttl_s=3600, dedup_ttl_s=600)


@pytest.fixture
def log(redis):
    return EventLog(redis)


async def test_run_chat_job_marks_running_then_succeeded(redis, store, log, monkeypatch):
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))

    # Stub out the heavy bits: agent loop + download poll.
    fake_agent = AsyncMock(return_value=type("O", (), {
        "kind": "committed",
        "download": {"id": "dl1"},
        "fallbacks": [],
        "display": "“Dune”",
        "title": "Dune",
        "author": "Frank Herbert",
        "message": None,
    })())
    monkeypatch.setattr("auto_torrent.server.jobs.worker.run_agent", fake_agent)
    monkeypatch.setattr(
        "auto_torrent.server.jobs.worker._emit_download_and_poll",
        AsyncMock(),
    )

    await run_chat_job({"redis": redis, "store": store, "log": log}, job.id)

    refreshed = await store.get(job.id)
    assert refreshed.status == JobStatus.succeeded
    assert refreshed.picked_title == "Dune"
    assert refreshed.picked_author == "Frank Herbert"


async def test_run_chat_job_marks_failed_on_exception(redis, store, log, monkeypatch):
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    monkeypatch.setattr(
        "auto_torrent.server.jobs.worker.run_agent",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    await run_chat_job({"redis": redis, "store": store, "log": log}, job.id)

    refreshed = await store.get(job.id)
    assert refreshed.status == JobStatus.failed
    assert "boom" in refreshed.error
