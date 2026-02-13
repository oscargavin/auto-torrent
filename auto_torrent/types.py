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


@dataclass(frozen=True)
class ScoredResult:
    result: SearchResult
    score: int
