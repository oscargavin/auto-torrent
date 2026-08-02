"""Tests for the recap module and the POST /recap endpoint.

The module tests cover the prompt/key/cache logic with generate() mocked; the
route tests cover bearer auth and the null-recap (unknown book) contract.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from auto_torrent.server.recap import _prompt, build_recap, recap_key
from auto_torrent.server.recommend import RecCache

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


# --- module ---------------------------------------------------------------


def test_prompt_places_the_listener():
    p = _prompt("Dune", "Frank Herbert", 11, "The Sietch", 48)
    assert '"Dune" by Frank Herbert' in p
    assert "chapter 12 of 48" in p  # 0-based index → human chapter number
    assert 'titled "The Sietch"' in p
    assert "up to but not including" in p


def test_prompt_without_optionals():
    p = _prompt("Dune", "", 0, "", 0)
    assert "chapter 1" in p
    assert "of 0" not in p
    assert " by " not in p


def test_recap_key_is_stable_and_chapter_scoped():
    a = recap_key("Dune", "Frank Herbert", 3)
    assert a == recap_key("  dune ", "FRANK HERBERT", 3)  # case/space-insensitive
    assert a != recap_key("Dune", "Frank Herbert", 4)


@pytest.mark.anyio
async def test_build_recap_caches_success(tmp_path):
    cache = RecCache(tmp_path / "recaps.json")
    calls = []

    async def fake_generate(*a, **kw):
        calls.append(a)
        return "Paul has joined the Fremen."

    with patch("auto_torrent.server.recap.generate", fake_generate):
        first = await build_recap("Dune", "Frank Herbert", 11, cache=cache)
        second = await build_recap("Dune", "Frank Herbert", 11, cache=cache)
    assert first == second == "Paul has joined the Fremen."
    assert len(calls) == 1  # second hit came from the cache


@pytest.mark.anyio
async def test_build_recap_caches_refusal(tmp_path):
    cache = RecCache(tmp_path / "recaps.json")
    calls = []

    async def fake_generate(*a, **kw):
        calls.append(a)
        return None

    with patch("auto_torrent.server.recap.generate", fake_generate):
        first = await build_recap("Obscure Fanfic", "", 5, cache=cache)
        second = await build_recap("Obscure Fanfic", "", 5, cache=cache)
    assert first is None and second is None
    assert len(calls) == 1  # the refusal is cached too — no re-run per open


# --- route ----------------------------------------------------------------


async def _post(body: dict, *, token: str | None = "test-token"):
    from httpx import ASGITransport, AsyncClient

    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    transport = ASGITransport(app=app_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/recap", headers=headers, json=body)
        return r.status_code, (r.json() if r.status_code == 200 else None)


@pytest.mark.anyio
async def test_route_rejects_missing_bearer():
    status, _ = await _post({"title": "Dune"}, token=None)
    assert status == 401


@pytest.mark.anyio
async def test_route_returns_recap():
    async def fake_build(title, author, chapter_index, chapter_title, chapters_total, *, cache):
        assert title == "Dune"
        assert chapter_index == 11
        return "Paul has joined the Fremen."

    with patch.object(app_module, "build_recap", fake_build):
        status, body = await _post(
            {
                "title": "Dune",
                "author": "Frank Herbert",
                "chapter_index": 11,
                "chapter_title": "The Sietch",
                "chapters_total": 48,
            }
        )
    assert status == 200
    assert body == {"recap": "Paul has joined the Fremen."}


@pytest.mark.anyio
async def test_route_null_recap_for_unknown_book():
    async def fake_build(*a, **kw):
        return None

    with patch.object(app_module, "build_recap", fake_build):
        status, body = await _post({"title": "Obscure Fanfic"})
    assert status == 200
    assert body == {"recap": None}
