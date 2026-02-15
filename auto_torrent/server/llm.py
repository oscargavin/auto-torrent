"""LLM-powered SMS message parsing — classifies intent and extracts book queries."""

import json
import logging
import os
import subprocess

logger = logging.getLogger("atb.llm")

LLM_MODEL = "claude-opus-4-6"
LLM_TIMEOUT = 30

SYSTEM_PROMPT = """\
You are a friendly SMS assistant for an audiobook service. A person texts you \
and you determine what they want. Your job is to either extract a searchable \
book title or reply with a helpful message.

Keep replies under 160 characters when possible — these are SMS messages."""

CLASSIFY_PROMPT = """\
<message>{message}</message>

<instructions>
Determine what action to take for this SMS message.

1. SEARCH — The message contains a recognisable book title (even misspelled or \
abbreviated). Extract the corrected, canonical title. If it's a series name \
with no specific book, use the first book in the series.
2. REPLY — The message needs a helpful response instead of a search. This \
includes: author names without a title, genre/mood requests, conversational \
messages (thanks, ok, yes), questions about the service, or text too vague to \
search.

For REPLY messages, be warm and brief. Suggest 2-3 specific book titles when \
relevant so the person can text back one of them.
</instructions>

<examples>
<example>
Message: "Project Hail Mary"
Action: search
Reasoning: Clear book title, search directly.
query: "Project Hail Mary"
</example>

<example>
Message: "Hairy Potter"
Action: search
Reasoning: Misspelled "Harry Potter" — assume first book in series.
query: "Harry Potter and the Philosopher's Stone"
</example>

<example>
Message: "Harry Potter"
Action: search
Reasoning: Series name, pick the first book.
query: "Harry Potter and the Philosopher's Stone"
</example>

<example>
Message: "Harry Potter 3"
Action: search
Reasoning: Third Harry Potter book.
query: "Harry Potter and the Prisoner of Azkaban"
</example>

<example>
Message: "Discworld"
Action: search
Reasoning: Series name, pick the first book.
query: "The Colour of Magic"
</example>

<example>
Message: "Georgette Heyer"
Action: reply
Reasoning: Author name only, no specific book.
reply: "Which Georgette Heyer book? Try one of these: The Grand Sophy, Cotillion, or Frederica"
</example>

<example>
Message: "something funny"
Action: reply
Reasoning: Genre request, suggest specific titles.
reply: "Try one of these: The Hitchhiker's Guide to the Galaxy, Good Omens, or Anxious People"
</example>

<example>
Message: "thank you!"
Action: reply
Reasoning: Conversational, no book request.
reply: "You're welcome! Text me a book title anytime."
</example>

<example>
Message: "the next one"
Action: reply
Reasoning: Follow-up with no context — need the book name.
reply: "Which book would you like next? Text me the title!"
</example>

<example>
Message: "My friend recommended a book about a man on Mars"
Action: search
Reasoning: Likely "The Martian" by Andy Weir.
query: "The Martian"
</example>

<example>
Message: "📚"
Action: reply
Reasoning: Just an emoji, no book title.
reply: "Send me a book title and I'll find the audiobook!"
</example>

<example>
Message: "can you get me that one about the girl with the dragon tattoo"
Action: search
Reasoning: Recognisable description of a specific book.
query: "The Girl with the Dragon Tattoo"
</example>

<example>
Message: "what books do I have"
Action: reply
Reasoning: Question about the service.
reply: "Open the BookPlayer app to see your library! Text me a title to add more."
</example>
</examples>

Classify the message above."""


def parse_sms(message: str) -> dict:
    """Parse an SMS message using Claude to classify intent.

    Returns:
        {"action": "search", "query": "..."} or
        {"action": "reply", "reply": "..."}
    """
    prompt = CLASSIFY_PROMPT.format(message=message)

    schema = json.dumps({
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "reply"],
            },
            "query": {
                "type": "string",
                "description": "Corrected book title to search for (when action is search)",
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
                "--max-turns", "2",
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
