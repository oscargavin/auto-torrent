"""Lifecycle event vocabulary shared across the /chat and /chat/jobs paths.

One source of truth so the SSE producers (app.py pump, worker.py poll) and the
Bookkeeper app's decoder agree on the strings. Discrete SSE events keep their
existing names (`committed`/`completed`/`error`); finer-grained lifecycle is
carried on `progress` events via the `stage` field so the vocabulary stays
additive and forward-compatible — an older consumer that doesn't know a stage
still renders the `text` fallback.
"""

# Discrete SSE event names.
EVENT_PROGRESS = "progress"
EVENT_COMMITTED = "committed"
EVENT_COMPLETED = "completed"
EVENT_ERROR = "error"

# `stage` values on a `progress` event.
STAGE_SEARCHING = "searching"
STAGE_FOUND = "found"
STAGE_DOWNLOADING = "downloading"
STAGE_STALLED = "stalled"
STAGE_IMPORTING = "importing"
STAGE_RETRYING = "retrying"
STAGE_IMPORT_FAILED = "import_failed"

# The full set the app mirrors. Every member must have a producer — a stage
# defined here but never emitted is a stage the client builds rendering for
# and never sees (searching and found were exactly that until U3), and one
# emitted but not listed here breaks the app's parity check.
ALL_STAGES = frozenset(
    {
        STAGE_SEARCHING, STAGE_FOUND, STAGE_DOWNLOADING, STAGE_STALLED,
        STAGE_IMPORTING, STAGE_RETRYING, STAGE_IMPORT_FAILED,
    }
)
