"""U1: structured lifecycle events from the shared poll path.

Covers the `importing` stage emit, the guarantee that the SMS-shaped sink
(which has `send` but no `emit`) never receives a structured dict, and the
event-vocabulary parity hook.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from auto_torrent.server import worker as worker_module
from auto_torrent.server.event_types import (
    EVENT_PROGRESS,
    STAGE_IMPORT_FAILED,
    STAGE_IMPORTING,
    STAGE_RETRYING,
)
from auto_torrent.server.worker import (
    ImportIncompleteError,
    _emit_event,
    _organize_files,
    poll_and_finalise,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _async(value):
    """Wrap a value as an awaitable so a plain lambda can stand in for an
    async function being monkeypatched."""
    return value


def _mk(tmp_path):
    """A landing dir with one file (so organise has something to move)."""
    d = tmp_path / "landing"
    d.mkdir(exist_ok=True)
    (d / "a.m4b").write_text("x")
    return d


class BusSink:
    """Chat/jobs-style sink: supports both send (strings) and emit (events)."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.emitted: list[tuple[str, dict]] = []

    def send(self, _to: str, body: str) -> None:
        assert isinstance(body, str), "send() must only ever receive a string"
        self.sent.append(body)

    def emit(self, event: str, data: dict) -> None:
        self.emitted.append((event, data))


