"""What the agent is told happened.

The case driving all of this: someone browses. They ask for suggestions, then
say "the second one, but more like that" — which only resolves if the agent can
still see the list it offered.
"""

from auto_torrent.server.threads.history import render_history
from auto_torrent.server.threads.store import option_from_payload
from auto_torrent.server.threads.types import Message, MessageKind


def _msg(kind: MessageKind, **kw) -> Message:
    return Message.new("t1", kind, **kw)


def _choice(chosen: int | None = None) -> Message:
    m = _msg(
        MessageKind.choice,
        text="All have that survival energy — which?",
        options=[
            option_from_payload(0, {"title": "Project Hail Mary", "author": "Andy Weir", "note": "same author"}),
            option_from_payload(1, {"title": "All Systems Red", "author": "Martha Wells", "note": "wry robot"}),
        ],
    )
    return m.model_copy(update={"chosen_index": chosen})


def test_options_reach_the_agent():
    """Without this a follow-up naming a position has nothing to resolve
    against — the agent could see that it asked, but not what it offered."""
    rendered = render_history([_choice()])
    assert len(rendered) == 1
    role, text = rendered[0]
    assert role == "assistant"
    assert "1. Project Hail Mary by Andy Weir — same author" in text
    assert "2. All Systems Red by Martha Wells — wry robot" in text


def test_options_are_numbered_from_one():
    """How someone refers to them out loud. The wire index stays 0-based."""
    _role, text = render_history([_choice()])[0]
    assert "1. Project Hail Mary" in text
    assert "0." not in text


def test_the_pick_is_marked():
    _role, text = render_history([_choice(chosen=1)])[0]
    assert "All Systems Red by Martha Wells — wry robot [they chose this]" in text
    assert "didn't pick" not in text


def test_an_unanswered_list_says_so():
    """'something else' has to read as 'not those', not as a fresh request."""
    _role, text = render_history([_choice()])[0]
    assert "(they didn't pick any of these)" in text


def test_the_request_being_answered_is_not_also_history():
    history = render_history(
        [
            _msg(MessageKind.user, text="something like the martian"),
            _msg(MessageKind.assistant, text="Here are a few."),
            _msg(MessageKind.user, text="more like the second"),
        ],
        exclude_last_text="more like the second",
    )
    assert [t for _r, t in history] == ["something like the martian", "Here are a few."]


def test_an_identical_earlier_message_survives():
    """Only the trailing entry is the request; the same words said earlier are
    genuinely part of the conversation."""
    history = render_history(
        [
            _msg(MessageKind.user, text="dune"),
            _msg(MessageKind.assistant, text="Already in your library."),
            _msg(MessageKind.user, text="dune"),
        ],
        exclude_last_text="dune",
    )
    assert [t for _r, t in history] == ["dune", "Already in your library."]


def test_downloads_are_not_context():
    history = render_history(
        [
            _msg(MessageKind.user, text="dune"),
            _msg(MessageKind.job, job_id="j1"),
        ]
    )
    assert [t for _r, t in history] == ["dune"]


def test_empty_messages_are_dropped():
    history = render_history([_msg(MessageKind.assistant, text="")])
    assert history == []


def test_roles_map_to_speakers():
    history = render_history(
        [
            _msg(MessageKind.user, text="hello"),
            _msg(MessageKind.assistant, text="hi"),
        ]
    )
    assert history == [("user", "hello"), ("assistant", "hi")]
