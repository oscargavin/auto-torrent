import json
import time

import pytest

from auto_torrent.server.jobs.events import EventLog
from auto_torrent.server.jobs.store import JobStore
from auto_torrent.server.jobs.types import CreateJobRequest, FailureClass, JobStatus


@pytest.fixture
def log(redis):
    return EventLog(redis)


@pytest.fixture
def store(redis, log):
    # 1h dedup TTL, 7d state TTL — concrete numbers are fine in tests.
    return JobStore(redis, log, state_ttl_s=7 * 24 * 3600, dedup_ttl_s=3600)


async def _events(redis, job_id: str) -> list[tuple[str, dict]]:
    """Every event published for a job, oldest first, as (type, data)."""
    entries = await redis.xrange(f"job:{job_id}:events")
    return [(fields["type"], json.loads(fields["data"])) for _id, fields in entries]


async def test_create_returns_pending_job(store):
    job, created = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    assert created is True
    assert job.status == JobStatus.pending
    assert job.profile_id == "p1"
    assert job.query == "dune"


async def test_create_is_idempotent_per_profile_query(store):
    a, created_a = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    b, created_b = await store.create(CreateJobRequest(profile_id="p1", query="DUNE  "))
    assert created_a is True
    assert created_b is False  # dedup hit
    assert a.id == b.id


async def test_dedup_scoped_to_profile(store):
    a, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    b, created_b = await store.create(CreateJobRequest(profile_id="p2", query="dune"))
    assert created_b is True
    assert a.id != b.id


async def test_dedup_releases_after_terminal_status(store):
    a, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await store.update_status(a.id, JobStatus.failed, error="no torrent")
    # A new request after failure starts a fresh job.
    b, created_b = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    assert created_b is True
    assert a.id != b.id


async def test_list_returns_recent_jobs_for_profile(store):
    a, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    b, _ = await store.create(CreateJobRequest(profile_id="p1", query="hyperion"))
    _, _ = await store.create(CreateJobRequest(profile_id="p2", query="dune"))

    jobs = await store.list_for_profile("p1", limit=10)
    ids = [j.id for j in jobs]
    # Newest first (b created after a).
    assert ids == [b.id, a.id]


async def test_list_respects_limit(store):
    for i in range(5):
        await store.create(CreateJobRequest(profile_id="p1", query=f"q{i}"))
    jobs = await store.list_for_profile("p1", limit=3)
    assert len(jobs) == 3


# --- U1: the terminal status IS the terminal event -------------------------
#
# Before U1 the store wrote a terminal status and published nothing, leaving
# call sites to emit — which the jobs worker then suppressed for every
# non-committed outcome. The client spun forever. These assert the store is
# now the sole, exactly-once producer.


async def test_failed_publishes_one_error_event(store, redis):
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await store.update_status(job.id, JobStatus.failed, error="no seeders")

    assert await _events(redis, job.id) == [
        ("error", {"message": "no seeders", "failure_class": "infra_error"})
    ]


async def test_succeeded_publishes_one_completed_event(store, redis):
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await store.update_status(job.id, JobStatus.running)
    await store.update_status(
        job.id, JobStatus.succeeded, picked_title="Dune", picked_author="Frank Herbert"
    )

    assert await _events(redis, job.id) == [
        ("completed", {"title": "Dune", "author": "Frank Herbert"})
    ]


async def test_cancelled_publishes_one_cancelled_event(store, redis):
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await store.update_status(job.id, JobStatus.running)
    await store.update_status(job.id, JobStatus.cancelled)

    # The class rides on the event so a client watching SSE can render the
    # right copy without a follow-up request.
    assert await _events(redis, job.id) == [
        ("cancelled", {"failure_class": "cancelled_by_user"})
    ]


async def test_non_terminal_transition_publishes_nothing(store, redis):
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await store.update_status(job.id, JobStatus.running)

    assert await _events(redis, job.id) == []


