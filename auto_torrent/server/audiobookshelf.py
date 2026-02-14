import httpx

from .settings import Settings


class ABSClient:
    def __init__(self, settings: Settings) -> None:
        self._base = settings.abs_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {settings.abs_api_token}"}

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
