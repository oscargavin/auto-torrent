import json
import time

import pytest

from auto_torrent.server.jobs.events import EventLog
from auto_torrent.server.threads.store import ThreadStore, option_from_payload
from auto_torrent.server.threads.types import (
    Message,
    MessageKind,
    ThreadStatus,
)


@pytest.fixture
def log(redis):
    return EventLog(redis, prefix="thread")


@pytest.fixture
def store(redis, log):
    return ThreadStore(redis, log, state_ttl_s=7 * 24 * 3600)


async def _events(redis, thread_id: str) -> list[tuple[str, dict]]:
    entries = await redis.xrange(f"thread:{thread_id}:events")
    return [(fields["type"], json.loads(fields["data"])) for _id, fields in entries]


async def test_create_starts_idle(store):
    t = await store.create("p1")
    assert t.status is ThreadStatus.idle
    assert t.profile_id == "p1"


async def test_thread_stream_is_separate_from_job_stream(store, redis):
    """The whole reason EventLog grew a prefix: a shared key would expire the
    conversation when the first job inside it reached a terminal event."""
    t = await store.create("p1")
    await store.append(t.id, Message.new(t.id, MessageKind.user, text="dune"))
    assert await redis.exists(f"thread:{t.id}:events")
    assert not await redis.exists(f"job:{t.id}:events")


async def test_append_publishes_and_persists(store, redis):
    t = await store.create("p1")
    await store.append(t.id, Message.new(t.id, MessageKind.user, text="dune"))
    msgs = await store.messages(t.id)
    assert [m.text for m in msgs] == ["dune"]
    assert [ty for ty, _ in await _events(redis, t.id)] == ["message"]


async def test_title_is_set_once_from_first_user_message(store):
    t = await store.create("p1")
    await store.append(t.id, Message.new(t.id, MessageKind.user, text="dune"))
    await store.append(t.id, Message.new(t.id, MessageKind.user, text="no, foundation"))
    assert (await store.get(t.id)).title == "dune"


async def test_history_drops_job_messages(store):
    t = await store.create("p1")
    await store.append(t.id, Message.new(t.id, MessageKind.user, text="dune"))
    await store.append(t.id, Message.new(t.id, MessageKind.job, job_id="j1"))
    await store.append(t.id, Message.new(t.id, MessageKind.assistant, text="found it"))
    assert [m.kind for m in await store.history_for_agent(t.id)] == [
        MessageKind.user,
        MessageKind.assistant,
    ]


async def test_resolve_choice_marks_and_republishes(store):
    t = await store.create("p1")
    msg = await store.append(
        t.id,
        Message.new(
            t.id,
            MessageKind.choice,
            options=[option_from_payload(0, {"title": "A"}), option_from_payload(1, {"title": "B"})],
        ),
    )
    resolved = await store.resolve_choice(t.id, msg.id, 1)
    assert resolved is not None and resolved.chosen_index == 1
    # Persisted, not just returned.
    stored = [m for m in await store.messages(t.id) if m.id == msg.id][0]
    assert stored.chosen_index == 1


async def test_resolve_choice_is_single_use(store):
    """A second tap must not start a second download."""
    t = await store.create("p1")
    msg = await store.append(
        t.id,
        Message.new(t.id, MessageKind.choice, options=[option_from_payload(0, {"title": "A"})]),
    )
    assert await store.resolve_choice(t.id, msg.id, 0) is not None
    assert await store.resolve_choice(t.id, msg.id, 0) is None


async def test_resolve_choice_rejects_unknown_index(store):
    t = await store.create("p1")
    msg = await store.append(
        t.id,
        Message.new(t.id, MessageKind.choice, options=[option_from_payload(0, {"title": "A"})]),
    )
    assert await store.resolve_choice(t.id, msg.id, 7) is None


async def test_resolve_choice_rejects_non_choice_message(store):
    t = await store.create("p1")
    msg = await store.append(t.id, Message.new(t.id, MessageKind.user, text="dune"))
    assert await store.resolve_choice(t.id, msg.id, 0) is None


async def test_pending_roundtrip_and_clear(store):
    t = await store.create("p1")
    await store.set_pending(t.id, [{"title": "A", "magnet": "magnet:?xt=1"}])
    assert (await store.take_pending(t.id))[0]["magnet"] == "magnet:?xt=1"
    await store.clear_pending(t.id)
    assert await store.take_pending(t.id) == []


async def test_magnets_never_reach_the_transcript(store):
    """The reason ChoiceOption exists as a separate shape from the agent payload."""
    opt = option_from_payload(0, {"title": "A", "magnet": "magnet:?xt=secret"})
    assert "magnet" not in opt.model_dump()
    assert "secret" not in opt.model_dump_json()


async def test_working_thread_unsticks_after_its_worker_dies(store, redis):
    t = await store.create("p1")
    await store.set_status(t.id, ThreadStatus.working)
    # Backdate past STUCK_AFTER_S — the worker was SIGKILLed and nothing else
    # will ever move this thread.
    await redis.hset(f"thread:{t.id}", "updated_at", str(time.time() - 3600))
    assert (await store.get(t.id)).status is ThreadStatus.idle


async def test_recent_working_thread_is_left_alone(store):
    t = await store.create("p1")
    await store.set_status(t.id, ThreadStatus.working)
    assert (await store.get(t.id)).status is ThreadStatus.working


async def test_list_for_profile_is_most_recent_first(store):
    a = await store.create("p1")
    b = await store.create("p1")
    await store.set_status(a.id, ThreadStatus.working)  # bumps updated_at
    assert [t.id for t in await store.list_for_profile("p1")][0] == a.id
    assert b.id in [t.id for t in await store.list_for_profile("p1")]