async def test_second_terminal_write_publishes_nothing(store, redis):
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await store.update_status(job.id, JobStatus.failed, error="first")
    unchanged = await store.update_status(job.id, JobStatus.succeeded, picked_title="Dune")

    # Terminal is final: the second write is a no-op that returns the job as-is.
    assert unchanged is not None
    assert unchanged.status == JobStatus.failed
    assert unchanged.error == "first"
    assert await _events(redis, job.id) == [
        ("error", {"message": "first", "failure_class": "infra_error"})
    ]


async def test_failed_without_error_still_carries_a_message(store, redis):
    """The client renders event.data.message directly — it must never be absent."""
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await store.update_status(job.id, JobStatus.failed)

    (event_type, data), = await _events(redis, job.id)
    assert event_type == "error"
    assert data["message"]


async def test_completed_falls_back_to_already_stored_picked_fields(store, redis):
    """The committed event may have set the title earlier; a later succeeded
    transition that omits it must still publish the book, not empty strings."""
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await store.update_status(
        job.id, JobStatus.running, picked_title="Dune", picked_author="Frank Herbert"
    )
    await store.update_status(job.id, JobStatus.succeeded)

    assert await _events(redis, job.id) == [
        ("completed", {"title": "Dune", "author": "Frank Herbert"})
    ]


async def test_set_download_id_publishes_nothing(store, redis):
    """It's metadata (a pointer to the subprocess), not a transition."""
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await store.set_download_id(job.id, "abc12345")

    assert await _events(redis, job.id) == []


# --- U5: reaping jobs whose worker died ------------------------------------
#
# Every terminal write happens inside the arq worker's own code. A SIGKILL,
# an OOM, or a host reboot leaves the job at `running` forever — and the
# client's poll faithfully confirms `running` on every request, which is the
# permanent spinner again, now with battery cost.


@pytest.fixture
def reaping_store(redis, log):
    """A store that reaps anything not touched in the last second."""
    return JobStore(
        redis, log, state_ttl_s=3600, dedup_ttl_s=600, reap_after_s=1
    )


async def _age(redis, job_id: str, seconds: float) -> None:
    """Backdate updated_at to simulate a worker that stopped writing."""
    await redis.hset(f"job:{job_id}", "updated_at", str(time.time() - seconds))


async def test_stale_running_job_is_reaped_on_read(reaping_store, redis):
    job, _ = await reaping_store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await reaping_store.update_status(job.id, JobStatus.running)
    await _age(redis, job.id, 60)

    reaped = await reaping_store.get(job.id)
    assert reaped.status == JobStatus.failed
    assert reaped.error


async def test_reaping_publishes_a_terminal_event(reaping_store, redis):
    """Otherwise the client has a terminal status it can only discover by
    polling — the reaper has to close the stream too."""
    job, _ = await reaping_store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await reaping_store.update_status(job.id, JobStatus.running)
    await _age(redis, job.id, 60)

    await reaping_store.get(job.id)

    assert [t for t, _d in await _events(redis, job.id)] == ["error"]


async def test_fresh_running_job_is_not_reaped(reaping_store, redis):
    job, _ = await reaping_store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await reaping_store.update_status(job.id, JobStatus.running)

    assert (await reaping_store.get(job.id)).status == JobStatus.running
    assert await _events(redis, job.id) == []


async def test_reaping_is_idempotent(reaping_store, redis):
    job, _ = await reaping_store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await reaping_store.update_status(job.id, JobStatus.running)
    await _age(redis, job.id, 60)

    first = await reaping_store.get(job.id)
    second = await reaping_store.get(job.id)

    assert first.status == second.status == JobStatus.failed
    assert len(await _events(redis, job.id)) == 1


async def test_already_terminal_job_is_left_alone(reaping_store, redis):
    job, _ = await reaping_store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await reaping_store.update_status(job.id, JobStatus.succeeded, picked_title="Dune")
    await _age(redis, job.id, 60)

    assert (await reaping_store.get(job.id)).status == JobStatus.succeeded
    assert [t for t, _d in await _events(redis, job.id)] == ["completed"]


