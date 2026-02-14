from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env"}

    twilio_account_sid: str
    twilio_auth_token: str
    twilio_phone_number: str
    allowed_numbers: list[str]

    abs_url: str = "http://localhost:13378"
    abs_api_token: str
    abs_library_id: str
    abs_library_path: str = "/srv/audiobooks"

    atb_cwd: str
    port: int = 8004

    @field_validator("allowed_numbers", mode="before")
    @classmethod
    def parse_numbers(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [n.strip() for n in v.split(",") if n.strip()]
        return v
