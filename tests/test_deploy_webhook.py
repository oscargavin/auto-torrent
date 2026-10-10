"""The deploy webhook starts the deploy as its own unit, and only for main.

Observed on basil, 9-10 October 2026: every auto-deploy logged "Deploy
failed". The script ran as a child of atb-server, so restarting atb-server
killed it before it restarted the worker, which kept running old code.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

with patch.dict(os.environ, {"ABS_API_TOKEN": "t", "ABS_LIBRARY_ID": "t", "ATB_CWD": "/tmp"}):
    from auto_torrent.server import app as app_module


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def started(monkeypatch):
    calls: list = []

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*argv, **_kw):
        calls.append(argv)
        return Proc()

    monkeypatch.setattr(app_module.settings, "github_webhook_secret", "")
    monkeypatch.setattr(app_module.asyncio, "create_subprocess_exec", fake_exec)
    return calls


async def _post(event: str, ref: str):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://t") as ac:
        return await ac.post(
            "/webhook/github", content=json.dumps({"ref": ref}), headers={"X-GitHub-Event": event}
        )


@pytest.mark.anyio
async def test_a_push_to_main_starts_the_deploy_unit(started):
    r = await _post("push", "refs/heads/main")
    assert r.status_code == 200
    assert started[0][:3] == ("systemd-run", "--user", "--unit=atb-deploy")
    assert started[0][-1].endswith("deploy/deploy.sh")


@pytest.mark.anyio
async def test_other_events_and_branches_do_not_deploy(started):
    assert (await _post("ping", "refs/heads/main")).status_code == 204
    assert (await _post("push", "refs/heads/feature")).status_code == 204
    assert started == []
