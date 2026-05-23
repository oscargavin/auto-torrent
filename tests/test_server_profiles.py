"""Behavioural tests for the /profiles endpoints (family account management).

Covers app-secret auth, create → list round-trip (the API-key token is surfaced
once), delete, and sync seeding non-admin users idempotently. ABS network calls
are mocked; the JSON store writes to a tmp file.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

# Settings() runs at import time — patch env (incl. the profiles secret) and the
# Twilio client/validator before the app module loads.
_env = {
    "TWILIO_ACCOUNT_SID": "test",
    "TWILIO_AUTH_TOKEN": "test",
    "TWILIO_PHONE_NUMBER": "+10000000000",
    "ALLOWED_NUMBERS": '["+1234"]',
    "ABS_API_TOKEN": "test",
    "ABS_LIBRARY_ID": "test",
    "ATB_CWD": "/tmp",
    "ATB_API_TOKEN": "test-token",
    "PROFILES_APP_SECRET": "profiles-secret",
}

with (
    patch.dict(os.environ, _env),
    patch("auto_torrent.server.sms.Client"),
    patch("auto_torrent.server.sms.RequestValidator"),
):
    from auto_torrent.server import app as app_module
    from auto_torrent.server.profiles import ProfileStore

_AUTH = {"Authorization": "Bearer profiles-secret"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A ProfileStore on a tmp file with a mocked ABS client, swapped into the
    app module. The secret is forced on regardless of import-time settings."""
    s = ProfileStore(app_module.settings)
    s._path = tmp_path / "profiles.json"
    s._abs = AsyncMock()
    monkeypatch.setattr(app_module, "profile_store", s)
    monkeypatch.setattr(app_module.settings, "profiles_app_secret", "profiles-secret")
    return s


def _client():
    from httpx import ASGITransport, AsyncClient

    return AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://test")


@pytest.mark.anyio
async def test_rejects_missing_or_wrong_secret(store):
    async with _client() as ac:
        assert (await ac.get("/profiles")).status_code == 401
        assert (
            await ac.get("/profiles", headers={"Authorization": "Bearer nope"})
        ).status_code == 401


@pytest.mark.anyio
async def test_create_then_list_surfaces_token_once(store):
    store._abs.create_user.return_value = {"id": "u1", "username": "tom", "type": "user"}
    store._abs.create_api_key.return_value = {"apiKey": "tok-1", "id": "k1"}

    async with _client() as ac:
        r = await ac.post("/profiles", headers=_AUTH, json={"name": "Tom"})
        assert r.status_code == 200
        profile = r.json()["profile"]
        assert profile["name"] == "Tom"
        assert profile["token"] == "tok-1"
        assert profile["color"].startswith("#")
        assert "absKeyId" not in profile  # server-internal field stays server-side

        r = await ac.get("/profiles", headers=_AUTH)
        listed = r.json()["profiles"]
        assert [p["name"] for p in listed] == ["Tom"]
        assert listed[0]["token"] == "tok-1"

    store._abs.create_user.assert_awaited_once()
    store._abs.create_api_key.assert_awaited_once()


@pytest.mark.anyio
async def test_create_rejects_blank_name(store):
    async with _client() as ac:
        assert (await ac.post("/profiles", headers=_AUTH, json={"name": "  "})).status_code == 400


@pytest.mark.anyio
async def test_delete_removes_profile_and_user(store):
    store._abs.create_user.return_value = {"id": "u1", "username": "tom"}
    store._abs.create_api_key.return_value = {"apiKey": "tok-1", "id": "k1"}
    store._abs.delete_user.return_value = None

    async with _client() as ac:
        await ac.post("/profiles", headers=_AUTH, json={"name": "Tom"})

        assert (await ac.delete("/profiles/u1", headers=_AUTH)).status_code == 200
        assert (await ac.get("/profiles", headers=_AUTH)).json()["profiles"] == []
        assert (await ac.delete("/profiles/missing", headers=_AUTH)).status_code == 404

    store._abs.delete_user.assert_awaited_once_with("u1")


@pytest.mark.anyio
async def test_sync_seeds_non_admin_users_idempotently(store):
    store._abs.list_users.return_value = [
        {"id": "r", "username": "root", "type": "root"},
        {"id": "m", "username": "mum", "type": "user"},
        {"id": "a", "username": "angela", "type": "user"},
    ]
    store._abs.create_api_key.side_effect = [
        {"apiKey": "tok-m", "id": "km"},
        {"apiKey": "tok-a", "id": "ka"},
    ]

    async with _client() as ac:
        r = await ac.post("/profiles/sync", headers=_AUTH)
        assert r.status_code == 200
        assert sorted(p["name"] for p in r.json()["profiles"]) == ["angela", "mum"]  # root skipped

        # A second sync must mint nothing new.
        store._abs.create_api_key.side_effect = AssertionError("must not re-mint")
        r = await ac.post("/profiles/sync", headers=_AUTH)
        assert r.status_code == 200
        assert len(r.json()["profiles"]) == 2


@pytest.mark.anyio
async def test_store_create_generates_unique_username(store):
    """Two profiles with the same display name get distinct ABS usernames."""
    store._abs.create_user.side_effect = lambda username: {"id": username, "username": username}
    store._abs.create_api_key.side_effect = lambda user_id, name: {"apiKey": f"tok-{user_id}", "id": f"k-{user_id}"}

    first = await store.create("Sam")
    second = await store.create("Sam")
    assert first["username"] == "sam"
    assert second["username"] != "sam"
    assert second["username"].startswith("sam-")
