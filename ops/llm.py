"""Transitional home for two model calls whose domain modules don't exist yet.

NOTE: this is NOT meant to be a permanent "all LLM calls live here" layer —
domain-specific model calls belong in their domain (habit matching moved to
habit_handlers). What's left:
  - transcribe_with_language_detection(): audio→text with Arabic/Hebrew support.
    Moves to a voice module when voice is extracted (or stays a small util).
  - parse_queue_entry(): queue-specific; moves to the queue plugin when extracted.

Kept here only so the entrypoint (bot.py) doesn't import the SDKs directly until
those two domains are carved out.
"""

import json
import logging
import os
import re
import time
from datetime import date

import anthropic
import openai
import requests

logger = logging.getLogger(__name__)

# Languages that need a second transcription pass with an explicit language code
# and a script-preserving prompt. Without this, Whisper sometimes translates
# Arabic and Hebrew into English instead of transcribing them.
_NEEDS_SECOND_PASS = {"arabic", "hebrew"}

# verbose_json language name → ISO 639-1 code
_LANG_CODES = {"arabic": "ar", "hebrew": "he", "english": "en"}


def detect_language_from_text(text: str) -> str:
    """Infer language from the Unicode script used in the transcription output.

    Used as a fallback when the API does not return language metadata.
    Returns an ISO 639-1 code: 'ar', 'he', or 'en'.
    """
    if re.search(r"[؀-ۿ]", text):
        return "ar"
    if re.search(r"[֐-׿]", text):
        return "he"
    return "en"


_SPEECHMATICS_API = "https://asr.api.speechmatics.com/v2"


def _transcribe_arabic_speechmatics(file_path: str) -> str | None:
    """Submit audio to Speechmatics and return the Arabic transcript.

    Speechmatics uses a single 'ar' language pack that covers all dialects
    (Levantine, Gulf, Egyptian, MSA) without normalization to MSA — unlike
    Whisper which is biased toward MSA regardless of prompts.

    Returns None if SPEECHMATICS_API_KEY is not set or the request fails,
    so the caller can fall back to the Whisper second pass.
    """
    api_key = os.environ.get("SPEECHMATICS_API_KEY")
    if not api_key:
        return None

    headers = {"Authorization": f"Bearer {api_key}"}
    config = {"type": "transcription", "transcription_config": {"language": "ar"}}

    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                f"{_SPEECHMATICS_API}/jobs",
                headers=headers,
                files={"config": (None, json.dumps(config)), "data_file": f},
                timeout=30,
            )
        resp.raise_for_status()
        job_id = resp.json()["id"]
        logger.info("Speechmatics job submitted: %s", job_id)
    except Exception:
        logger.exception("Speechmatics job submission failed")
        return None

    # Poll until done. Voice notes are short; typically completes in a few seconds.
    for attempt in range(30):
        time.sleep(2)
        try:
            status_resp = requests.get(
                f"{_SPEECHMATICS_API}/jobs/{job_id}",
                headers=headers,
                timeout=10,
            )
            status_resp.raise_for_status()
            status = status_resp.json().get("job", {}).get("status")
            logger.info(
                "Speechmatics job %s status: %s (attempt %d)",
                job_id,
                status,
                attempt + 1,
            )
            if status == "done":
                break
            if status in ("rejected", "deleted"):
                logger.error(
                    "Speechmatics job %s ended with status: %s", job_id, status
                )
                return None
        except Exception:
            logger.exception("Speechmatics poll failed on attempt %d", attempt + 1)
            return None
    else:
        logger.error("Speechmatics job %s timed out after polling", job_id)
        return None

    try:
        text_resp = requests.get(
            f"{_SPEECHMATICS_API}/jobs/{job_id}/transcript",
            headers=headers,
            params={"format": "txt"},
            timeout=10,
        )
        text_resp.raise_for_status()
        # Force UTF-8: requests defaults to ISO-8859-1 for text/plain when the
        # Content-Type header omits charset, which garbles Arabic.
        return text_resp.content.decode("utf-8").strip()
    except Exception:
        logger.exception("Speechmatics transcript fetch failed for job %s", job_id)
        return None


def build_transcription_prompt(language: str) -> str:
    """Return a language-specific context hint for Whisper's `prompt` parameter.

    The prompt is written in the target language so Whisper primes itself to
    produce output in that script, not English.
    """
    match language:
        case "ar":
            # Written in Arabic: primes Whisper to stay in Arabic script and
            # preserve Shami/Levantine dialect without converting to MSA.
            return (
                "هذا مقطع صوتي بالعامية الشامية. "
                "أرجو نقله حرفياً بالحروف العربية كما نُطق. "
                "لا تترجم. لا تحوّله إلى الفصحى. "
                "الكلمات الإنجليزية تبقى بالإنجليزية."
            )
        case "he":
            return (
                "זהו דיבור בעברית מדוברת. תמלל בכתב עברי בלבד. "
                "אל תתרגם לאנגלית. שמור על מילים באנגלית כפי שנאמרו."
            )
        case _:
            return ""


