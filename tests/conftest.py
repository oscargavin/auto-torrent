import os as _os
from unittest.mock import patch as _patch

import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeredis_aio

# Set test env + neuter network-touching SMS client BEFORE any test imports
# auto_torrent.server.app — Settings() reads env at module load, and the SMS
# client constructs a real Twilio Client. These must be in place first.
_TEST_ENV = {
    "TWILIO_ACCOUNT_SID": "test",
    "TWILIO_AUTH_TOKEN": "test",
    "TWILIO_PHONE_NUMBER": "+10000000000",
    "ALLOWED_NUMBERS": '["+1234"]',
    "ABS_API_TOKEN": "test",
    "ABS_LIBRARY_ID": "test",
    "ATB_CWD": "/tmp",
    "ATB_API_TOKEN": "test-token",
}
_os.environ.update(_TEST_ENV)
_patch("auto_torrent.server.sms.Client").start()
_patch("auto_torrent.server.sms.RequestValidator").start()


@pytest_asyncio.fixture
async def redis():
    """An isolated fakeredis instance per test."""
    client = fakeredis_aio.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()
