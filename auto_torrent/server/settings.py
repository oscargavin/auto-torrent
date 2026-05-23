from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env"}

    twilio_account_sid: str
    twilio_auth_token: str = ""
    twilio_phone_number: str
    twilio_api_key_sid: str = ""
    twilio_api_key_secret: str = ""
    allowed_numbers: list[str]

    abs_url: str = "http://localhost:13378"
    abs_api_token: str
    abs_library_id: str
    abs_library_path: str = "/home/oscar/audiobooks"

    atb_cwd: str
    port: int = 8004
    github_webhook_secret: str = ""
    atb_api_token: str = ""

    # Bookkeeper "profiles" feature: a shared app secret the mobile app sends to
    # manage family ABS accounts via /profiles, and the JSON file the minted
    # per-user API keys are persisted to (tokens are only shown once by ABS).
    profiles_app_secret: str = ""
    profiles_store_path: str = "profiles.json"

    # Per-(profile + listening history) recommendation cache, so Claude only
    # runs when a profile's finished books change (or on refresh).
    rec_cache_path: str = "rec-cache.json"

    @field_validator("allowed_numbers", mode="before")
    @classmethod
    def parse_numbers(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [n.strip() for n in v.split(",") if n.strip()]
        return v
