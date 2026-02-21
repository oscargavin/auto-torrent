"""FastAPI app — SMS webhook for audiobook requests."""

import asyncio
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import BackgroundTasks, FastAPI, Form, Header, Request, Response

from .settings import Settings
from .sms import SMSClient
from .llm import clear_conversation, get_pending_result, parse_sms, store_suggestions
from .worker import _download_and_notify, get_active_downloads, process_audiobook_request

logger = logging.getLogger("atb.server")

settings = Settings()
sms = SMSClient(settings)


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


def _try_quick_pick(query: str, phone: str) -> dict | None:
    """Check if query is a bare digit selecting a pending search result."""
    stripped = query.strip()
    if not stripped.isdigit():
        return None
    index = int(stripped)
    if index < 1:
        return None
    return get_pending_result(phone, index)


@app.post("/sms")
async def sms_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    Body: str = Form(""),
    From: str = Form(""),
    x_twilio_signature: str = Header("", alias="X-Twilio-Signature"),
) -> Response:
    # Validate Twilio signature
    form_data = dict(await request.form())
    url = _reconstruct_url(request)

    if not sms.validate_request(url, {k: str(v) for k, v in form_data.items()}, x_twilio_signature):
        logger.warning("Invalid Twilio signature from %s", From)
        return Response(status_code=403)

    # Check whitelist
    if From not in settings.allowed_numbers:
        logger.warning("Unauthorized number: %s", From)
        return Response(status_code=403)

    query = Body.strip()
    logger.info("SMS from %s: %s", From, query)

    # Handle special commands
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

    # LLM classifies the message in a background task — Opus can take 10-15s
    # which exceeds Twilio's webhook timeout. Return TwiML immediately,
    # then the background task handles classification + search + reply.
    background_tasks.add_task(_classify_and_process, query, From, settings, sms)
    return _twiml_response("Got it! Give me a moment...")


async def _classify_and_process(
    query: str, phone: str, settings: Settings, sms: SMSClient,
) -> None:
    """LLM classifies the SMS, then replies, suggests, or dispatches search pipeline."""
    # Fast path: bare digit picks a pending search result (skips 10-15s LLM call)
    picked = _try_quick_pick(query, phone)
    if picked:
        logger.info("Quick pick: '%s' → result '%s'", query, picked.get("title"))
        clear_conversation(phone)
        await _download_and_notify(picked, phone, settings, sms)
        return

    classification = await asyncio.to_thread(parse_sms, query, phone)
    action = classification.get("action") or "search"
    logger.info("LLM: '%s' → %s", query, action)

    if action == "reply":
        reply_text = classification.get("reply", "Send me a book title and I'll find it for you!")
        sms.send(phone, reply_text)
        return

    if action == "suggest":
        suggestions = classification.get("suggestions", [])
        if not suggestions:
            sms.send(phone, "Send me a book title and I'll find it for you!")
            return
        store_suggestions(phone, suggestions)
        numbered = "\n".join(f"{i+1}. {s}" for i, s in enumerate(suggestions))
        sms.send(phone, f"How about one of these?\n{numbered}\n\nReply with a number to download!")
        return

    # Action is "search" — run the full pipeline with the cleaned query
    search_query = classification.get("query", query)
    if search_query != query:
        logger.info("LLM cleaned query: '%s' → '%s'", query, search_query)
    await process_audiobook_request(search_query, phone, settings, sms)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/github")
async def github_webhook(request: Request) -> Response:
    body = await request.body()

    # Verify GitHub signature if secret is configured
    secret = settings.github_webhook_secret
    if secret:
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return Response(status_code=403)

    # Run deploy script in background
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


def serve() -> None:
    import uvicorn

    uvicorn.run(
        "auto_torrent.server.app:app",
        host="127.0.0.1",
        port=settings.port,  # default 8004
        log_level="info",
    )
