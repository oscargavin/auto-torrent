"""Wire types for the jobs API. Pydantic on the boundary; Redis hashes are str→str."""

from __future__ import annotations

import enum
import hashlib
import time
import uuid
from typing import Self

from pydantic import BaseModel, Field, field_validator


class JobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


TERMINAL_STATUSES = {JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled}


class CreateJobRequest(BaseModel):
    profile_id: str = Field(min_length=1, max_length=64)
    query: str

    @field_validator("query")
    @classmethod
    def _strip_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query must not be empty")
        if len(v) > 500:
            raise ValueError("query too long")
        return v


def dedup_hash(profile_id: str, query: str) -> str:
    """Stable hash so a re-request hits the same in-flight job. Profile-scoped so
    two profiles asking for the same book don't collide on each other's state —
    they could share, but per-profile state (status visible in that profile's
    inbox) is simpler."""
    key = f"{profile_id}:{query.strip().lower()}"
    return hashlib.sha256(key.encode()).hexdigest()


def new_job_id() -> str:
    return uuid.uuid4().hex


class Job(BaseModel):
    id: str
    profile_id: str
    query: str
    status: JobStatus
    created_at: float
    updated_at: float
    picked_title: str | None = None
    picked_author: str | None = None
    error: str | None = None

    @classmethod
    def new(cls, profile_id: str, query: str) -> Self:
        now = time.time()
        return cls(
            id=new_job_id(),
            profile_id=profile_id,
            query=query,
            status=JobStatus.pending,
            created_at=now,
            updated_at=now,
        )

    def to_redis_hash(self) -> dict[str, str]:
        d = self.model_dump()
        # Use .value for enums so we get "pending" not "JobStatus.pending".
        def _str(v: object) -> str:
            if isinstance(v, enum.Enum):
                return v.value
            return str(v)

        return {k: "" if v is None else _str(v) for k, v in d.items()}

    @classmethod
    def from_redis_hash(cls, data: dict[str, str]) -> Self:
        # Pydantic does the coercion; empty strings → None for optional fields.
        cleaned = {k: (None if v == "" else v) for k, v in data.items()}
        return cls.model_validate(cleaned)
