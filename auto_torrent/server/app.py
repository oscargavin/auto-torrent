"""FastAPI app — SMS webhook for audiobook requests."""

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import BackgroundTasks, FastAPI, Form, Header, Request, Response

from .settings import Settings
from .sms import SMSClient
from .worker import get_active_downloads, process_audiobook_request

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


app = FastAPI(title="atb-server", lifespan=lifespan)


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

    # Dispatch background pipeline
    background_tasks.add_task(process_audiobook_request, query, From, settings, sms)
    return _twiml_response(f'Searching for "{query}"...')


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def serve() -> None:
    import uvicorn

    uvicorn.run(
        "auto_torrent.server.app:app",
        host="0.0.0.0",
        port=settings.port,  # default 8004
        log_level="info",
    )
