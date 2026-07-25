import json

import pytest

from auto_torrent.server.jobs.events import EventLog
from auto_torrent.server.jobs.store import JobStore
from auto_torrent.server.jobs.types import CreateJobRequest, JobStatus


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

    assert await _events(redis, job.id) == [("error", {"message": "no seeders"})]


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

    assert await _events(redis, job.id) == [("cancelled", {})]


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
    assert await _events(redis, job.id) == [("error", {"message": "first"})]


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
