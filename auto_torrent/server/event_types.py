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
STAGE_IMPORTING = "importing"
STAGE_RETRYING = "retrying"

# The full set the app mirrors — a parity test asserts the app decoder knows
# exactly these stages.
ALL_STAGES = frozenset(
    {STAGE_SEARCHING, STAGE_FOUND, STAGE_DOWNLOADING, STAGE_IMPORTING, STAGE_RETRYING}
)