def transcribe_with_language_detection(file_path: str) -> dict:
    """Transcribe a voice note with automatic language detection.

    Strategy (Option A when metadata is available, Option B otherwise):
    1. First pass with response_format="verbose_json" to get the detected language.
    2. If Arabic or Hebrew: second pass with explicit language + script-preserving
       prompt so Whisper transcribes rather than translates.
    3. Otherwise use the first-pass text directly.

    Returns a dict with keys: text, detected_language, was_second_pass.
    """
    client = openai.OpenAI()

    with open(file_path, "rb") as audio:
        first = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio,
            response_format="verbose_json",
            # task defaults to "transcribe" — never set task="translate"
        )

    raw_lang = (getattr(first, "language", "") or "").lower()
    text = first.text.strip()

    # Use API language metadata when available; fall back to script inspection.
    if raw_lang:
        detected_language = _LANG_CODES.get(raw_lang, raw_lang)
    else:
        detected_language = detect_language_from_text(text)

    logger.info(
        "Transcription first pass: api_lang=%r → detected=%r, preview=%r",
        raw_lang,
        detected_language,
        text[:80],
    )

    was_second_pass = False
    if raw_lang in _NEEDS_SECOND_PASS or detected_language in ("ar", "he"):
        lang_code = "ar" if detected_language == "ar" or raw_lang == "arabic" else "he"
        was_second_pass = True

        if lang_code == "ar":
            # Speechmatics uses a dialect-aware Arabic model that doesn't normalize
            # to MSA — unlike Whisper which converges to MSA regardless of prompting.
            sm_text = _transcribe_arabic_speechmatics(file_path)
            if sm_text:
                logger.info("Speechmatics Arabic transcript: %r", sm_text[:120])
                text = sm_text
                detected_language = lang_code
            else:
                # Fall back to Whisper second pass if Speechmatics is unavailable.
                logger.info("Falling back to Whisper second pass for Arabic")
                prompt = build_transcription_prompt(lang_code)
                with open(file_path, "rb") as audio:
                    second = client.audio.transcriptions.create(
                        model="whisper-1",
                        file=audio,
                        language=lang_code,
                        prompt=prompt,
                    )
                text = second.text.strip()
                detected_language = lang_code
        else:
            # Hebrew: Whisper handles this acceptably.
            prompt = build_transcription_prompt(lang_code)
            logger.info(
                "Transcription second pass: lang=%s, prompt=%r", lang_code, prompt[:60]
            )
            with open(file_path, "rb") as audio:
                second = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio,
                    language=lang_code,
                    prompt=prompt,
                    # task defaults to "transcribe" — never set task="translate"
                )
            text = second.text.strip()
            detected_language = lang_code

    logger.info(
        "Transcription done: lang=%s, second_pass=%s, text=%r",
        detected_language,
        was_second_pass,
        text[:120],
    )
    return {
        "text": text,
        "detected_language": detected_language,
        "was_second_pass": was_second_pass,
    }


# NOTE: `#directive` (a standing instruction to the app) is deliberately absent here.
# It is rare and high-stakes — misfiling silently corrupts agenda logic — so it is
# *declared* via a `directive:`/`policy:` prefix, never *inferred*. Keeping it out of
# this enum removes the semantic-magnet effect the old `values` tag had, where any
# first-person value statement ("I care about X") got pulled in. Such personal/emotional
# content now falls through to `checkin`/`insight` as intended.
_BASE_CLASSIFICATION_TAGS = [
    ("insight", "a new realization, lesson, or pattern noticed"),
    ("hypothesis", "an empirical claim to test (I think X causes Y)"),
    ("note", "a general observation or reference note"),
    ("task", "something to do or action item"),
    ("wrong", "a mistake, problem, or thing that went badly"),
    ("win", "an accomplishment or positive outcome"),
    ("backlog", "a someday/maybe idea, not urgent"),
    ("checkin", "emotional, physical, or energy status update"),
    ("log", "anything else (default fallback)"),
]


async def classify_entry(text: str, extra_tags: list[dict] | None = None) -> str:
    """Classify a log entry as a tag when no explicit prefix was detected.

    `extra_tags` — additional {"tag", "description"} dicts contributed by plugins
    (e.g. grocery, food). They extend the enum so the LLM can route to them.
    """
    plugin_pairs = [(t["tag"], t["description"]) for t in (extra_tags or [])]
    all_pairs = _BASE_CLASSIFICATION_TAGS + plugin_pairs
    enum_values = [tag for tag, _ in all_pairs]
    prompt_lines = [f"{tag} — {desc}" for tag, desc in all_pairs]

    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        tools=[
            {
                "name": "classify",
                "description": "Classify a personal log entry into one tag",
                "input_schema": {
                    "type": "object",
                    "properties": {"tag": {"type": "string", "enum": enum_values}},
                    "required": ["tag"],
                },
            }
        ],
        tool_choice={"type": "tool", "name": "classify"},
        messages=[
            {
                "role": "user",
                "content": (
                    "Classify this personal log entry as exactly one tag:\n\n"
                    f'"{text}"\n\n' + "\n".join(prompt_lines)
                ),
            }
        ],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input["tag"]
    return "log"


async def parse_queue_entry(text: str) -> dict | None:
    """Extract {day, item} from a queue/defer request via Claude tool use."""
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        tools=[
            {
                "name": "queue_agenda_item",
                "description": "Extract the target day and agenda item text from a queue/defer request",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "day": {
                            "type": "string",
                            "description": "Day name or date, e.g. 'Sunday', 'Monday', 'tomorrow'",
                        },
                        "item": {
                            "type": "string",
                            "description": "The agenda item text to queue",
                        },
                    },
                    "required": ["day", "item"],
                },
            }
        ],
        tool_choice={"type": "tool", "name": "queue_agenda_item"},
        messages=[
            {"role": "user", "content": f"Today is {date.today()}. Parse this: {text}"}
        ],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return None
