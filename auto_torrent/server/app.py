"""FastAPI app — Twilio SMS webhook → agentic worker → deterministic poll."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .agent import AgentOutcome, run_agent
from .llm import clear_conversation, get_pending_options, get_pending_result
from .settings import Settings
from .sms import SMSClient
from .worker import _refresh_state, get_active_downloads, poll_and_finalise

logger = logging.getLogger("atb.server")

settings = Settings()
sms = SMSClient(settings)

# Strong references to detached /chat work tasks so they aren't garbage-collected
# if the client disconnects mid-download (app backgrounded/closed).
_background_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logger.info("atb-server starting on port %d", settings.port)
    yield


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


def _reconstruct_url(request: Request) -> str:
    """Reconstruct the public URL Twilio signed against (behind Cloudflare tunnel)."""
    proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return f"{proto}://{host}{request.url.path}"


def _twiml_response(message: str) -> Response:
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{message}</Message></Response>"
    )
    return Response(content=body, media_type="application/xml")


def _maybe_quick_pick(query: str, phone: str) -> dict | None:
    """If query is a bare digit, resolve it against pending results."""
    stripped = query.strip()
    if not stripped.isdigit():
        return None
    return get_pending_result(phone, int(stripped))


@app.post("/sms")
async def sms_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    Body: str = Form(""),
    From: str = Form(""),
    x_twilio_signature: str = Header("", alias="X-Twilio-Signature"),
) -> Response:
    form_data = dict(await request.form())
    url = _reconstruct_url(request)

    if not sms.validate_request(url, {k: str(v) for k, v in form_data.items()}, x_twilio_signature):
        logger.warning("Invalid Twilio signature from %s", From)
        return Response(status_code=403)

    if From not in settings.allowed_numbers:
        logger.warning("Unauthorized number: %s", From)
        return Response(status_code=403)

    query = Body.strip()
    logger.info("SMS from %s: %s", From, query)

    if not query:
        return _twiml_response("Send me a book title and I'll find it for you!")

    lower = query.lower()
    if lower == "help":
        return _twiml_response(
            "Text me a book title and I'll find the audiobook! "
            'Send "status" to check active downloads.'
        )

    if lower == "status":
        downloads = await get_active_downloads(settings)
        if not downloads:
            return _twiml_response("No active downloads.")
        lines = []
        for d in downloads:
            pct = int(d.get("progress", 0) * 100)
            lines.append(f'{d.get("title", "Unknown")} ({pct}%)')
        return _twiml_response("Downloading:\n" + "\n".join(lines))

    background_tasks.add_task(_handle_request, query, From)
    return _twiml_response("Got it!")


async def _handle_request(query: str, phone: str) -> None:
    """Resolve a digit-pick if pending, else hand to the agent. Then poll."""
    picked = _maybe_quick_pick(query, phone)

    if picked:
        clear_conversation(phone)
        await _commit_and_poll_from_pick(picked, phone)
        return

    pending = get_pending_options(phone)
    outcome = await run_agent(query, phone, settings, sms, pending_options=pending)

    if outcome.kind == "committed":
        clear_conversation(phone)
        try:
            await poll_and_finalise(
                download=outcome.download,
                fallbacks=outcome.fallbacks,
                display=outcome.display,
                author=outcome.author,
                title=outcome.title,
                phone=phone,
                settings=settings,
                sms=sms,
            )
        except Exception:
            logger.exception("poll_and_finalise crashed")
            sms.send(phone, "Something went wrong while downloading. Send the title again to retry?")
    elif outcome.kind in ("asked", "no_results"):
        # Agent already sent any user-facing SMS.
        pass
    else:
        logger.warning("agent error outcome: %s", outcome.message)
        sms.send(phone, "I had trouble with that — try again with the full title?")


async def _commit_and_poll_from_pick(picked: dict, phone: str) -> None:
    """User picked a number from a pending list. Start the download and poll."""
    from ..cli import _execute_download_bg

    title = picked.get("title") or "Unknown"
    author = picked.get("author") or ""
    magnet = picked.get("magnet") or ""
    if not magnet:
        sms.send(phone, "That option's gone stale, sorry — search again?")
        return

    bg_title = f"{title} - {author}" if author else title
    try:
        download = await asyncio.to_thread(_execute_download_bg, bg_title, magnet, None)
    except Exception:
        logger.exception("pick → BG start failed")
        sms.send(phone, "Couldn't start that download. Try again?")
        return

    display = f"“{title}”" + (f" by {author}" if author else "")
    narrator = picked.get("narrator")
    if narrator:
        sms.send(phone, f"Found {display}, narrated by {narrator}. Downloading now…")
    else:
        sms.send(phone, f"Found {display}. Downloading now…")

    try:
        await poll_and_finalise(
            download=download,
            fallbacks=[],
            display=display,
            author=author,
            title=title,
            phone=phone,
            settings=settings,
            sms=sms,
        )
    except Exception:
        logger.exception("poll_and_finalise after pick crashed")
        sms.send(phone, "Something went wrong while downloading. Send the title again to retry?")


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

    async def _deploy() -> None:
        proc = await asyncio.create_subprocess_exec(
            "/home/oscar/auto-torrent/deploy.sh",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            logger.info("Deploy OK: %s", stdout.decode().strip())
        else:
            logger.error("Deploy failed: %s", stderr.decode().strip())

    asyncio.create_task(_deploy())
    return Response(status_code=200)


class ChatEventBus:
    """Duck-typed SMS sink that funnels agent/worker `sms.send(...)` calls into SSE.

    Why: run_agent + poll_and_finalise both call `sms.send(phone, body)`. We don't
    want to fork those paths for chat — we just hand them a bus that emits the
    same messages as Server-Sent Events instead of SMS.
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()
        # Constructed inside an async request handler, so the loop is running.
        self._loop = asyncio.get_running_loop()
        # Whether the agent has surfaced anything to the user via send(). Used
        # by /chat to decide if an "agent ended without commit/ask" outcome is
        # a real failure or just a graceful no-results that already messaged.
        self.messaged = False

    def send(self, to: str, body: str) -> None:
        # `to` is the session id in chat mode — ignored, the SSE stream is the channel.
        self.messaged = True
        self._loop.call_soon_threadsafe(
            self.queue.put_nowait, ("progress", {"text": body})
        )

    def system_progress(self, body: str) -> None:
        """Server-emitted progress (e.g. "Searching…") that does NOT count as
        the agent having messaged — so a subsequent agent crash with no output
        still surfaces as an error rather than being suppressed."""
        self._loop.call_soon_threadsafe(
            self.queue.put_nowait, ("progress", {"text": body})
        )

    def emit(self, event: str, data: dict) -> None:
        self._loop.call_soon_threadsafe(self.queue.put_nowait, (event, data))

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self.queue.put_nowait, None)


