"""Behavioural tests for the POST /recommend endpoint.

Covers bearer auth, request → build_recommendations arg pass-through, the happy
path shape, and the cold-start (no history) case. build_recommendations itself
is mocked — its logic is covered in test_server_recommend.py.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

# Settings() + SMSClient run at import time — patch env + Twilio first (mirrors
# test_server_chat.py).
_env = {
    "TWILIO_ACCOUNT_SID": "test",
    "TWILIO_AUTH_TOKEN": "test",
    "TWILIO_PHONE_NUMBER": "+10000000000",
    "ALLOWED_NUMBERS": '["+1234"]',
    "ABS_API_TOKEN": "test",
    "ABS_LIBRARY_ID": "test",
    "ATB_CWD": "/tmp",
    "ATB_API_TOKEN": "test-token",
}

with (
    patch.dict(os.environ, _env),
    patch("auto_torrent.server.sms.Client"),
    patch("auto_torrent.server.sms.RequestValidator"),
):
    from auto_torrent.server import app as app_module


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _post(body: dict, *, token: str | None = "test-token"):
    from httpx import ASGITransport, AsyncClient

    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/recommend", headers=headers, json=body)
        return r.status_code, (r.json() if r.status_code == 200 else None)


@pytest.mark.anyio
async def test_rejects_missing_bearer():
    status, _ = await _post({"profile_id": "p"}, token=None)
    assert status == 401


@pytest.mark.anyio
async def test_rejects_wrong_bearer():
    status, _ = await _post({"profile_id": "p"}, token="nope")
    assert status == 401


@pytest.mark.anyio
async def test_happy_path_returns_recommendations():
    async def fake_build(profile_id, finished, **kw):
        return [
            {
                "title": "Project Hail Mary",
                "author": "Andy Weir",
                "reason": "Because you liked The Martian",
                "cover_url": "https://m.media-amazon.com/x._SL500_.jpg",
                "narrators": ["Ray Porter"],
            }
        ]

    with patch.object(app_module, "build_recommendations", fake_build):
        status, body = await _post(
            {"profile_id": "p1", "finished": [{"title": "The Martian", "author": "Andy Weir"}]}
        )
    assert status == 200
    assert body["recommendations"][0]["title"] == "Project Hail Mary"
    assert body["recommendations"][0]["cover_url"].endswith("_SL500_.jpg")


@pytest.mark.anyio
async def test_passes_request_through_to_builder():
    captured: dict = {}

    async def fake_build(profile_id, finished, *, exclude, n, refresh, cache):
        captured.update(
            profile_id=profile_id, finished=finished, exclude=exclude, refresh=refresh
        )
        return []

    with patch.object(app_module, "build_recommendations", fake_build):
        await _post(
            {
                "profile_id": "rafay",
                "finished": [{"title": "Dune", "author": "Frank Herbert"}],
                "exclude": ["Already Owned"],
                "refresh": True,
            }
        )
    assert captured["profile_id"] == "rafay"
    assert captured["finished"] == [{"title": "Dune", "author": "Frank Herbert"}]
    assert captured["exclude"] == ["Already Owned"]
    assert captured["refresh"] is True


@pytest.mark.anyio
async def test_cold_start_no_history_allowed():
    async def fake_build(profile_id, finished, **kw):
        assert finished == []
        return []

    with patch.object(app_module, "build_recommendations", fake_build):
        status, body = await _post({"profile_id": "new-user"})
    assert status == 200
    assert body["recommendations"] == []
