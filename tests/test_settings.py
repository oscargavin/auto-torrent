from auto_torrent.server.settings import Settings


def _setup_required_env(monkeypatch) -> None:
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "x")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "x")
    monkeypatch.setenv("ALLOWED_NUMBERS", '["x"]')
    monkeypatch.setenv("ABS_API_TOKEN", "x")
    monkeypatch.setenv("ABS_LIBRARY_ID", "x")
    monkeypatch.setenv("ATB_CWD", "x")


def test_redis_url_default(monkeypatch):
    _setup_required_env(monkeypatch)
    s = Settings(_env_file=None)
    assert s.redis_url == "redis://127.0.0.1:6379/0"


def test_redis_url_from_env(monkeypatch):
    _setup_required_env(monkeypatch)
    monkeypatch.setenv("REDIS_URL", "redis://example:6379/2")
    s = Settings(_env_file=None)
    assert s.redis_url == "redis://example:6379/2"
