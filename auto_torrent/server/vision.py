"""Cover-image OCR via Sonnet vision (claude_agent_sdk, subscription auth)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import urllib.request

from claude_agent_sdk import ClaudeAgentOptions, query

logger = logging.getLogger("atb.vision")

VISION_TIMEOUT_S = 30
VISION_PROMPT = (
    "This is an audiobook cover image. Read any text printed on it and return "
    "ONLY a JSON object: "
    '{"title": <string|null>, "author": <string|null>, "narrator": <string|null>}. '
    "Use null for fields not visible. Do not include any prose."
)


def _fetch_image(url: str) -> tuple[str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = resp.read()
        ctype = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    return base64.b64encode(data).decode(), ctype


def _extract_json(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else text
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


async def _query_vision(b64: str, media_type: str) -> str:
    async def stream():
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": VISION_PROMPT},
                ],
            },
            "parent_tool_use_id": None,
            "session_id": "",
        }

    result = ""
    async for msg in query(
        prompt=stream(),
        options=ClaudeAgentOptions(model="claude-sonnet-4-6", max_turns=1),
    ):
        if getattr(msg, "subtype", None) == "success":
            result = msg.result or ""
    return result


async def analyze_cover(cover_url: str) -> dict:
    """Best-effort. Returns {} on any failure so callers can continue without."""
    if not cover_url:
        return {}
    try:
        b64, media_type = await asyncio.to_thread(_fetch_image, cover_url)
    except Exception as e:
        logger.warning("Cover fetch failed for %s: %s", cover_url, e)
        return {}

    try:
        text = await asyncio.wait_for(_query_vision(b64, media_type), timeout=VISION_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("Cover vision timed out for %s", cover_url)
        return {}
    except Exception as e:
        logger.warning("Cover vision failed for %s: %s", cover_url, e)
        return {}

    parsed = _extract_json(text)
    return {k: parsed.get(k) for k in ("title", "author", "narrator") if parsed.get(k)}
