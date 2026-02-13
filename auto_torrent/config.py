import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".auto-torrent"
CONFIG_FILE = CONFIG_DIR / "config.json"

ABB_BASE_URL = "https://audiobookbay.lu"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
}

DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.moeking.me:6969/announce",
]

DOWNLOAD_DIR = Path.home() / "Downloads" / "audiobooks"
STATE_DIR = Path.home() / ".auto-torrent" / "downloads"
CACHE_DIR = Path.home() / ".auto-torrent" / "cache"
STREAM_DIR = Path.home() / ".auto-torrent" / "stream"
DEFAULT_LIMIT = 10

# Streaming
STREAM_BUFFER_MB = 5
STREAM_PORT = 8888

# Scoring weights
TITLE_WEIGHT = 0.5
AUTHOR_WEIGHT = 0.3
SERIES_WEIGHT = 0.2
NARRATOR_MATCH_BONUS = 15
NARRATOR_WRONG_BONUS = 5
NARRATOR_MISSING_PENALTY = 5
NARRATOR_MATCH_THRESHOLD = 80
MIN_SCORE = 60

# Concurrency
SCRAPE_WORKERS = 4

# TPB scoring
TPB_SOURCE_SCORES = {"bluray": 25, "web-dl": 20, "webrip": 15, "hdrip": 10, "hdtv": 8, "cam": 0}
TPB_STATUS_SCORES = {"vip": 15, "trusted": 10, "helper": 5, "member": 3}
TPB_RESOLUTION_LADDER = ["480p", "720p", "1080p", "2160p"]
TPB_RESOLUTION_DISTANCE_SCORES = {0: 20, 1: 12, 2: 5, 3: 2}
TPB_CODEC_SCORES = {"x265": 5, "av1": 5, "x264": 3}
TPB_MAX_SEED_SCORE = 30
TPB_SEED_LOG_SCALE = 5

def load_user_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def get_proxy() -> str | None:
    """Resolve proxy: env var > config file. CLI --proxy overrides both (handled in cli.py)."""
    return os.environ.get("AUTO_TORRENT_PROXY") or load_user_config().get("proxy")


# Open Library subjects to ignore when detecting series
GENERIC_SUBJECTS = frozenset({
    "fiction", "fantasy", "science fiction", "epic fantasy",
    "paranormal fiction", "magic", "heroes", "wizards", "magicians",
    "fairies", "assassination", "attempted assassination",
    "mercenary troops", "mercenary soldiers", "reading materials",
    "spanish language",
})
