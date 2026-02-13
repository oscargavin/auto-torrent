from rapidfuzz import fuzz

from .config import (
    AUTHOR_WEIGHT,
    MIN_SCORE,
    NARRATOR_MATCH_BONUS,
    NARRATOR_MATCH_THRESHOLD,
    NARRATOR_MISSING_PENALTY,
    NARRATOR_WRONG_BONUS,
    SERIES_WEIGHT,
    TITLE_WEIGHT,
)
from .types import BookMetadata, ScoredResult, SearchResult


def score_result(
    result: SearchResult,
    book: BookMetadata,
    prefer_narrator: str | None = None,
) -> int:
    title = result.title.lower()
    scores: list[float] = []

    title_score = max(
        fuzz.token_set_ratio(book.title.lower(), title),
        fuzz.partial_ratio(book.title.lower(), title),
    )
    scores.append(title_score * TITLE_WEIGHT)

    if book.author:
        author_lower = book.author.lower()
        author_score = max(
            fuzz.token_set_ratio(author_lower, title),
            fuzz.partial_ratio(author_lower, title),
            fuzz.token_set_ratio(author_lower, result.author.lower()),
        )
        scores.append(author_score * AUTHOR_WEIGHT)

    if book.series:
        series_lower = book.series.lower()
        series_score = max(
            fuzz.token_set_ratio(series_lower, title),
            fuzz.partial_ratio(series_lower, title),
        )
        scores.append(series_score * SERIES_WEIGHT)

    base = sum(scores)

    narrator = result.narrator.lower()
    if prefer_narrator and narrator:
        narrator_sim = fuzz.token_set_ratio(prefer_narrator.lower(), narrator)
        if narrator_sim >= NARRATOR_MATCH_THRESHOLD:
            base = min(base + NARRATOR_MATCH_BONUS, 100)
        else:
            base = min(base + NARRATOR_WRONG_BONUS, 100)
    elif prefer_narrator and not narrator:
        base = max(base - NARRATOR_MISSING_PENALTY, 0)

    return round(base)


def score_and_sort(
    results: list[SearchResult],
    book: BookMetadata,
    prefer_narrator: str | None = None,
    min_score: int = MIN_SCORE,
) -> list[ScoredResult]:
    scored = [
        ScoredResult(result=r, score=score_result(r, book, prefer_narrator))
        for r in results
    ]
    scored.sort(key=lambda s: s.score, reverse=True)
    return [s for s in scored if s.score >= min_score and s.result.magnet]
