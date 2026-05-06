"""Per-phone conversation state (pending search results / suggestions, 10-min TTL)."""

from __future__ import annotations

import time

CONVERSATION_TTL_S = 600

_conversations: dict[str, dict] = {}


def _expired(conv: dict) -> bool:
    return time.time() - conv["ts"] > CONVERSATION_TTL_S


def store_pending_results(phone: str, results: list[dict]) -> None:
    _conversations[phone] = {"pending_results": list(results), "ts": time.time()}


def get_pending_result(phone: str, index: int) -> dict | None:
    """Get a pending result by 1-based index. Returns None if expired/invalid."""
    conv = _conversations.get(phone)
    if not conv:
        return None
    if _expired(conv):
        _conversations.pop(phone, None)
        return None
    results = conv.get("pending_results") or []
    if index < 1 or index > len(results):
        return None
    return results[index - 1]


def get_pending_options(phone: str) -> list[dict]:
    """Return pending options if any (non-expired). Used to inform the agent."""
    conv = _conversations.get(phone)
    if not conv or _expired(conv):
        return []
    return conv.get("pending_results") or []


def has_pending_results(phone: str) -> bool:
    return bool(get_pending_options(phone))


def clear_conversation(phone: str) -> None:
    _conversations.pop(phone, None)
