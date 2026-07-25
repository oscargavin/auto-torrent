import json
from unittest.mock import AsyncMock

import pytest

from auto_torrent.server.jobs.events import EventLog
from auto_torrent.server.jobs.store import JobStore
from auto_torrent.server.jobs.types import CreateJobRequest, JobStatus
from auto_torrent.server.jobs.worker import run_chat_job


async def _events(redis, job_id: str) -> list[tuple[str, dict]]:
    entries = await redis.xrange(f"job:{job_id}:events")
    return [(fields["type"], json.loads(fields["data"])) for _id, fields in entries]


def _outcome(**kwargs):
    """An AgentOutcome-shaped stub. Defaults to the graceful no-results shape:
    kind='error' with a message, which is what run_agent returns when the agent
    told the user it couldn't find the book and stopped."""
    fields = {
        "kind": "error",
        "download": None,
        "fallbacks": [],
        "display": "",
        "title": "",
        "author": "",
        "message": "agent ended without committing or asking",
        "narrator": "",
        "file_format": "",
        **kwargs,
    }
    return type("O", (), fields)()


@pytest.fixture
def store(redis, log):
    return JobStore(redis, log, state_ttl_s=3600, dedup_ttl_s=600)


@pytest.fixture
def log(redis):
    return EventLog(redis)


async def test_run_chat_job_marks_running_then_succeeded(redis, store, log, monkeypatch):
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))

    # Stub out the heavy bits: agent loop + download poll.
    fake_agent = AsyncMock(return_value=_outcome(
        kind="committed",
        download={"id": "dl1"},
        display="“Dune”",
        title="Dune",
        author="Frank Herbert",
        message=None,
    ))
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
    # The traceback belongs in the log; the card gets something actionable.
    assert "RuntimeError" not in refreshed.error
    assert refreshed.error


async def test_run_chat_job_skips_already_terminal(redis, store, log, monkeypatch):
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    # Pre-cancel before the worker picks it up.
    await store.update_status(job.id, JobStatus.cancelled)

    # If run_chat_job didn't short-circuit, it'd hit run_agent. Set a poison
    # mock — if it's called, the test fails loudly.
    monkeypatch.setattr(
        "auto_torrent.server.jobs.worker.run_agent",
        AsyncMock(side_effect=AssertionError("agent should not run for terminal job")),
    )
    await run_chat_job({"redis": redis, "store": store, "log": log}, job.id)
    final = await store.get(job.id)
    assert final.status == JobStatus.cancelled  # unchanged


# --- U1: outcomes that used to die silently --------------------------------
#
# The old guard was `outcome.kind not in ("asked", "no_results") and not
# bus.messaged`. bus.messaged is set by the mandatory opening "Searching…"
# frame, so it suppressed the terminal event for EVERY non-committed outcome
# — the client kept its spinner up forever. Each of these asserts a terminal
# frame now reaches the stream.


@pytest.mark.parametrize(
    "kind,message",
    [
        ("error", "agent ended without committing or asking"),
        ("asked", None),
        ("no_results", None),
    ],
)
async def test_non_committed_outcomes_publish_a_terminal_event(
    redis, store, log, monkeypatch, kind, message
):
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    monkeypatch.setattr(
        "auto_torrent.server.jobs.worker.run_agent",
        AsyncMock(return_value=_outcome(kind=kind, message=message)),
    )

    await run_chat_job({"redis": redis, "store": store, "log": log}, job.id)

    assert (await store.get(job.id)).status == JobStatus.failed
    terminal = [e for e in await _events(redis, job.id) if e[0] == "error"]
    assert len(terminal) == 1
    assert terminal[0][1]["message"]


