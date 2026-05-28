import asyncio
import json

import pytest

from auto_torrent.server.jobs.events import EventLog


@pytest.fixture
def log(redis):
    return EventLog(redis, stream_max_len=1000)


async def test_publish_then_subscribe_from_zero(log):
    await log.publish("j1", "progress", {"text": "searching"})
    await log.publish("j1", "completed", {"title": "Dune"})

    received = []

    async def reader():
        async for event_id, event in log.subscribe("j1", since=None, idle_timeout_s=0.5):
            received.append((event_id, event))
            if event["type"] == "completed":
                break

    await asyncio.wait_for(reader(), timeout=2.0)
    assert [e[1]["type"] for e in received] == ["progress", "completed"]
    assert received[1][1]["data"] == {"title": "Dune"}


async def test_subscribe_with_last_event_id_only_returns_newer(log):
    e1 = await log.publish("j1", "progress", {"text": "a"})
    e2 = await log.publish("j1", "progress", {"text": "b"})

    received = []

    async def reader():
        async for event_id, event in log.subscribe("j1", since=e1, idle_timeout_s=0.5):
            received.append(event_id)
            break  # one event then exit

    await asyncio.wait_for(reader(), timeout=2.0)
    assert received == [e2]


async def test_subscribe_yields_keepalive_on_idle(log):
    received = []

    async def reader():
        async for event_id, event in log.subscribe("j1", since=None, idle_timeout_s=0.05):
            received.append(event)
            if len(received) >= 2:
                break

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.2)  # let two keepalives fire
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert all(e["type"] == "keepalive" for e in received)
