"""The agent reads how each past download went."""

from types import SimpleNamespace

from auto_torrent.server.threads.history import describe_job, render_history
from auto_torrent.server.threads.types import Message, MessageKind


def _job(**over):
    base = dict(
        id="j1", query="the fifth discipline", picked_title="The Fifth Discipline",
        picked_narrator="", picked_format="MP3", status="failed", already_had=False,
        error="Found it, but no one's sharing it right now.",
    )
    return SimpleNamespace(**{**base, **over})


def test_a_failed_download_says_why():
    """Observed live: "I don't see it in my lib" went to an agent that did not
    know a download had been tried, let alone that it had failed."""
    msgs = [
        Message.new("t", MessageKind.user, text="The fifth discipline Peter senge"),
        Message.new("t", MessageKind.job, job_id="j1"),
    ]
    out = render_history(msgs, jobs={"j1": _job()})
    assert out[-1] == (
        "assistant",
        "[download] “The Fifth Discipline” (MP3) — failed: Found it, but no one's sharing it right now.",
    )


def test_each_state_reads_as_the_card_shows_it():
    assert describe_job(_job(status="succeeded")).endswith("done, in the library")
    assert "already in the library" in describe_job(_job(status="succeeded", already_had=True))
    assert describe_job(_job(status="running")).endswith("still downloading or importing")
    assert describe_job(_job(status="cancelled")).endswith("cancelled by the user")


def test_a_card_whose_job_has_expired_is_left_out():
    msgs = [Message.new("t", MessageKind.job, job_id="gone")]
    assert render_history(msgs, jobs={}) == []
