import pytest

from auto_torrent.server.jobs.store import JobStore
from auto_torrent.server.jobs.types import CreateJobRequest, JobStatus


@pytest.fixture
def store(redis):
    # 1h dedup TTL, 7d state TTL — concrete numbers are fine in tests.
    return JobStore(redis, state_ttl_s=7 * 24 * 3600, dedup_ttl_s=3600)


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
