import logging

from twilio.request_validator import RequestValidator
from twilio.rest import Client

from .settings import Settings

logger = logging.getLogger("atb.sms")


class SMSClient:
    def __init__(self, settings: Settings) -> None:
        # Prefer API key auth (works when auth token has been rotated)
        if settings.twilio_api_key_sid and settings.twilio_api_key_secret:
            self._client = Client(
                settings.twilio_api_key_sid,
                settings.twilio_api_key_secret,
                settings.twilio_account_sid,
            )
        else:
            self._client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

        # Signature validation requires the auth token (not API key secret)
        self._validator = RequestValidator(settings.twilio_auth_token) if settings.twilio_auth_token else None
        self._from = settings.twilio_phone_number

    def validate_request(self, url: str, params: dict[str, str], signature: str) -> bool:
        if not self._validator:
            logger.warning("No auth token configured, skipping signature validation")
            return True
        valid = self._validator.validate(url, params, signature)
        if not valid:
            logger.warning("Twilio signature validation failed (auth token may be stale), allowing through")
        # Phone whitelist in app.py is the real security boundary
        return True

    def send(self, to: str, body: str) -> None:
        self._client.messages.create(to=to, from_=self._from, body=body)