async def test_list_reaps_stale_jobs_too(reaping_store, redis):
    """The app's list endpoint is the other read path a stranded job reaches."""
    job, _ = await reaping_store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await reaping_store.update_status(job.id, JobStatus.running)
    await _age(redis, job.id, 60)

    listed = await reaping_store.list_for_profile("p1", limit=10)
    assert [j.status for j in listed] == [JobStatus.failed]


async def test_default_store_does_not_reap_a_normal_running_job(store, redis):
    """Guards the default budget: a job that just started must survive."""
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await store.update_status(job.id, JobStatus.running)

    assert (await store.get(job.id)).status == JobStatus.running


# --- U4: the chosen edition is visible -------------------------------------


async def test_set_picked_book_records_narrator_and_format(store):
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await store.set_picked_book(
        job.id, title="Dune", narrator="Scott Brick", file_format="M4B"
    )

    refreshed = await store.get(job.id)
    assert refreshed.picked_narrator == "Scott Brick"
    assert refreshed.picked_format == "M4B"


async def test_set_picked_book_skips_empty_fields(store):
    """The agent often has no narrator. Writing "" would render an empty
    bullet on the card rather than nothing."""
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await store.set_picked_book(job.id, narrator="", file_format="")

    refreshed = await store.get(job.id)
    assert refreshed.picked_narrator is None
    assert refreshed.picked_format is None


async def test_set_picked_book_works_on_a_cancelled_job(store):
    """Metadata, not a transition — a cancel racing the agent's commit should
    still leave an accurate record of what was actually started."""
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await store.update_status(job.id, JobStatus.cancelled)
    await store.set_picked_book(job.id, narrator="Scott Brick", file_format="M4B")

    assert (await store.get(job.id)).picked_narrator == "Scott Brick"


async def test_job_without_edition_fields_deserialises(store):
    """Rows written before U4 have no such keys."""
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    refreshed = await store.get(job.id)
    assert refreshed.picked_narrator is None
    assert refreshed.picked_format is None


# --- U10: failure_class ----------------------------------------------------


async def test_cancelled_job_remembers_the_book_it_had_picked(store):
    """A cancel returns early and never reaches the terminal transition that
    writes picked_title, so without set_picked_book the card falls back to the
    raw query and the user can't tell which book was cancelled."""
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await store.set_picked_book(job.id, title="Dune", author="Frank Herbert")
    await store.update_status(job.id, JobStatus.cancelled)

    d = await store.get(job.id)
    assert d.picked_title == "Dune"
    assert d.picked_author == "Frank Herbert"


async def test_cancel_is_classified_without_the_caller_knowing_the_vocabulary(store):
    """The DELETE handler lives in the API process; it shouldn't have to."""
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await store.update_status(job.id, JobStatus.cancelled)
    assert (await store.get(job.id)).failure_class is FailureClass.cancelled_by_user


async def test_failure_class_round_trips_through_redis(store):
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await store.update_status(
        job.id, JobStatus.failed, error="nope",
        failure_class=FailureClass.no_seeders,
    )
    assert (await store.get(job.id)).failure_class is FailureClass.no_seeders


async def test_absent_failure_class_deserialises(store):
    """Rows written before this field must still load."""
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    assert (await store.get(job.id)).failure_class is None


async def test_reaped_job_is_classified_as_a_timeout(reaping_store, redis):
    job, _ = await reaping_store.create(CreateJobRequest(profile_id="p1", query="dune"))
    await reaping_store.update_status(job.id, JobStatus.running)
    await _age(redis, job.id, 60)

    assert (await reaping_store.get(job.id)).failure_class is FailureClass.download_timeout


async def test_update_status_return_value_matches_redis(store):
    """The returned object is built locally to save a round-trip, so every
    field written must be mirrored onto it — a caller acting on the return
    value must not see a job that disagrees with what was stored."""
    job, _ = await store.create(CreateJobRequest(profile_id="p1", query="dune"))
    returned = await store.update_status(
        job.id, JobStatus.failed, error="nope",
        failure_class=FailureClass.no_seeders,
    )
    stored = await store.get(job.id)

    assert returned.failure_class == stored.failure_class == FailureClass.no_seeders
    assert returned.status == stored.status
    assert returned.error == stored.error
