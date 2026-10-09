from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # "ignore", because the server's .env still holds the old Twilio keys. The
    # default ("forbid") would refuse to start on them.
    model_config = {"env_file": ".env", "extra": "ignore"}

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

    # Per-(book, chapter) "previously on…" recap cache — recaps of a published
    # book never change, so Claude runs once per chapter per book, ever.
    recap_cache_path: str = "recap-cache.json"

    redis_url: str = "redis://127.0.0.1:6379/0"
    # How long completed/failed job rows live in Redis before GC.
    job_state_ttl_s: int = 7 * 24 * 3600
    # How long the dedup key lives so a re-request hits the same in-flight job.
    job_dedup_ttl_s: int = 3600
