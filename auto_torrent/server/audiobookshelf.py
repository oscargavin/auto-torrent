import secrets

import httpx

from .settings import Settings

# Permissions every app-created family profile gets: stream + download for
# offline use, but no library mutation and no explicit content. Mirrors the
# accounts already configured by hand (mum/rafay/angela).
PROFILE_PERMISSIONS = {
    "download": True,
    "update": False,
    "delete": False,
    "upload": False,
    "accessAllLibraries": True,
    "accessAllTags": True,
    "accessExplicitContent": False,
}


class ABSClient:
    def __init__(self, settings: Settings) -> None:
        self._base = settings.abs_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {settings.abs_api_token}"}

    async def list_users(self) -> list[dict]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base}/api/users", headers=self._headers, timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            return data["users"] if isinstance(data, dict) else data

    async def create_user(self, username: str) -> dict:
        """Create a standard ABS user with a random password (login is by API
        key, so the password is never used). Returns the created user object."""
        body = {
            "username": username,
            "password": secrets.token_urlsafe(24),
            "type": "user",
            # ABS defaults API-created users to inactive, and its file-download
            # routes require an ACTIVE account (canDownload && isActive) — so
            # without this the profile's offline downloads 403 despite the
            # download permission. Must be set at creation.
            "isActive": True,
            "permissions": PROFILE_PERMISSIONS,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base}/api/users", headers=self._headers, json=body, timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("user", data)

    async def delete_user(self, user_id: str) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{self._base}/api/users/{user_id}", headers=self._headers, timeout=15
            )
            resp.raise_for_status()

    async def create_api_key(self, user_id: str, name: str) -> dict:
        """Mint a long-lived (no-expiry) API key on behalf of a user. The raw
        token is at `apiKey` and is only returned by ABS this once."""
        body = {"name": name, "userId": user_id, "isActive": True}
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base}/api/api-keys",
                headers=self._headers,
                json=body,
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()["apiKey"]

    async def scan_library(self, library_id: str) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base}/api/libraries/{library_id}/scan",
                headers=self._headers,
                timeout=30,
            )
            resp.raise_for_status()

    async def get_libraries(self) -> list[dict]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base}/api/libraries",
                headers=self._headers,
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("libraries", [])
