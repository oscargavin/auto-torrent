import hashlib

from auto_torrent.server.jobs.types import (
    CreateJobRequest,
    Job,
    JobStatus,
    dedup_hash,
)


def test_status_values():
    assert {s.value for s in JobStatus} == {
        "pending",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    }


def test_create_request_validates_query():
    # Empty / whitespace queries rejected.
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CreateJobRequest(profile_id="p1", query="   ")


def test_dedup_hash_normalises_query():
    # Same logical request → same hash regardless of case/whitespace.
    a = dedup_hash("p1", "The Lies of Locke Lamora")
    b = dedup_hash("p1", "  the lies of LOCKE lamora  ")
    assert a == b
    assert a != dedup_hash("p2", "The Lies of Locke Lamora")
    assert len(a) == 64  # sha256 hex
    assert a == hashlib.sha256(b"p1:the lies of locke lamora").hexdigest()


def test_job_roundtrip_through_redis_hash():
    job = Job(
        id="j1",
        profile_id="p1",
        query="x",
        status=JobStatus.pending,
        created_at=1.0,
        updated_at=1.0,
    )
    encoded = job.to_redis_hash()
    assert all(isinstance(v, str) for v in encoded.values())
    decoded = Job.from_redis_hash(encoded)
    assert decoded == job
