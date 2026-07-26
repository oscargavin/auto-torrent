"""The two rules that keep chapter hydration from doing harm."""

from auto_torrent.server.chapters import (
    AudibleChapters,
    is_placeholder,
    placeholder_share,
    runtimes_agree,
    to_abs_payload,
    worth_replacing,
)


class TestPlaceholderTitles:
    """What ABS derives from filenames, and therefore what is safe to replace."""

    def test_bare_numbers_say_nothing(self):
        for t in ("001", "00", "93", "0"):
            assert is_placeholder(t)

    def test_file_and_track_labels_say_nothing(self):
        for t in ("Part 001", "Track 5", "Chapter 12", "Disc 2", "CD 1", "file_03"):
            assert is_placeholder(t)

    def test_spelled_out_chapter_numbers_say_nothing(self):
        assert is_placeholder("Chapter One")
        assert is_placeholder("chapter three")

    def test_a_number_with_a_letter_says_nothing(self):
        # The Midnight Library: 189 chapters called 1a, 1b, 1c…
        for t in ("1a", "1b", "12c"):
            assert is_placeholder(t)

    def test_disc_labels_say_nothing(self):
        # Steve Jobs: "01 Audio CD" ×7.
        assert is_placeholder("01 Audio CD")

    def test_the_books_own_name_says_nothing(self):
        # Best Served Cold: every chapter is the title plus a number.
        assert is_placeholder("Best Served Cold 01", "Best Served Cold")
        assert is_placeholder("Still Life - 04", "Still Life")
        assert is_placeholder(
            "01/15 - The Girl Who Played with Fire", "The Girl Who Played with Fire"
        )

    def test_an_edition_suffix_on_the_book_title_is_ignored(self):
        # The library title carries "(Unabridged)"; the chapters never do.
        assert is_placeholder("Dune 001", "Dune (Unabridged)")

    def test_a_numbered_real_title_still_counts(self):
        # Kitchen Confidential: "001 - Introduction" is a real chapter name
        # that merely happens to be numbered.
        assert not is_placeholder("001 - Introduction")
        assert not is_placeholder("002 - Food Is Good")

    def test_empty_says_nothing(self):
        assert is_placeholder("")
        assert is_placeholder("   ")

    def test_a_real_title_is_not_a_placeholder(self):
        for t in (
            "1. The Riddle House",
            "Chapter One - Dudley Demented",
            "Opening Credits",
            "The Boy Who Lived",
        ):
            assert not is_placeholder(t)


class TestWorthReplacing:
    def test_an_item_with_no_chapters_qualifies(self):
        assert worth_replacing([])

    def test_ninety_four_filenames_qualify(self):
        # The Name of the Wind: 94 files → 94 chapters called 00–93.
        assert worth_replacing([{"title": f"{i:02d}"} for i in range(94)])

    def test_real_titles_are_left_alone(self):
        # Audible's own titles are often just "Chapter 1", so overwriting good
        # embedded metadata would be a downgrade, not a fix.
        assert not worth_replacing(
            [{"title": "1. The Riddle House"}, {"title": "2. The Scar"}]
        )

    def test_dune_qualifies(self):
        # 48 bare "Chapter N" against three part names. The old 5% rule kept
        # this, which is the case that proved the rule wrong.
        chapters = (
            [{"title": "Book One: Dune"}, {"title": "Book Two: Muad'Dib"}, {"title": "Book Three: The Prophet"}]
            + [{"title": f"Chapter {i}"} for i in range(1, 49)]
        )
        assert worth_replacing(chapters, "Dune (Unabridged)")

    def test_identical_titles_qualify_however_wordy(self):
        # 16 rows reading the same thing tell you as much as 16 reading "001".
        assert worth_replacing(
            [{"title": "The Girl with the Dragon Tattoo"}] * 16,
            "The Girl With The Dragon Tattoo",
        )

    def test_a_mostly_named_list_is_left_alone(self):
        chapters = [{"title": "Opening Credits"}] + [
            {"title": f"Chapter {i} - Something"} for i in range(1, 20)
        ]
        assert not worth_replacing(chapters)

    def test_one_stray_name_among_placeholders_still_qualifies(self):
        chapters = [{"title": "Opening Credits"}] + [
            {"title": f"{i:03d}"} for i in range(60)
        ]
        assert worth_replacing(chapters)

    def test_harry_potter_is_left_alone(self):
        chapters = [{"title": "Opening Credits"}] + [
            {"title": t}
            for t in (
                "Chapter One - Dudley Demented",
                "Chapter Two - A Peck of Owls",
                "Chapter Three - The Advance Guard",
            )
        ]
        assert not worth_replacing(chapters, "Harry Potter and the Order of the Phoenix")


class TestPlaceholderShare:
    def test_an_empty_list_is_wholly_uninformative(self):
        assert placeholder_share([]) == 1.0

    def test_a_mixed_list_reports_the_fraction(self):
        assert placeholder_share(["001", "002", "The Riddle House", "The Scar"]) == 0.5


class TestRuntimesAgree:
    def test_same_recording_agrees(self):
        assert runtimes_agree(58244, 58244)

    def test_a_couple_of_minutes_of_credits_is_tolerated(self):
        assert runtimes_agree(58244, 58244 + 90)

    def test_an_abridgement_is_rejected(self):
        # Applying a full edition's marks to an abridgement puts every chapter
        # in the wrong place — worse than the filenames being replaced.
        assert not runtimes_agree(20000, 58244)

    def test_a_short_book_gets_an_absolute_floor(self):
        # 2% of 20 minutes is 24s, which is tighter than the difference two
        # intros can make.
        assert runtimes_agree(1200, 1250)

    def test_missing_runtime_fails_closed(self):
        assert not runtimes_agree(0, 58244)
        assert not runtimes_agree(58244, 0)


class TestAbsPayload:
    def _chapters(self):
        return AudibleChapters(
            runtime_s=1000.0,
            chapters=((0.0, "Opening Credits"), (11.0, "Dedication"), (17.0, "Chapter 1")),
        )

    def test_each_chapter_runs_to_the_next(self):
        out = to_abs_payload(self._chapters(), 1000.0)
        assert [(c["start"], c["end"]) for c in out] == [
            (0.0, 11.0),
            (11.0, 17.0),
            (17.0, 1000.0),
        ]

    def test_the_last_chapter_ends_at_the_items_own_duration(self):
        # The item's, not Audible's — a few seconds of difference at the tail
        # should not leave a gap or overrun the file.
        out = to_abs_payload(self._chapters(), 990.0)
        assert out[-1]["end"] == 990.0

    def test_titles_and_ids_survive(self):
        out = to_abs_payload(self._chapters(), 1000.0)
        assert [c["title"] for c in out] == ["Opening Credits", "Dedication", "Chapter 1"]
        assert [c["id"] for c in out] == [0, 1, 2]

    def test_out_of_order_offsets_are_sorted(self):
        jumbled = AudibleChapters(
            runtime_s=100.0, chapters=((50.0, "Two"), (0.0, "One"))
        )
        assert [c["title"] for c in to_abs_payload(jumbled, 100.0)] == ["One", "Two"]

    def test_a_zero_length_chapter_is_dropped(self):
        dupes = AudibleChapters(
            runtime_s=100.0, chapters=((0.0, "One"), (0.0, "Also One"), (50.0, "Two"))
        )
        assert [c["title"] for c in to_abs_payload(dupes, 100.0)] == ["One", "Two"]
