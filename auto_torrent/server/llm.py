"""LLM-powered SMS message parsing — classifies intent and extracts book queries."""

import json
import logging
import os
import subprocess
import time

logger = logging.getLogger("atb.llm")

LLM_MODEL = "claude-opus-4-6"
LLM_TIMEOUT = 30

# Per-phone conversation state: {phone: {suggestions: [...], context: str, ts: float}}
_conversations: dict[str, dict] = {}
CONVERSATION_TTL = 600  # 10 minutes

SYSTEM_PROMPT = """\
You are a friendly SMS assistant for an audiobook service. A person texts you \
and you determine what they want. Your job is to either extract a searchable \
book title, suggest similar books, or reply with a helpful message.

Keep replies under 160 characters when possible — these are SMS messages."""

CLASSIFY_PROMPT = """\
<message>{message}</message>
{context}
<instructions>
Determine what action to take for this SMS message.

1. SEARCH — The message contains a recognisable book title (even misspelled or \
abbreviated). Extract the corrected, canonical title. If it's a series name \
with no specific book, use the first book in the series. Also use this when the \
person picks a number from pending suggestions.
2. SUGGEST — The person wants book recommendations ("something like...", \
"books similar to...", a genre/mood request, or an author name without a \
specific title). Return exactly 3 numbered suggestions with title and author.
3. REPLY — The message is conversational (thanks, ok, yes), a question about \
the service, or too vague to act on. Reply with a warm, brief message.

When there are pending suggestions and the person texts a number (1, 2, 3) or \
a phrase like "the first one" or "number 2", treat it as SEARCH with the \
corresponding suggestion title.
</instructions>

<examples>
<example>
Message: "Project Hail Mary"
Action: search
query: "Project Hail Mary"
</example>

<example>
Message: "Hairy Potter"
Action: search
query: "Harry Potter and the Philosopher's Stone"
</example>

<example>
Message: "Harry Potter 3"
Action: search
query: "Harry Potter and the Prisoner of Azkaban"
</example>

<example>
Message: "Discworld"
Action: search
query: "The Colour of Magic"
</example>

<example>
Message: "something like April Lady"
Action: suggest
suggestions: ["The Grand Sophy by Georgette Heyer", "Cotillion by Georgette Heyer", "Venetia by Georgette Heyer"]
</example>

<example>
Message: "Georgette Heyer"
Action: suggest
suggestions: ["The Grand Sophy by Georgette Heyer", "Cotillion by Georgette Heyer", "Frederica by Georgette Heyer"]
</example>

<example>
Message: "something funny"
Action: suggest
suggestions: ["The Hitchhiker's Guide to the Galaxy by Douglas Adams", "Good Omens by Terry Pratchett & Neil Gaiman", "Anxious People by Fredrik Backman"]
</example>

<example>
Message: "a good thriller"
Action: suggest
suggestions: ["The Girl with the Dragon Tattoo by Stieg Larsson", "Gone Girl by Gillian Flynn", "The Silent Patient by Alex Michaelides"]
</example>

<example>
Pending suggestions: 1. The Grand Sophy  2. Cotillion  3. Frederica
Message: "2"
Action: search
query: "Cotillion"
</example>

<example>
Pending suggestions: 1. The Hitchhiker's Guide  2. Good Omens  3. Anxious People
Message: "the first one"
Action: search
query: "The Hitchhiker's Guide to the Galaxy"
</example>

<example>
Message: "thank you!"
Action: reply
reply: "You're welcome! Text me a book title anytime."
</example>

<example>
Message: "the next one"
Action: reply
reply: "Which book would you like next? Text me the title!"
</example>

<example>
Message: "My friend recommended a book about a man on Mars"
Action: search
query: "The Martian"
</example>

<example>
Message: "can you get me that one about the girl with the dragon tattoo"
Action: search
query: "The Girl with the Dragon Tattoo"
</example>

<example>
Message: "what books do I have"
Action: reply
reply: "Open the BookPlayer app to see your library! Text me a title to add more."
</example>
</examples>

Classify the message above."""


def _get_context(phone: str) -> str:
    """Build context block from conversation state for the LLM prompt."""
    conv = _conversations.get(phone)
    if not conv:
        return ""
    if time.time() - conv["ts"] > CONVERSATION_TTL:
        del _conversations[phone]
        return ""
    suggestions = conv.get("suggestions", [])
    if not suggestions:
        return ""
    numbered = "\n".join(f"{i+1}. {s}" for i, s in enumerate(suggestions))
    return f"\n<pending_suggestions>\n{numbered}\n</pending_suggestions>\n"


def store_suggestions(phone: str, suggestions: list[str]) -> None:
    """Store suggestions for a phone number so the next message can reference them."""
    _conversations[phone] = {
        "suggestions": suggestions,
        "ts": time.time(),
    }


def parse_sms(message: str, phone: str = "") -> dict:
    """Parse an SMS message using Claude to classify intent.

    Returns:
        {"action": "search", "query": "..."} or
        {"action": "suggest", "suggestions": ["...", "...", "..."]} or
        {"action": "reply", "reply": "..."}
    """
    context = _get_context(phone) if phone else ""
    prompt = CLASSIFY_PROMPT.format(message=message, context=context)

    schema = json.dumps({
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "suggest", "reply"],
            },
            "query": {
                "type": "string",
                "description": "Corrected book title to search for (when action is search)",
            },
            "suggestions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "3 book suggestions with author (when action is suggest)",
            },
            "reply": {
                "type": "string",
                "description": "Friendly SMS reply to send back (when action is reply)",
            },
        },
        "required": ["action"],
    })

    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}

    try:
        result = subprocess.run(
            [
                "claude", "-p", prompt,
                "--model", LLM_MODEL,
                "--output-format", "json",
                "--json-schema", schema,
                "--max-turns", "3",
                "--system-prompt", SYSTEM_PROMPT,
            ],
            capture_output=True, text=True, timeout=LLM_TIMEOUT,
            env=env,
        )

        if result.returncode != 0:
            logger.error("LLM failed (exit %d): %s", result.returncode, result.stderr[:300])
            return {"action": "search", "query": message}

        parsed = json.loads(result.stdout)
        output = parsed.get("structured_output", parsed)
        logger.info("LLM classified '%s' → %s", message, output.get("action"))
        return output

    except subprocess.TimeoutExpired:
        logger.warning("LLM timed out, falling back to raw search")
        return {"action": "search", "query": message}
    except (json.JSONDecodeError, KeyError) as e:
        logger.error("LLM parse error: %s", e)
        return {"action": "search", "query": message}
