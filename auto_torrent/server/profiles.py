"""Family "profiles" = ABS user accounts surfaced to the Bookkeeper app.

Each profile is an ABS user plus a long-lived API key. The app fetches the list
(GET /profiles) and authenticates as a person using that user's key — so
listening progress, bookmarks and playback settings are per-person. Creating a
profile (POST /profiles) mints the ABS user + key here, behind the app secret,
so the app never needs the ABS admin token.

API-key tokens are only shown once by ABS, so they're persisted to a JSON file;
that file is the source of truth for the app-facing list. The ABS admin key
never leaves the server.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from pathlib import Path

import httpx

from .audiobookshelf import ABSClient
from .settings import Settings

# Avatar accent colours for the picker — the app's greens plus warm contrasts so
# family members are visually distinct. Assigned round-robin on creation.
_COLORS = [
    "#2e9e6b",
    "#5fd6a0",
    "#e0a458",
    "#d96c6c",
    "#7d8ce0",
    "#c084d9",
    "#4db6ac",
    "#e88fb0",
]

_KEY_NAME_PREFIX = "bookkeeper-profile"

# Avatars are DiceBear (https://www.dicebear.com) — the app renders
# `https://api.dicebear.com/9.x/{style}/png?seed={seed}`. We store just the
# {style, seed} so it syncs as two short strings; the allowlist keeps the app
# from being handed an unknown style.
ALLOWED_AVATAR_STYLES = {
    "adventurer",
    "fun-emoji",
    "bottts",
    "lorelei",
    "big-smile",
    "thumbs",
    "micah",
    "notionists",
    "avataaars",
    "big-ears",
    "pixel-art",
    "croodles",
}
DEFAULT_AVATAR_STYLE = "adventurer"


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "user"


def _default_avatar(seed: str) -> dict:
    return {"style": DEFAULT_AVATAR_STYLE, "seed": seed}


def public_view(profile: dict) -> dict:
    """The fields the app needs. Drops server-internal bookkeeping (absKeyId)."""
    return {
        "id": profile["id"],
        "name": profile["name"],
        "color": profile["color"],
        "token": profile["token"],
        "avatar": profile.get("avatar")
        or _default_avatar(profile.get("username") or profile["name"]),
    }


class ProfileStore:
    def __init__(self, settings: Settings) -> None:
        self._abs = ABSClient(settings)
        self._path = Path(settings.profiles_store_path)
        self._lock = asyncio.Lock()

    def _read(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return []

    def _write(self, profiles: list[dict]) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(profiles, indent=2))
        tmp.replace(self._path)

    def _record(self, *, user_id: str, name: str, username: str, key: dict, index: int) -> dict:
        return {
            "id": user_id,
            "name": name,
            "username": username,
            "color": _COLORS[index % len(_COLORS)],
            "token": key["apiKey"],
            "absKeyId": key["id"],
            "avatar": _default_avatar(username),
        }

    async def list(self) -> list[dict]:
        async with self._lock:
            return self._read()

    async def create(self, name: str) -> dict:
        name = name.strip()
        if not name:
            raise ValueError("name required")
        async with self._lock:
            profiles = self._read()
            base = _slugify(name)
            existing = {p["username"] for p in profiles}
            username = base
            while username in existing:
                username = f"{base}-{secrets.token_hex(2)}"
            user = await self._abs.create_user(username)
            key = await self._abs.create_api_key(
                user["id"], f"{_KEY_NAME_PREFIX}:{username}"
            )
            profile = self._record(
                user_id=user["id"],
                name=name,
                username=username,
                key=key,
                index=len(profiles),
            )
            profiles.append(profile)
            self._write(profiles)
            return profile

    async def delete(self, profile_id: str) -> bool:
        async with self._lock:
            profiles = self._read()
            if not any(p["id"] == profile_id for p in profiles):
                return False
            # Deleting the ABS user also drops its API keys. Tolerate an already
            # gone user (404) so a half-deleted record can still be cleaned up.
            try:
                await self._abs.delete_user(profile_id)
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404:
                    raise
            self._write([p for p in profiles if p["id"] != profile_id])
            return True

    async def update(
        self, profile_id: str, *, avatar: dict | None = None, color: str | None = None
    ) -> dict | None:
        async with self._lock:
            profiles = self._read()
            target = next((p for p in profiles if p["id"] == profile_id), None)
            if target is None:
                return None
            if avatar is not None:
                target["avatar"] = avatar
            if color is not None:
                target["color"] = color
            self._write(profiles)
            return target

    async def sync(self) -> list[dict]:
        """Ensure every non-admin ABS user has a profile + key, and backfill a
        default avatar on any record missing one. Seeds accounts that predate
        this feature (mum/rafay/angela) or were added in the ABS web UI.
        Idempotent."""
        async with self._lock:
            profiles = self._read()
            known = {p["id"] for p in profiles}
            for user in await self._abs.list_users():
                if user.get("type") in ("root", "admin") or user["id"] in known:
                    continue
                key = await self._abs.create_api_key(
                    user["id"], f"{_KEY_NAME_PREFIX}:{user['username']}"
                )
                profiles.append(
                    self._record(
                        user_id=user["id"],
                        name=user["username"],
                        username=user["username"],
                        key=key,
                        index=len(profiles),
                    )
                )
            for p in profiles:
                if not p.get("avatar"):
                    p["avatar"] = _default_avatar(p.get("username") or p["name"])
            self._write(profiles)
            return profiles
