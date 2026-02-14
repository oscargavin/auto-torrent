from twilio.request_validator import RequestValidator
from twilio.rest import Client

from .settings import Settings


class SMSClient:
    def __init__(self, settings: Settings) -> None:
        self._client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        self._validator = RequestValidator(settings.twilio_auth_token)
        self._from = settings.twilio_phone_number

    def validate_request(self, url: str, params: dict[str, str], signature: str) -> bool:
        return self._validator.validate(url, params, signature)

    def send(self, to: str, body: str) -> None:
        self._client.messages.create(to=to, from_=self._from, body=body)
