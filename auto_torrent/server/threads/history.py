"""Turning a transcript into what the agent is told happened.

This is the difference between a conversation and a sequence of unrelated
requests. The naive version — take each message's `text` — silently drops the
most referable thing on screen: the options themselves. The agent could see
that it had asked "which of these?" but not what "these" were, so a follow-up
naming a position ("the second one", "that first one but shorter") had nothing
to resolve against.
"""

from __future__ import annotations

from typing import Any

from .types import Message, MessageKind


def render_history(
    messages: list[Message],
    *,
    exclude_last_text: str | None = None,
    jobs: dict[str, Any] | None = None,
) -> list[tuple[str, str]]:
    """(role, text) pairs, oldest first.

    `exclude_last_text` drops the trailing user message when it is the request
    being answered — it belongs in the prompt as the question, not in the
    history as context. Matched only against the last entry, so an identical
    message from earlier in the conversation still counts as history.
    """
    items = list(messages)
    if (
        exclude_last_text is not None
        and items
        and items[-1].kind is MessageKind.user
        and items[-1].text == exclude_last_text
    ):
        items.pop()

    out: list[tuple[str, str]] = []
    for m in items:
        if m.kind is MessageKind.job:
            # The download card. Dropped entirely before, so "I don't see it
            # in my library" got an agent that did not know it had started a
            # download, let alone that it had failed.
            job = (jobs or {}).get(m.job_id or "")
            if job is not None:
                out.append(("assistant", f"[download] {describe_job(job)}"))
            continue
        if m.kind is MessageKind.choice:
            out.append(("assistant", _render_choice(m)))
            continue
        if not m.text:
            continue
        out.append(("user" if m.kind is MessageKind.user else "assistant", m.text))
    return out


def describe_job(job: Any) -> str:
    """One line on where a download stands, as the user's card shows it."""
    title = job.picked_title or job.query
    status = getattr(job.status, "value", job.status)
    if status == "succeeded":
        state = "already in the library, nothing downloaded" if job.already_had else "done, in the library"
    elif status == "failed":
        state = f"failed: {job.error or 'unknown error'}"
    elif status == "cancelled":
        state = "cancelled by the user"
    else:
        state = "still downloading or importing"
    edition = ", ".join(p for p in (job.picked_narrator, job.picked_format) if p)
    return f"“{title}”" + (f" ({edition})" if edition else "") + f" — {state}"


def _render_choice(message: Message) -> str:
    """A question plus the list it offered, numbered as the user saw it.

    Numbered from 1 because that is how someone refers to them out loud; the
    index stays 0-based on the wire. Which one they picked is marked, so
    "something else" reads as "not that one" rather than as a fresh request.
    """
    lines = [message.text or "I offered a few options:"]
    for option in message.options:
        parts = [option.title]
        if option.author:
            parts.append(f"by {option.author}")
        if option.note:
            parts.append(f"— {option.note}")
        chosen = " [they chose this]" if message.chosen_index == option.index else ""
        lines.append(f"  {option.index + 1}. {' '.join(parts)}{chosen}")
    if message.chosen_index is None:
        lines.append("  (they didn't pick any of these)")
    return "\n".join(lines)
