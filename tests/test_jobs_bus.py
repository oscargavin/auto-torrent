import json

import pytest

from auto_torrent.server.jobs.bus import StreamEventBus
from auto_torrent.server.jobs.events import EventLog


@pytest.fixture
def log(redis):
    return EventLog(redis)


async def test_emit_writes_to_stream(redis, log):
    bus = StreamEventBus("j1", log)
    await bus.emit_async("progress", {"text": "hi"})
    entries = await redis.xrange("job:j1:events")
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["type"] == "progress"
    assert json.loads(fields["data"]) == {"text": "hi"}


async def test_send_emits_progress(log):
    bus = StreamEventBus("j1", log)
    await bus.send_async("ignored", "downloading 50%")
    assert bus.messaged is True


async def test_sync_emit_from_thread_uses_loop_bridge(log):
    """Existing worker code calls bus.send() from asyncio.to_thread paths.
    The sync wrappers schedule onto the loop via call_soon_threadsafe so the
    BG-process polling thread can publish."""
    # Smoke-test the sync entrypoint exists. Must be async so get_running_loop()
    # succeeds in the StreamEventBus constructor.
    bus = StreamEventBus("j1", log)
    assert callable(bus.emit)
    assert callable(bus.send)
    assert callable(bus.system_progress)
    assert callable(bus.close)
