"""FastAPI app — the Bookkeeper chat, profiles, recaps and recommendations."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel

from .profiles import ALLOWED_AVATAR_STYLES, ProfileStore, public_view
from .recap import RECAP_CACHE_TTL_S, build_recap
from .recommend import DEFAULT_N, RecCache, build_recommendations
from .settings import Settings
from .audiobookshelf import ABSClient
from .covers import backfill_covers

logger = logging.getLogger("atb.server")

settings = Settings()
profile_store = ProfileStore(settings)
rec_cache = RecCache(settings.rec_cache_path)
recap_cache = RecCache(settings.recap_cache_path, ttl_s=RECAP_CACHE_TTL_S)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    from redis.asyncio import Redis

    from .jobs.api import build_router
    from .runtime import build_stores, turn_queue
    from .threads.api import build_router as build_threads_router

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logger.info("atb-server starting on port %d", settings.port)

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    arq_pool, enqueue_turn = await turn_queue(settings)
    stores = build_stores(redis, settings)

    _app.include_router(build_router(store=stores.jobs, log=stores.log))
    _app.include_router(
        build_threads_router(
            threads=stores.threads, log=stores.thread_log, enqueue_turn=enqueue_turn
        )
    )

    try:
        yield
    finally:
        await arq_pool.close()
        await redis.aclose()


app = FastAPI(title="atb-server", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    if "server" in response.headers:
        del response.headers["server"]
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/github")
async def github_webhook(request: Request) -> Response:
    body = await request.body()

    secret = settings.github_webhook_secret
    if secret:
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return Response(status_code=403)

    # Only a push to main deploys. GitHub also sends ping and other events.
    if request.headers.get("X-GitHub-Event") != "push":
        return Response(status_code=204)
    try:
        ref = json.loads(body or b"{}").get("ref")
    except ValueError:
        return Response(status_code=400)
    if ref != "refs/heads/main":
        return Response(status_code=204)

    # Its own systemd unit, not a child process: the deploy restarts this
    # server, and a child would die with it before restarting the worker.
    # A deploy already running makes this fail, which is fine: it re-checks
    # origin/main before it finishes.
    proc = await asyncio.create_subprocess_exec(
        "systemd-run", "--user", "--unit=atb-deploy", "--collect", "--quiet",
        "/home/oscar/auto-torrent/deploy/deploy.sh",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode == 0:
        logger.info("deploy started (journalctl --user -u atb-deploy)")
    else:
        logger.info("deploy not started: %s", stderr.decode().strip())
    return Response(status_code=200)


def _require_bearer(authorization: str = Header("", alias="Authorization")) -> None:
    token = settings.atb_api_token
    if not token:
        raise HTTPException(status_code=503, detail="chat endpoint not configured")
    expected = f"Bearer {token}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.post("/covers/backfill")
async def covers_backfill(
    dry_run: bool = False,
    _: None = Depends(_require_bearer),
) -> dict:
    """Find artwork for library items that have none.

    The import path covers new books; this fixes what is already on the shelf,
    and anything a lookup misses today that a re-run catches later. Idempotent
    — an item that gained a cover simply isn't in the list any more.
    """
    return await backfill_covers(
        ABSClient(settings), settings.abs_library_id, dry_run=dry_run
    )


# --- Bookkeeper family profiles -----------------------------------------
#
# The mobile app manages family ABS accounts here using a shared app secret, so
# the app never holds the ABS admin token. Each profile is an ABS user + a
# long-lived API key; the app authenticates as a person by that key, giving
# per-user listening progress. See profiles.py.


def _require_profiles_secret(authorization: str = Header("", alias="Authorization")) -> None:
    secret = settings.profiles_app_secret
    if not secret:
        raise HTTPException(status_code=503, detail="profiles endpoint not configured")
    if not hmac.compare_digest(authorization, f"Bearer {secret}"):
        raise HTTPException(status_code=401, detail="unauthorized")


class CreateProfileRequest(BaseModel):
    name: str


@app.get("/profiles")
async def get_profiles(_: None = Depends(_require_profiles_secret)) -> dict:
    profiles = await profile_store.list()
    return {"profiles": [public_view(p) for p in profiles]}


@app.post("/profiles")
async def create_profile(
    req: CreateProfileRequest, _: None = Depends(_require_profiles_secret)
) -> dict:
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    if len(name) > 40:
        raise HTTPException(status_code=400, detail="name too long")
    try:
        profile = await profile_store.create(name)
    except httpx.HTTPStatusError as e:
        logger.warning("profile create failed: %s", e)
        raise HTTPException(status_code=502, detail="audiobookshelf rejected the request") from e
    return {"profile": public_view(profile)}


class AvatarSpec(BaseModel):
    style: str
    seed: str


class UpdateProfileRequest(BaseModel):
    avatar: AvatarSpec | None = None
    color: str | None = None


@app.patch("/profiles/{profile_id}")
async def update_profile(
    profile_id: str,
    req: UpdateProfileRequest,
    _: None = Depends(_require_profiles_secret),
) -> dict:
    avatar: dict | None = None
    if req.avatar is not None:
        if req.avatar.style not in ALLOWED_AVATAR_STYLES:
            raise HTTPException(status_code=400, detail="unknown avatar style")
        seed = req.avatar.seed.strip()[:64]
        if not seed:
            raise HTTPException(status_code=400, detail="seed required")
        avatar = {"style": req.avatar.style, "seed": seed}
    updated = await profile_store.update(profile_id, avatar=avatar, color=req.color)
    if updated is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return {"profile": public_view(updated)}


@app.delete("/profiles/{profile_id}")
async def delete_profile(
    profile_id: str, _: None = Depends(_require_profiles_secret)
) -> dict:
    try:
        removed = await profile_store.delete(profile_id)
    except httpx.HTTPStatusError as e:
        logger.warning("profile delete failed: %s", e)
        raise HTTPException(status_code=502, detail="audiobookshelf rejected the request") from e
    if not removed:
        raise HTTPException(status_code=404, detail="profile not found")
    return {"ok": True}


@app.post("/profiles/sync")
async def sync_profiles(_: None = Depends(_require_profiles_secret)) -> dict:
    """Seed profiles for any non-admin ABS user that doesn't have one yet."""
    try:
        profiles = await profile_store.sync()
    except httpx.HTTPStatusError as e:
        logger.warning("profile sync failed: %s", e)
        raise HTTPException(status_code=502, detail="audiobookshelf rejected the request") from e
    return {"profiles": [public_view(p) for p in profiles]}