class SmsOnlySink:
    """SMS-shaped sink: send only, no emit (like the real SMSClient)."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, _to: str, body: str) -> None:
        assert isinstance(body, str)
        self.sent.append(body)


def test_emit_event_noops_without_emit():
    sink = SmsOnlySink()
    # Must not raise, must not somehow turn into a send.
    _emit_event(sink, EVENT_PROGRESS, {"stage": STAGE_IMPORTING})
    assert sink.sent == []


class _FakeABS:
    def __init__(self, _settings):
        pass

    async def scan_library(self, _library_id):
        return None


@pytest.mark.anyio
async def test_importing_emitted_once_before_success(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_module, "ABSClient", _FakeABS)

    async def fake_watch(_id, **_kw):
        return "completed"

    landing = tmp_path / "landing"
    landing.mkdir()
    monkeypatch.setattr(worker_module, "_watch_until_done", fake_watch)
    monkeypatch.setattr(
        worker_module, "_refresh_state",
        lambda _id: {"id": _id, "path": str(landing), "status": "completed"},
    )
    monkeypatch.setattr(worker_module, "_organize_files", lambda *a, **k: tmp_path / "dest")

    sink = BusSink()
    settings = SimpleNamespace(abs_library_path=str(tmp_path), abs_library_id="lib")

    await poll_and_finalise(
        download={"id": "d1"}, fallbacks=[], display="“The Book”",
        author="Author", title="The Book", phone="s1", settings=settings, sms=sink,
    )

    importing = [d for ev, d in sink.emitted if ev == EVENT_PROGRESS and d.get("stage") == STAGE_IMPORTING]
    assert len(importing) == 1, f"expected exactly one importing event, got {sink.emitted}"
    # The success line still goes out as a plain string on the send channel.
    assert any("library" in s for s in sink.sent)
    # No structured dict ever reached the send channel.
    assert all(isinstance(s, str) for s in sink.sent)


@pytest.mark.anyio
async def test_scan_failure_raises_import_incomplete(tmp_path, monkeypatch):
    class _FailingABS:
        def __init__(self, _settings):
            pass

        async def scan_library(self, _library_id):
            raise RuntimeError("ABS down")

    monkeypatch.setattr(worker_module, "ABSClient", _FailingABS)

    async def fake_watch(_id, **_kw):
        return "completed"

    landing = tmp_path / "landing"
    landing.mkdir()
    monkeypatch.setattr(worker_module, "_watch_until_done", fake_watch)
    monkeypatch.setattr(
        worker_module, "_refresh_state",
        lambda _id: {"id": _id, "path": str(landing), "status": "completed"},
    )
    monkeypatch.setattr(worker_module, "_organize_files", lambda *a, **k: tmp_path / "dest")

    sink = BusSink()
    settings = SimpleNamespace(abs_library_path=str(tmp_path), abs_library_id="lib")

    with pytest.raises(ImportIncompleteError):
        await poll_and_finalise(
            download={"id": "d1"}, fallbacks=[], display="“The Book”",
            author="Author", title="The Book", phone="s1", settings=settings, sms=sink,
        )

    # User must NOT be told it's in the library.
    assert not any("in your library" in s for s in sink.sent)
    # And an import_failed stage was surfaced.
    assert any(
        ev == EVENT_PROGRESS and d.get("stage") == STAGE_IMPORT_FAILED
        for ev, d in sink.emitted
    )


@pytest.mark.anyio
async def test_stall_with_seeders_extends_grace_once(tmp_path, monkeypatch):
    """R14: a stall on a torrent that still has seeders earns one extra grace
    window (no fallback switch) before being abandoned."""
    monkeypatch.setattr(worker_module, "ABSClient", _FakeABS)

    outcomes = iter(["stalled", "completed"])
    monkeypatch.setattr(worker_module, "_watch_until_done", lambda _id, **_kw: _async(next(outcomes)))
    monkeypatch.setattr(
        worker_module, "_refresh_state",
        lambda _id: {"id": _id, "magnet": "magnet:seeded", "path": str(_mk(tmp_path)), "status": "completed"},
    )
    monkeypatch.setattr(worker_module, "_organize_files", lambda *a, **k: tmp_path / "dest")
    # Still seeded → extend grace, don't switch.
    monkeypatch.setattr(worker_module, "_reprobe_seeders", lambda _m: _async(5))

    # No fallback should ever be started.
    monkeypatch.setattr(
        worker_module, "_execute_download_bg",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not switch when seeded")),
    )

    sink = BusSink()
    settings = SimpleNamespace(abs_library_path=str(tmp_path), abs_library_id="lib")
    await poll_and_finalise(
        download={"id": "d1", "magnet": "magnet:seeded"}, fallbacks=[],
        display="“Book”", author="A", title="Book", phone="s", settings=settings, sms=sink,
    )

    assert any(d.get("stage") == STAGE_RETRYING for _, d in sink.emitted)


@pytest.mark.anyio
async def test_rediscovery_when_fallbacks_empty(tmp_path, monkeypatch):
    """R13: when the fixed list is empty, rediscovery supplies a fresh
    candidate (excluding already-tried magnets) and the download continues."""
    monkeypatch.setattr(worker_module, "ABSClient", _FakeABS)

    outcomes = iter(["failed", "completed"])
    monkeypatch.setattr(worker_module, "_watch_until_done", lambda _id, **_kw: _async(next(outcomes)))
    monkeypatch.setattr(
        worker_module, "_refresh_state",
        lambda _id: {"id": _id, "magnet": "magnet:dead", "path": str(_mk(tmp_path)), "status": "failed"},
    )
    monkeypatch.setattr(worker_module, "_organize_files", lambda *a, **k: tmp_path / "dest")
    monkeypatch.setattr(worker_module, "_kill_download_and_clean", lambda _s: _async(None))

    seen = {}

    def fake_exec(title, magnet, cover):
        seen["magnet"] = magnet
        return {"id": "d2", "magnet": magnet}

    monkeypatch.setattr(worker_module, "_execute_download_bg", fake_exec)

    async def fake_rediscover(query, tried):
        assert "magnet:dead" in tried  # original excluded
        return [{"magnet": "magnet:fresh", "title": "Book"}]

    monkeypatch.setattr(worker_module, "_rediscover_candidates", fake_rediscover)

    sink = BusSink()
    settings = SimpleNamespace(abs_library_path=str(tmp_path), abs_library_id="lib")
    await poll_and_finalise(
        download={"id": "d1", "magnet": "magnet:dead"}, fallbacks=[],
        display="“Book”", author="A", title="Book", phone="s", settings=settings,
        sms=sink, query="the book",
    )

    assert seen.get("magnet") == "magnet:fresh"
    assert any(d.get("stage") == STAGE_RETRYING for _, d in sink.emitted)


@pytest.mark.anyio
async def test_rediscovery_capped(tmp_path, monkeypatch):
    """Rediscovery is bounded: it re-searches at most MAX_REDISCOVERY_ROUNDS
    times across repeated stalls, then gives up rather than looping forever."""
    monkeypatch.setattr(worker_module, "ABSClient", _FakeABS)
    # Every download fails; never seeded.
    monkeypatch.setattr(worker_module, "_watch_until_done", lambda _id, **_kw: _async("failed"))
    monkeypatch.setattr(
        worker_module, "_refresh_state",
        lambda _id: {"id": _id, "magnet": "magnet:dead", "status": "failed"},
    )
    monkeypatch.setattr(worker_module, "_kill_download_and_clean", lambda _s: _async(None))
    monkeypatch.setattr(
        worker_module, "_execute_download_bg",
        lambda title, magnet, cover: {"id": "dN", "magnet": magnet},
    )

    calls = {"n": 0}

    async def fake_rediscover(query, tried):
        calls["n"] += 1
        # First round finds a candidate (so the poll continues), later rounds
        # come up empty.
        return [{"magnet": f"magnet:fresh{calls['n']}", "title": "Book"}] if calls["n"] == 1 else []

    monkeypatch.setattr(worker_module, "_rediscover_candidates", fake_rediscover)

    sink = BusSink()
    settings = SimpleNamespace(abs_library_path=str(tmp_path), abs_library_id="lib")
    # Giving up raises rather than returning — a normal return here is
    # indistinguishable from success to the caller, which is what made a
    # never-downloaded book report "saved to your library".
    with pytest.raises(worker_module.NoCandidatesLeftError):
        await poll_and_finalise(
            download={"id": "d1", "magnet": "magnet:dead"}, fallbacks=[],
            display="“Book”", author="A", title="Book", phone="s", settings=settings,
            sms=sink, query="the book",
        )

    assert calls["n"] == worker_module.MAX_REDISCOVERY_ROUNDS
    assert any("Couldn't get" in s for s in sink.sent)


def test_organize_files_namespaces_on_collision(tmp_path):
    library = tmp_path / "library"

    def _make_download(name, fname):
        d = tmp_path / name
        d.mkdir()
        (d / fname).write_text("audio")
        return d

    src1 = _make_download("dl1", "a.m4b")
    dest1 = _organize_files(str(src1), str(library), "Author", "Wolf Hall", "job-aaa")

    # A second, different download of the same title must not overwrite the first.
    src2 = _make_download("dl2", "b.m4b")
    dest2 = _organize_files(str(src2), str(library), "Author", "Wolf Hall", "job-bbb")

    assert dest1 != dest2
    assert (dest1 / "a.m4b").exists()
    assert (dest2 / "b.m4b").exists()
    # First download's file was not clobbered.
    assert (dest1 / "a.m4b").read_text() == "audio"


# --- U12: abandoned downloads must not look like success -------------------
#
# Before U12 each of these paths did a bare `return`, which the caller could
# not distinguish from "the book is in the library" — so it emitted
# `completed` and the user was told a book they never received had been saved.


async def test_no_candidates_left_raises_with_no_seeders_class(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_module, "ABSClient", _FakeABS)
    monkeypatch.setattr(worker_module, "_watch_until_done", lambda _id, **_kw: _async("failed"))
    monkeypatch.setattr(
        worker_module, "_refresh_state",
        lambda _id: {"id": _id, "magnet": "magnet:dead", "status": "failed"},
    )
    monkeypatch.setattr(worker_module, "_kill_download_and_clean", lambda _s: _async(None))
    monkeypatch.setattr(worker_module, "_rediscover_candidates", lambda q, t: _async([]))

    settings = SimpleNamespace(abs_library_path=str(tmp_path), abs_library_id="lib")
    with pytest.raises(worker_module.NoCandidatesLeftError) as exc:
        await poll_and_finalise(
            download={"id": "d1", "magnet": "magnet:dead"}, fallbacks=[],
            display="“Book”", author="A", title="Book", phone="s",
            settings=settings, sms=BusSink(), query="the book",
        )
    assert exc.value.failure_class == "no_seeders"


async def test_unknown_poll_outcome_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_module, "ABSClient", _FakeABS)
    monkeypatch.setattr(worker_module, "_watch_until_done", lambda _id, **_kw: _async("unknown"))

    settings = SimpleNamespace(abs_library_path=str(tmp_path), abs_library_id="lib")
    with pytest.raises(worker_module.UnknownDownloadOutcomeError):
        await poll_and_finalise(
            download={"id": "d1", "magnet": "m"}, fallbacks=[],
            display="“Book”", author="A", title="Book", phone="s",
            settings=settings, sms=BusSink(),
        )


async def test_lost_state_file_after_completion_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_module, "ABSClient", _FakeABS)
    monkeypatch.setattr(worker_module, "_watch_until_done", lambda _id, **_kw: _async("completed"))
    monkeypatch.setattr(worker_module, "_refresh_state", lambda _id: None)

    settings = SimpleNamespace(abs_library_path=str(tmp_path), abs_library_id="lib")
    with pytest.raises(worker_module.DownloadStateLostError):
        await poll_and_finalise(
            download={"id": "d1", "magnet": "m"}, fallbacks=[],
            display="“Book”", author="A", title="Book", phone="s",
            settings=settings, sms=BusSink(),
        )


def test_every_abandon_path_carries_a_failure_class():
    """Each reason maps to a distinct, non-empty class so the app can render
    different copy per failure rather than one generic message."""
    classes = {
        worker_module.NoCandidatesLeftError.failure_class,
        worker_module.ImportIncompleteError.failure_class,
        worker_module.DownloadStateLostError.failure_class,
        worker_module.UnknownDownloadOutcomeError.failure_class,
    }
    assert all(c for c in classes)
    assert worker_module.NoCandidatesLeftError.failure_class == "no_seeders"
    assert worker_module.ImportIncompleteError.failure_class == "import_failed"


def test_download_result_from_error_reads_the_class():
    result = worker_module.DownloadResult.from_error(
        worker_module.NoCandidatesLeftError("“Book”")
    )
    assert result.ok is False
    assert result.failure_class == "no_seeders"
    assert result.message


def test_download_result_from_unexpected_error_falls_back_to_infra():
    result = worker_module.DownloadResult.from_error(RuntimeError("boom"))
    assert result.ok is False
    assert result.failure_class == "infra_error"


# --- U5: one budget for the whole job --------------------------------------


async def test_expired_deadline_times_out_rather_than_polling(tmp_path, monkeypatch):
    """With the budget already spent, the poll must not start another attempt."""
    monkeypatch.setattr(worker_module, "ABSClient", _FakeABS)
    monkeypatch.setattr(worker_module, "_refresh_state", lambda _id: None)
    monkeypatch.setattr(worker_module, "_kill_download_and_clean", lambda _s: _async(None))

    settings = SimpleNamespace(abs_library_path=str(tmp_path), abs_library_id="lib")
    sink = BusSink()
    with pytest.raises(worker_module.DownloadTimedOutError) as exc:
        await poll_and_finalise(
            download={"id": "d1", "magnet": "m"}, fallbacks=[],
            display="“Book”", author="A", title="Book", phone="s",
            settings=settings, sms=sink,
            deadline=time.monotonic() - 1,  # already expired
        )
    assert exc.value.failure_class == "download_timeout"
    # The user is told their book was stopped, not that a "worker cancelled".
    assert any("too long" in s for s in sink.sent)


async def test_watch_returns_timed_out_on_expired_deadline():
    assert await worker_module._watch_until_done(
        "d1", deadline=time.monotonic() - 1
    ) == "timed_out"


async def test_watch_without_deadline_keeps_per_attempt_behaviour(monkeypatch):
    """Legacy callers (SMS, /chat) pass no deadline and must be unaffected."""
    monkeypatch.setattr(worker_module, "_refresh_state", lambda _id: None)
    monkeypatch.setattr(worker_module.asyncio, "sleep", lambda _s: _async(None))
    # No deadline → falls through to the state read, returns 'unknown', never
    # 'timed_out'.
    assert await worker_module._watch_until_done("d1") == "unknown"


def test_arq_timeout_exceeds_the_poll_budget():
    """If these are equal again, any job that swaps to a fallback gets killed
    by arq mid-attempt and reported as 'worker cancelled'."""
    from auto_torrent.server.jobs.worker import WorkerSettings

    assert WorkerSettings.job_timeout > worker_module.JOB_BUDGET_S


# --- U3: every stage in the vocabulary has a producer ----------------------


def test_all_stages_have_an_emit_site():
    """STAGE_SEARCHING and STAGE_FOUND were defined and emitted by nothing, so
    the app would have rendered states the server never sends. Any stage added
    without a producer fails here."""
    import pathlib

    from auto_torrent.server import event_types

    server_dir = pathlib.Path(event_types.__file__).parent
    sources = "\n".join(
        p.read_text()
        for p in server_dir.rglob("*.py")
        if p.name != "event_types.py"
    )

    names = {
        value: name
        for name, value in vars(event_types).items()
        if name.startswith("STAGE_")
    }
    missing = [names[v] for v in event_types.ALL_STAGES if names[v] not in sources]
    assert not missing, f"stages defined but never emitted: {missing}"


async def test_stall_emits_stalled_before_any_fallback_swap(tmp_path, monkeypatch):
    """The signal must land when the bytes stop, not when we give up — that
    gap is minutes long, and until now a frozen download looked healthy."""
    monkeypatch.setattr(worker_module, "ABSClient", _FakeABS)
    outcomes = iter(["stalled", "completed"])
    monkeypatch.setattr(
        worker_module, "_watch_until_done", lambda _id, **_kw: _async(next(outcomes))
    )
    monkeypatch.setattr(
        worker_module, "_refresh_state",
        lambda _id: {
            "id": _id, "magnet": "magnet:seeded", "path": str(_mk(tmp_path)),
            "status": "completed", "progress": 0.37, "peers": 0,
        },
    )
    monkeypatch.setattr(worker_module, "_organize_files", lambda *a, **k: tmp_path / "dest")
    monkeypatch.setattr(worker_module, "_reprobe_seeders", lambda _m: _async(5))

    sink = BusSink()
    settings = SimpleNamespace(abs_library_path=str(tmp_path), abs_library_id="lib")
    await poll_and_finalise(
        download={"id": "d1", "magnet": "magnet:seeded"}, fallbacks=[],
        display="“Book”", author="A", title="Book", phone="s",
        settings=settings, sms=sink,
    )

    stages = [d.get("stage") for _e, d in sink.emitted]
    assert "stalled" in stages
    # And it precedes the retry narration rather than replacing it.
    assert stages.index("stalled") < stages.index(STAGE_RETRYING)


async def test_stalled_frame_carries_percent_and_peers(tmp_path, monkeypatch):
    """Zero peers at 37% is the difference between 'slow' and 'dead' — the
    card can't distinguish them without these."""
    monkeypatch.setattr(worker_module, "ABSClient", _FakeABS)
    outcomes = iter(["stalled", "completed"])
    monkeypatch.setattr(
        worker_module, "_watch_until_done", lambda _id, **_kw: _async(next(outcomes))
    )
    monkeypatch.setattr(
        worker_module, "_refresh_state",
        lambda _id: {
            "id": _id, "magnet": "m", "path": str(_mk(tmp_path)),
            "status": "completed", "progress": 0.37, "peers": 0,
        },
    )
    monkeypatch.setattr(worker_module, "_organize_files", lambda *a, **k: tmp_path / "dest")
    monkeypatch.setattr(worker_module, "_reprobe_seeders", lambda _m: _async(3))

    sink = BusSink()
    settings = SimpleNamespace(abs_library_path=str(tmp_path), abs_library_id="lib")
    await poll_and_finalise(
        download={"id": "d1", "magnet": "m"}, fallbacks=[],
        display="“Book”", author="A", title="Book", phone="s",
        settings=settings, sms=sink,
    )

    stalled = next(d for _e, d in sink.emitted if d.get("stage") == "stalled")
    assert stalled["percent"] == 37
    assert stalled["peers"] == 0


def test_failure_messages_are_human_not_the_book_title():
    """These exceptions are raised with the display title, so a naive str(exc)
    puts a bare book name where the user expects to read what went wrong."""
    for exc_cls in (
        worker_module.NoCandidatesLeftError,
        worker_module.ImportIncompleteError,
        worker_module.DownloadStateLostError,
        worker_module.UnknownDownloadOutcomeError,
        worker_module.DownloadTimedOutError,
    ):
        result = worker_module.DownloadResult.from_error(exc_cls("“Dune”"))
        assert result.message != "“Dune”", f"{exc_cls.__name__} leaks the title"
        assert len(result.message.split()) >= 4, f"{exc_cls.__name__} message too terse"


def test_no_seeders_message_explains_the_problem():
    result = worker_module.DownloadResult.from_error(
        worker_module.NoCandidatesLeftError("“Dune”")
    )
    assert "sharing" in result.message
    assert result.failure_class == "no_seeders"
