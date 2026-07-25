from auto_torrent.server.library_match import find_existing, normalise_title


def item(title: str, author: str = "") -> dict:
    return {"media": {"metadata": {"title": title, "authorName": author}}}


LIBRARY = [
    item("Dune", "Frank Herbert"),
    item("Dune Messiah", "Frank Herbert"),
    item("The Hobbit: There and Back Again", "J. R. R. Tolkien"),
    item("Project Hail Mary", "Andy Weir"),
]


def test_normalise_drops_article_and_subtitle():
    assert normalise_title("The Hobbit: There and Back Again") == "hobbit"


def test_finds_exact_title():
    assert find_existing(LIBRARY, "Dune") is LIBRARY[0]


def test_finds_with_author():
    assert find_existing(LIBRARY, "Dune", "Frank Herbert") is LIBRARY[0]


def test_ignores_case_and_edition_noise():
    assert find_existing(LIBRARY, "dune (Unabridged)") is LIBRARY[0]


def test_matches_through_library_subtitle():
    assert find_existing(LIBRARY, "The Hobbit") is LIBRARY[2]


def test_sequel_is_not_the_same_book():
    """The whole reason this matches on equality rather than containment."""
    assert find_existing(LIBRARY, "Dune Messiah") is LIBRARY[1]
    assert find_existing(LIBRARY, "Children of Dune") is None


def test_same_title_different_author_is_not_a_duplicate():
    assert find_existing(LIBRARY, "Dune", "Someone Else") is None


def test_partial_author_still_matches():
    assert find_existing(LIBRARY, "The Hobbit", "Tolkien") is LIBRARY[2]


def test_title_too_short_is_not_evidence():
    assert find_existing([item("It", "Stephen King")], "It") is None


def test_empty_library():
    assert find_existing([], "Dune") is None
