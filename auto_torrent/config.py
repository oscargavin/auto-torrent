from pathlib import Path

ABB_BASE_URL = "https://audiobookbay.lu"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
}

DEFAULT_TRACKERS = [
    "udp://tracker.openbittorrent.com:80",
    "udp://opentor.org:2710",
    "udp://tracker.ccc.de:80",
    "udp://tracker.coppersurfer.tk:6969",
    "udp://tracker.leechers-paradise.org:6969",
]

DOWNLOAD_DIR = Path.home() / "Downloads" / "audiobooks"
STATE_DIR = Path.home() / ".auto-torrent" / "downloads"
CACHE_DIR = Path.home() / ".auto-torrent" / "cache"
DEFAULT_LIMIT = 10

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

# Open Library subjects to ignore when detecting series
GENERIC_SUBJECTS = frozenset({
    "fiction", "fantasy", "science fiction", "epic fantasy",
    "paranormal fiction", "magic", "heroes", "wizards", "magicians",
    "fairies", "assassination", "attempted assassination",
    "mercenary troops", "mercenary soldiers", "reading materials",
    "spanish language",
})