# --- Bookkeeper recommendations -----------------------------------------
#
# Claude recommends audiobooks the profile doesn't own yet, hydrated server-side
# into rich cards (square Audible covers) so the app just renders + offers a
# "Request" that flows into the existing Get-a-book chat. Same atb_api_token
# bearer the app already uses for /chat/threads. See recommend.py.


class FinishedBook(BaseModel):
    title: str
    author: str = ""


class RecommendRequest(BaseModel):
    profile_id: str = "default"
    finished: list[FinishedBook] = []
    exclude: list[str] = []
    refresh: bool = False
    n: int = DEFAULT_N


# --- Bookkeeper "previously on…" recaps ----------------------------------
#
# A spoiler-safe recap of the story so far, for re-opening a book after weeks
# away. Claude recaps from its knowledge of the published work and refuses
# (recap: null) when it doesn't know the book — the app then shows only its
# local "last heard N days ago" line. Cached per (book, chapter); shared across
# profiles because a recap has no personal content. See recap.py.


class RecapRequest(BaseModel):
    title: str
    author: str = ""
    chapter_index: int = 0
    chapter_title: str = ""
    chapters_total: int = 0


@app.post("/recap")
async def recap(req: RecapRequest, _: None = Depends(_require_bearer)) -> dict:
    text = await build_recap(
        req.title,
        req.author,
        max(0, req.chapter_index),
        req.chapter_title,
        max(0, req.chapters_total),
        cache=recap_cache,
    )
    return {"recap": text}


@app.post("/recommend")
async def recommend(req: RecommendRequest, _: None = Depends(_require_bearer)) -> dict:
    finished = [{"title": b.title, "author": b.author} for b in req.finished]
    items = await build_recommendations(
        req.profile_id,
        finished,
        exclude=req.exclude,
        n=min(max(req.n, 1), 24),
        refresh=req.refresh,
        cache=rec_cache,
    )
    return {"recommendations": items}


def serve() -> None:
    import uvicorn

    uvicorn.run(
        "auto_torrent.server.app:app",
        host="127.0.0.1",
        port=settings.port,
        log_level="info",
    )
