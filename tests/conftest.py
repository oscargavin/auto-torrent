import os as _os

import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeredis_aio

# Set test env BEFORE any test imports auto_torrent.server.app — Settings()
# reads env at module load.
_TEST_ENV = {
    "ABS_API_TOKEN": "test",
    "ABS_LIBRARY_ID": "test",
    "ATB_CWD": "/tmp",
    "ATB_API_TOKEN": "test-token",
}
_os.environ.update(_TEST_ENV)


@pytest_asyncio.fixture
async def redis():
    """An isolated fakeredis instance per test."""
    client = fakeredis_aio.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()