class ChatRequest(BaseModel):
    query: str
    session_id: str = "default"


# How often the chat path polls download state and emits "Downloading… N%" as
# a progress event so the bubble shows motion. Tests override.
CHAT_PROGRESS_INTERVAL_S: float = 30.0


def _require_bearer(authorization: str = Header("", alias="Authorization")) -> None:
    token = settings.atb_api_token
    if not token:
        raise HTTPException(status_code=503, detail="chat endpoint not configured")
    expected = f"Bearer {token}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


async def _chat_progress_pump(
    download_id: str, bus: "ChatEventBus", stop: asyncio.Event
) -> None:
    """Emit a progress event whenever the download's percentage changes.

    Why: poll_and_finalise blocks for the entire download (minutes to hours)
    without sending SMS — fine for SMS UX, dead silent for chat UX. The pump
    fills the gap so the bubble shows motion.
    """
    last_pct = -1
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=CHAT_PROGRESS_INTERVAL_S)
            return
        except asyncio.TimeoutError:
            pass
        state = _refresh_state(download_id)
        if not state:
            continue
        pct = int(float(state.get("progress") or 0) * 100)
        if pct != last_pct:
            bus.send("", f"Downloading… {pct}%")
            last_pct = pct


async def _emit_download_and_poll(
    bus: "ChatEventBus",
    *,
    download: dict,
    fallbacks: list[dict],
    display: str,
    title: str,
    author: str,
    session: str,
) -> None:
    """Emit `committed`, run the poll with a progress pump, then `completed`."""
    download_id = download.get("id", "")
    bus.emit(
        "committed",
        {"id": download_id, "title": title, "author": author, "display": display},
    )
    stop = asyncio.Event()
    pump = asyncio.create_task(_chat_progress_pump(download_id, bus, stop))
    try:
        await poll_and_finalise(
            download=download,
            fallbacks=fallbacks,
            display=display,
            author=author,
            title=title,
            phone=session,
            settings=settings,
            sms=bus,
        )
        bus.emit("completed", {"title": title, "author": author})
    except Exception as e:  # noqa: BLE001
        logger.exception("chat poll_and_finalise crashed")
        bus.emit("error", {"message": f"download error: {e}"})
    finally:
        stop.set()
        try:
            await pump
        except Exception:  # noqa: BLE001
            pass