async def test_crash_publishes_exactly_one_terminal_event(redis, store, log, monkeypatch):
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    monkeypatch.setattr(
        "auto_torrent.server.jobs.worker.run_agent",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    await run_chat_job({"redis": redis, "store": store, "log": log}, job.id)

    terminal = [e for e in await _events(redis, job.id) if e[0] == "error"]
    assert len(terminal) == 1
    assert "RuntimeError" not in terminal[0][1]["message"]
    assert terminal[0][1]["message"]


async def test_success_publishes_exactly_one_completed_event(
    redis, store, log, monkeypatch
):
    """The double-publish guard: _emit_download_and_poll used to emit
    `completed` unconditionally, and the store now publishes on the succeeded
    transition. Only one frame may reach the stream."""
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    monkeypatch.setattr(
        "auto_torrent.server.jobs.worker.run_agent",
        AsyncMock(return_value=_outcome(
            kind="committed",
            download={"id": "dl1"},
            display="“Dune”",
            title="Dune",
            author="Frank Herbert",
            message=None,
        )),
    )
    monkeypatch.setattr(
        "auto_torrent.server.jobs.worker._emit_download_and_poll", AsyncMock()
    )

    await run_chat_job({"redis": redis, "store": store, "log": log}, job.id)

    completed = [e for e in await _events(redis, job.id) if e[0] == "completed"]
    assert len(completed) == 1
    assert completed[0][1] == {"title": "Dune", "author": "Frank Herbert", "already_had": False}


async def test_jobs_path_defers_terminal_emit_to_the_store(redis, store, log, monkeypatch):
    """Guards the wiring itself: if emit_terminal ever stops being passed as
    False, the double-publish race comes straight back."""
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    monkeypatch.setattr(
        "auto_torrent.server.jobs.worker.run_agent",
        AsyncMock(return_value=_outcome(
            kind="committed",
            download={"id": "dl1"},
            display="“Dune”",
            title="Dune",
            author="Frank Herbert",
            message=None,
        )),
    )
    emit = AsyncMock()
    monkeypatch.setattr("auto_torrent.server.jobs.worker._emit_download_and_poll", emit)

    await run_chat_job({"redis": redis, "store": store, "log": log}, job.id)

    assert emit.await_args.kwargs["emit_terminal"] is False


async def test_downloaded_but_not_imported_publishes_a_terminal_event(
    redis, store, log, monkeypatch
):
    """_emit_download_and_poll returns False when organise or the ABS scan
    failed. That path set a failed status and published nothing."""
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    monkeypatch.setattr(
        "auto_torrent.server.jobs.worker.run_agent",
        AsyncMock(return_value=_outcome(
            kind="committed",
            download={"id": "dl1"},
            display="“Dune”",
            title="Dune",
            author="Frank Herbert",
            message=None,
        )),
    )
    monkeypatch.setattr(
        "auto_torrent.server.jobs.worker._emit_download_and_poll",
        AsyncMock(return_value=False),
    )

    await run_chat_job({"redis": redis, "store": store, "log": log}, job.id)

    assert (await store.get(job.id)).status == JobStatus.failed
    terminal = [e for e in await _events(redis, job.id) if e[0] == "error"]
    assert len(terminal) == 1
    assert [e for e in await _events(redis, job.id) if e[0] == "completed"] == []


# --- U12: a book that never downloaded must not report success -------------


@pytest.mark.parametrize(
    "failure_class,message",
    [
        ("no_seeders", "“Dune”"),
        ("import_failed", "“Dune”"),
        ("infra_error", "boom"),
    ],
)
async def test_unfinished_download_never_reports_success(
    redis, store, log, monkeypatch, failure_class, message
):
    from auto_torrent.server.worker import DownloadResult

    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    monkeypatch.setattr(
        "auto_torrent.server.jobs.worker.run_agent",
        AsyncMock(return_value=_outcome(
            kind="committed",
            download={"id": "dl1"},
            display="“Dune”",
            title="Dune",
            author="Frank Herbert",
            message=None,
        )),
    )
    monkeypatch.setattr(
        "auto_torrent.server.jobs.worker._emit_download_and_poll",
        AsyncMock(return_value=DownloadResult(
            ok=False, failure_class=failure_class, message=message
        )),
    )

    await run_chat_job({"redis": redis, "store": store, "log": log}, job.id)

    assert (await store.get(job.id)).status == JobStatus.failed
    events = await _events(redis, job.id)
    assert [e for e in events if e[0] == "completed"] == []
    assert len([e for e in events if e[0] == "error"]) == 1


async def test_finished_download_still_reports_success(redis, store, log, monkeypatch):
    """The other side of the guard — a real success must not get caught by it."""
    from auto_torrent.server.worker import DownloadResult

    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    monkeypatch.setattr(
        "auto_torrent.server.jobs.worker.run_agent",
        AsyncMock(return_value=_outcome(
            kind="committed",
            download={"id": "dl1"},
            display="“Dune”",
            title="Dune",
            author="Frank Herbert",
            message=None,
        )),
    )
    monkeypatch.setattr(
        "auto_torrent.server.jobs.worker._emit_download_and_poll",
        AsyncMock(return_value=DownloadResult.success()),
    )

    await run_chat_job({"redis": redis, "store": store, "log": log}, job.id)

    assert (await store.get(job.id)).status == JobStatus.succeeded
    assert len([e for e in await _events(redis, job.id) if e[0] == "completed"]) == 1
