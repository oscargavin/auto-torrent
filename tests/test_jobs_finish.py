import json
from unittest.mock import AsyncMock

import pytest

from auto_torrent.server.jobs.events import EventLog
from auto_torrent.server.jobs.store import JobStore
from auto_torrent.server.jobs.types import CreateJobRequest, JobStatus
from auto_torrent.server.jobs.bus import StreamEventBus
from auto_torrent.server.jobs.finish import finish_agent_outcome


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


async def _finish(store, log, job, outcome) -> None:
    await finish_agent_outcome(
        store=store, bus=StreamEventBus(job.id, log), job=job, outcome=outcome
    )


@pytest.fixture
def store(redis, log):
    return JobStore(redis, log, state_ttl_s=3600, dedup_ttl_s=600)


@pytest.fixture
def log(redis):
    return EventLog(redis)


async def test_committed_outcome_marks_succeeded(redis, store, log, monkeypatch):
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))

    # Stub out the download poll.
    outcome = _outcome(
        kind="committed",
        download={"id": "dl1"},
        display="“Dune”",
        title="Dune",
        author="Frank Herbert",
        message=None,
    )
    monkeypatch.setattr(
        "auto_torrent.server.jobs.finish._emit_download_and_poll",
        AsyncMock(),
    )

    await _finish(store, log, job, outcome)

    refreshed = await store.get(job.id)
    assert refreshed.status == JobStatus.succeeded
    assert refreshed.picked_title == "Dune"
    assert refreshed.picked_author == "Frank Herbert"


# --- U1: outcomes that used to die silently --------------------------------
#
# The old guard was `outcome.kind not in ("asked", "no_results") and not
# bus.messaged`. That suppressed the terminal event for every non-committed
# outcome, and the client kept its spinner up forever. Each of these asserts a
# terminal frame now reaches the stream.


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
    outcome = _outcome(kind=kind, message=message)

    await _finish(store, log, job, outcome)

    assert (await store.get(job.id)).status == JobStatus.failed
    terminal = [e for e in await _events(redis, job.id) if e[0] == "error"]
    assert len(terminal) == 1
    assert terminal[0][1]["message"]


async def test_success_publishes_exactly_one_completed_event(
    redis, store, log, monkeypatch
):
    """The double-publish guard: _emit_download_and_poll used to emit
    `completed` unconditionally, and the store now publishes on the succeeded
    transition. Only one frame may reach the stream."""
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    outcome = _outcome(
        kind="committed",
        download={"id": "dl1"},
        display="“Dune”",
        title="Dune",
        author="Frank Herbert",
        message=None,
    )
    monkeypatch.setattr(
        "auto_torrent.server.jobs.finish._emit_download_and_poll", AsyncMock()
    )

    await _finish(store, log, job, outcome)

    completed = [e for e in await _events(redis, job.id) if e[0] == "completed"]
    assert len(completed) == 1
    assert completed[0][1] == {"title": "Dune", "author": "Frank Herbert", "already_had": False}


async def test_downloaded_but_not_imported_publishes_a_terminal_event(
    redis, store, log, monkeypatch
):
    """_emit_download_and_poll returns a not-ok result when organise or the ABS
    scan failed. That path set a failed status and published nothing."""
    from auto_torrent.server.worker import DownloadResult
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    outcome = _outcome(
        kind="committed",
        download={"id": "dl1"},
        display="“Dune”",
        title="Dune",
        author="Frank Herbert",
        message=None,
    )
    monkeypatch.setattr(
        "auto_torrent.server.jobs.finish._emit_download_and_poll",
        AsyncMock(return_value=DownloadResult(
            ok=False, failure_class="import_failed", message="“Dune”"
        )),
    )

    await _finish(store, log, job, outcome)

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
    outcome = _outcome(
        kind="committed",
        download={"id": "dl1"},
        display="“Dune”",
        title="Dune",
        author="Frank Herbert",
        message=None,
    )
    monkeypatch.setattr(
        "auto_torrent.server.jobs.finish._emit_download_and_poll",
        AsyncMock(return_value=DownloadResult(
            ok=False, failure_class=failure_class, message=message
        )),
    )

    await _finish(store, log, job, outcome)

    assert (await store.get(job.id)).status == JobStatus.failed
    events = await _events(redis, job.id)
    assert [e for e in events if e[0] == "completed"] == []
    assert len([e for e in events if e[0] == "error"]) == 1


async def test_finished_download_still_reports_success(redis, store, log, monkeypatch):
    """The other side of the guard — a real success must not get caught by it."""
    from auto_torrent.server.worker import DownloadResult

    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    outcome = _outcome(
        kind="committed",
        download={"id": "dl1"},
        display="“Dune”",
        title="Dune",
        author="Frank Herbert",
        message=None,
    )
    monkeypatch.setattr(
        "auto_torrent.server.jobs.finish._emit_download_and_poll",
        AsyncMock(return_value=DownloadResult.success()),
    )

    await _finish(store, log, job, outcome)

    assert (await store.get(job.id)).status == JobStatus.succeeded
    assert len([e for e in await _events(redis, job.id) if e[0] == "completed"]) == 1


async def test_already_in_library_stops_the_spawned_download(redis, store, log, monkeypatch):
    """The agent spawns the download before the library check. When the book is
    already on the shelf, that download must actually be killed — the call was
    once left un-awaited, so the coroutine never ran and a second copy
    downloaded behind a card that said "Already in your library"."""
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    outcome = _outcome(
        kind="committed", download={"id": "dl1"}, display="“Dune”",
        title="Dune", author="Frank Herbert", message=None,
    )
    monkeypatch.setattr(
        "auto_torrent.server.jobs.finish.already_in_library", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        "auto_torrent.server.jobs.finish._read_state", lambda _id: {"id": _id, "pid": 1}
    )
    kill = AsyncMock()
    monkeypatch.setattr("auto_torrent.server.jobs.finish._kill_download_and_clean", kill)

    await _finish(store, log, job, outcome)

    kill.assert_awaited_once_with({"id": "dl1", "pid": 1})
    completed = [e for e in await _events(redis, job.id) if e[0] == "completed"]
    assert completed[0][1]["already_had"] is True


async def test_the_chosen_edition_reaches_the_download_pump(redis, store, log, monkeypatch):
    """Narrator and format travel on `committed`, so the card shows which copy
    this is at once rather than on the next snapshot poll."""
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    outcome = _outcome(
        kind="committed", download={"id": "dl1"}, display="“Dune”",
        title="Dune", author="Frank Herbert", message=None,
        narrator="Scott Brick", file_format="M4B",
    )
    emit = AsyncMock()
    monkeypatch.setattr("auto_torrent.server.jobs.finish._emit_download_and_poll", emit)

    await _finish(store, log, job, outcome)

    # The pump reads the edition off the outcome itself.
    assert emit.await_args.args[1] is outcome