@app.post("/chat")
async def chat(req: ChatRequest, _: None = Depends(_require_bearer)) -> StreamingResponse:
    bus = ChatEventBus()

    async def run_chat() -> None:
        try:
            query = req.query.strip()
            session = req.session_id.strip() or "default"

            if not query:
                bus.emit("error", {"message": "empty query"})
                return

            picked = _maybe_quick_pick(query, session)
            if picked:
                clear_conversation(session)
                await _chat_commit_and_poll(picked, session, bus)
                return

            # Instant feedback. The agent's pre-commit phase (LLM parse →
            # fan-out ABB search → enrich → score → rank → DHT probe) can run
            # 60–90s before the first send_sms. Without this, the bubble sits
            # on a static "Thinking…" spinner that looks frozen.
            bus.system_progress(f"Searching for “{query}”…")

            pending = get_pending_options(session)
            outcome = await run_agent(query, session, settings, bus, pending_options=pending)

            if outcome.kind == "committed":
                clear_conversation(session)
                await _emit_download_and_poll(
                    bus,
                    download=outcome.download or {},
                    fallbacks=outcome.fallbacks,
                    display=outcome.display,
                    title=outcome.title,
                    author=outcome.author,
                    session=session,
                )
            elif outcome.kind in ("asked", "no_results"):
                # Agent already pushed user-facing text via bus.send (→ progress events).
                pass
            elif bus.messaged:
                # Agent ended in error but already informed the user (graceful
                # no-results path returns kind="error" with no commit/ask).
                # Suppress the phantom red bubble after a real message.
                pass
            else:
                bus.emit("error", {"message": outcome.message or "agent error"})
        except Exception as e:  # noqa: BLE001
            logger.exception("chat task crashed")
            bus.emit("error", {"message": f"{type(e).__name__}: {e}"})
        finally:
            bus.close()

    # Run the work detached from the SSE response. If the client disconnects
    # (app backgrounded or closed), the download AND the library organise/scan
    # must still finish so the book lands in the library regardless — so this
    # task is deliberately NOT cancelled when the stream closes.
    task = asyncio.create_task(run_chat())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    async def event_stream() -> AsyncIterator[str]:
        # Drain the bus while the client is connected. On disconnect this
        # generator is closed, but `task` keeps running to completion.
        while True:
            try:
                item = await asyncio.wait_for(bus.queue.get(), timeout=20.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if item is None:
                break
            event, data = item
            yield f"event: {event}\ndata: {json.dumps(data)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _chat_commit_and_poll(picked: dict, session: str, bus: ChatEventBus) -> None:
    """Mirror of _commit_and_poll_from_pick, emitting SSE instead of SMS."""
    from ..cli import _execute_download_bg

    title = picked.get("title") or "Unknown"
    author = picked.get("author") or ""
    magnet = picked.get("magnet") or ""
    if not magnet:
        bus.emit("error", {"message": "That option's gone stale — search again?"})
        return

    bg_title = f"{title} - {author}" if author else title
    try:
        download = await asyncio.to_thread(_execute_download_bg, bg_title, magnet, None)
    except Exception as e:  # noqa: BLE001
        logger.exception("chat pick → BG start failed")
        bus.emit("error", {"message": f"Couldn't start that download: {e}"})
        return

    display = f"“{title}”" + (f" by {author}" if author else "")
    narrator = picked.get("narrator")
    line = (
        f"Found {display}, narrated by {narrator}. Downloading now…"
        if narrator
        else f"Found {display}. Downloading now…"
    )
    bus.send(session, line)
    await _emit_download_and_poll(
        bus,
        download=download,
        fallbacks=[],
        display=display,
        title=title,
        author=author,
        session=session,
    )


def serve() -> None:
    import uvicorn

    uvicorn.run(
        "auto_torrent.server.app:app",
        host="127.0.0.1",
        port=settings.port,
        log_level="info",
    )
