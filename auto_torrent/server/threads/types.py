"""Wire types for /chat/threads*.

Mirrored by the app in src/features/get-book/types.ts. Keep the two in sync:
every field here is rendered there, and the client's exhaustiveness checks turn
a new variant into a build failure rather than a blank message.
"""

from __future__ import annotations

import enum
import time
import uuid
from typing import Self

from pydantic import BaseModel, Field, field_validator

MAX_MESSAGE_LEN = 500
# What a thread carries into the agent as history. Long enough to hold a real
# exchange ("something funny" → three options → "the second" → "actually
# shorter"), short enough that the prompt stays cheap on a thread someone has
# been adding to for a week.
HISTORY_LIMIT = 20


class MessageKind(str, enum.Enum):
    """What a message *is*, which is what the client switches its renderer on."""

    user = "user"
    assistant = "assistant"
    #: A question with tappable answers. `options` is populated; `chosen_index`
    #: fills in once answered, so the card can render its resolved state rather
    #: than vanishing.
    choice = "choice"
    #: A download. Carries only `job_id` — the job's own record and SSE stream
    #: remain the authority, so a thread can't hold a stale copy of its status.
    job = "job"


class ThreadStatus(str, enum.Enum):
    idle = "idle"
    #: An agent turn is running. The composer stays usable; the client shows a
    #: thinking indicator.
    working = "working"
    #: The agent asked something and the worker exited. Nothing progresses until
    #: the user answers (or the choice expires).
    awaiting_choice = "awaiting_choice"


class ChoiceOption(BaseModel):
    """One tappable answer.

    Deliberately does NOT carry the magnet. The client never needs it, magnets
    are bulky, and keeping them server-side means a thread transcript can be
    logged or replayed without leaking torrent links. The worker holds them in
    a parallel Redis key keyed by the same index.
    """

    index: int
    title: str
    author: str = ""
    narrator: str = ""
    book_format: str = ""
    size: str = ""
    cover_url: str = ""
    abridged: bool = False
    #: A short reason this option is on the list ("unabridged, most seeders").
    #: The agent writes it; it is what makes a list of near-identical editions
    #: choosable rather than a coin toss.
    note: str = ""


class Message(BaseModel):
    id: str
    thread_id: str
    kind: MessageKind
    created_at: float
    text: str = ""
    options: list[ChoiceOption] = Field(default_factory=list)
    chosen_index: int | None = None
    job_id: str | None = None

    @classmethod
    def new(cls, thread_id: str, kind: MessageKind, **kw) -> Self:
        return cls(
            id=uuid.uuid4().hex,
            thread_id=thread_id,
            kind=kind,
            created_at=time.time(),
            **kw,
        )


class Thread(BaseModel):
    id: str
    profile_id: str
    #: First user message, truncated — the label in a thread list.
    title: str = ""
    status: ThreadStatus = ThreadStatus.idle
    created_at: float
    updated_at: float

    @classmethod
    def new(cls, profile_id: str) -> Self:
        now = time.time()
        return cls(
            id=uuid.uuid4().hex,
            profile_id=profile_id,
            created_at=now,
            updated_at=now,
        )

    def to_redis_hash(self) -> dict[str, str]:
        d = self.model_dump()

        def _str(v: object) -> str:
            return v.value if isinstance(v, enum.Enum) else str(v)

        return {k: "" if v is None else _str(v) for k, v in d.items()}

    @classmethod
    def from_redis_hash(cls, data: dict[str, str]) -> Self:
        # No ""→None coercion here, unlike Job: every field on Thread is
        # non-optional, and an empty title is a real value (a thread whose
        # first message hasn't landed yet) rather than a missing one.
        return cls.model_validate(data)


class ThreadView(BaseModel):
    """A thread plus its messages — what GET /chat/threads/{id} returns."""

    thread: Thread
    messages: list[Message]


class CreateThreadRequest(BaseModel):
    profile_id: str = Field(min_length=1, max_length=64)


class PostMessageRequest(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def _strip_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message must not be empty")
        if len(v) > MAX_MESSAGE_LEN:
            raise ValueError("message too long")
        return v


class ChooseRequest(BaseModel):
    message_id: str = Field(min_length=1)
    option_index: int = Field(ge=0)


# SSE event names on a thread's stream. `message` carries a whole Message —
# there is no delta stream, because the agent speaks in whole sentences via its
# own tool rather than emitting tokens.
EVENT_MESSAGE = "message"
EVENT_STATUS = "status"
EVENT_ERROR = "error"
#: Ephemeral narration while the agent works, before anything has committed.
#: Not persisted — it is the thinking row, not the transcript.
EVENT_ACTIVITY = "activity"
