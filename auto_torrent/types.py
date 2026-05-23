from dataclasses import dataclass


@dataclass(frozen=True)
class BookMetadata:
    title: str
    author: str
    year: int | None = None
    series: str | None = None
    cover_id: int | None = None


@dataclass(frozen=True)
class SearchResult:
    title: str
    link: str
    format: str = ""
    bitrate: str = ""
    file_size: str = ""
    posted: str = ""
    narrator: str = ""
    author: str = ""
    description: str = ""
    category: str = ""
    language: str = ""
    abridged: bool | None = None
    magnet: str = ""
    cover_url: str = ""


@dataclass(frozen=True)
class ScoredResult:
    result: SearchResult
    score: int


@dataclass(frozen=True)
class BookCard:
    """Display metadata for a recommended audiobook, hydrated from Audible/
    Audnexus (square cover) with Open Library as a fallback. Tuples (not lists)
    so the dataclass stays immutable/hashable and JSON-serialises cleanly."""

    title: str
    author: str
    asin: str | None = None
    subtitle: str | None = None
    narrators: tuple[str, ...] = ()
    description: str = ""
    cover_url: str | None = None
    series: str | None = None
    series_position: str | None = None
    genres: tuple[str, ...] = ()
    runtime_min: int | None = None
    year: int | None = None
    source: str = "audible"
