import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeredis_aio


@pytest_asyncio.fixture
async def redis():
    """An isolated fakeredis instance per test."""
    client = fakeredis_aio.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()
