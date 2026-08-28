"""Tests for the deterministic (pre-LLM) classification layer in text_router.

Classification is load-bearing: tags drive habit streaks, food macros, burnout
detection, and (via `#directive`) agenda weighting. These tests lock in the rules
that must NOT silently regress:

- A `#directive` is *declared*, never inferred — only an explicit `directive:`/
  `policy:` prefix produces it. This is the fix for the old `#values` "semantic
  magnet" that swallowed any first-person value statement.
- Personal/emotional value-laden statements must NOT be classified as directives by
  the deterministic layer; they fall through to `log` so the LLM can route them to
  checkin/insight.
"""

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from context import Context
from habit_handlers import HabitStore, exact_habit_match
from logs import Logs
from text_router import (
    TextRouter,
    _ADD_HABIT_PREFIX_RE,
    _ADD_HABIT_SUFFIX_RE,
    _AGENDA_DEST_RE,
    _agenda_extraction_is_suspect,
    _extract_agenda_item,
    _intent_gate_matches,
    _is_nutrition_breakdown,
    _parse_metric_body,
    _food_negative_signal,
    _PARTIAL_FRACTION_WORDS,
    _REMOVE_HABIT_PREFIX_RE,
    _REMOVE_HABIT_SUFFIX_RE,
    _RETRACT_BARE_RE,
    _RETRACT_NAMED_RE,
    _RETRACT_PARTIAL_RE,
    _STOP_TRACKING_HABIT_RE,
)

classify = TextRouter._classify_entry


def test_directive_prefix_is_declared():
    """`directive:` and `policy:` declare a #directive and strip the prefix."""
    assert classify("directive: more is not always better") == (
        "directive",
        "more is not always better",
    )
    assert classify("policy: don't launder the agenda through support language") == (
        "directive",
        "don't launder the agenda through support language",
    )


def test_value_laden_personal_statement_is_not_a_directive():
    """A first-person statement about what the user cares about is NOT a directive.

    This is the exact failure mode of the old `#values` tag: 'I care about my mother'
    is personal content, not an instruction to the system. The deterministic layer must
    leave it as `log` so the LLM routes it to checkin/insight — it must never become a
    directive without the explicit prefix.
    """
    for text in (
        "I care about my mother",
        "my family's financial situation matters to me",
        "I value being present with my kids",
    ):
        tag, _ = classify(text)
        assert tag != "directive", f"{text!r} should not be a directive"
        assert tag == "log", f"{text!r} should fall through to the LLM as 'log'"


def test_ambiguous_insight_or_checkin_falls_through_to_llm():
    """An entry that's ambiguous between insight and checkin is left for the LLM.

    The deterministic layer only fires on explicit prefixes; a bare reflective sentence
    returns 'log' so `classify_entry` (Haiku) can decide between insight and checkin.
    """
    tag, content = classify("I notice I feel calmer on the days I walk before shul")
    assert tag == "log"
    assert content == "I notice I feel calmer on the days I walk before shul"


def test_values_prefix_no_longer_recognized():
    """The retired `values:` prefix no longer produces a tag of its own.

    Regression guard: a message opening with `values:` must not resurrect the old
    magnet tag. It falls through as `log` (the leading word is just treated as text).
    """
    tag, _ = classify("values: I want to be more patient")
    assert tag != "values"
    assert tag != "directive"


# --- Rules-first pass: nutrition, habit, metric (no LLM call) ---


def test_structured_nutrition_routes_to_food_without_llm():
    """An entry with an explicit calorie + macro breakdown is tagged #food deterministically."""
    tag, content = classify("chicken bowl — 550 kcal, 40g protein")
    assert tag == "food"
    assert content == "chicken bowl — 550 kcal, 40g protein"
    # The natural-language phrasing that had been leaking into #log now routes to food.
    assert (
        classify(
            "drinking a protein-enhanced coffee, 25 grams of protein and 130 calories"
        )[0]
        == "food"
    )


def test_calorie_mention_without_macros_is_not_food():
    """A calorie figure alone (no macro grams) is not enough to force #food.

    'burned 500 calories on my walk' is a checkin, not a meal — it must fall through.
    """
    assert classify("burned 500 calories on my walk today")[0] == "log"


def test_metric_parser_handles_plural_and_possessive():
    """Regression: 'metrics:' (plural) and a possessive filler word used to drop to #log,
    silently losing the reading. Key/value in either order must also parse."""
    assert _parse_metric_body("weight 92.9") == ("weight", 92.9, "", "92.9")
    assert _parse_metric_body("steps 12779") == ("steps", 12779.0, "", "12779")
    assert _parse_metric_body("8000 steps") == ("steps", 8000.0, "", "8000")
    assert _parse_metric_body("yesterday's steps 7095") == ("steps", 7095.0, "", "7095")


def test_metric_body_requires_a_number():
    """No numeric value → not a parseable metric (caller falls through to normal logging)."""
    assert _parse_metric_body("feeling good") is None


def test_nutrition_breakdown_predicate():
    assert _is_nutrition_breakdown("550 kcal, 40g protein")
    assert not _is_nutrition_breakdown("I feel tired today")


# --- Explicit agenda destination ("... to my agenda") ---


def test_agenda_destination_is_detected_and_item_extracted():
    """Regression: a stated destination ('to my agenda') used to be discarded by the
    classifier, which tagged the utterance #task and dropped it so it never reached
    /agenda. The rules-first match must fire and pull out the item text."""
    u = "Add goal reflection to my agenda and it should include putting it in personal ops."
    assert _AGENDA_DEST_RE.search(u.lower())
    assert _extract_agenda_item(u) == "goal reflection"

    for text, item in [
        ("put the dentist call on the agenda", "the dentist call"),
        ("add finish the deck to my agenda", "finish the deck"),
        ("note buy milk on my agenda", "buy milk"),
    ]:
        assert _AGENDA_DEST_RE.search(text.lower()), text
        assert _extract_agenda_item(text) == item, text


def test_agenda_destination_does_not_fire_without_the_phrase():
    """The phrase must be an explicit destination — an ordinary mention of the word
    'agenda' elsewhere, or none at all, must not trigger agenda routing."""
    assert not _AGENDA_DEST_RE.search("the meeting agenda was long")
    assert not _AGENDA_DEST_RE.search("add milk to the shopping list")


def test_agenda_extraction_flags_the_rambling_voice_transcript_bug():
    """Regression: a rambling voice transcript that mentions 'agenda' twice used to
    have its regex extraction silently commit meta-commentary ('whatever I missed')
    as a literal agenda item. The suspicion check must catch this so the caller
    escalates to the LLM instead of trusting the regex blindly."""
    text = (
        "Put whatever I missed on my agenda today on my agenda tomorrow just "
        "because yeah I think that's like I think what was on my agenda was worth "
        "doing it just wasn't what I needed to do today."
    )
    item = _extract_agenda_item(text)
    assert item == "whatever I missed"  # the exact bad extraction that shipped
    assert _agenda_extraction_is_suspect(text, item)


def test_agenda_extraction_not_suspect_for_the_clean_common_case():
    """The common single-mention, named-task case must NOT pay for an LLM round-trip."""
    text = "add finish the deck to my agenda"
    item = _extract_agenda_item(text)
    assert item == "finish the deck"
    assert not _agenda_extraction_is_suspect(text, item)


def _router_with_planner(parse_agenda_item, logs=None):
    r = TextRouter.__new__(TextRouter)
    r.planner = types.SimpleNamespace(parse_agenda_item=parse_agenda_item)
    r.logs = logs
    return r


class _FakeLogs:
    def __init__(self):
        self.events = []

    def log_label_event(self, ref_entry_id, event_type, from_label, to_label, **kw):
        self.events.append((ref_entry_id, event_type, from_label, to_label, kw))


def test_resolve_agenda_item_escalates_and_asks_for_clarification():
    """When the LLM also can't name a concrete task, the caller must ask the user
    instead of committing the regex's bad guess — the actual fix for the bug above."""

    async def fake_parse(text):
        return {"clarification_needed": True}

    r = _router_with_planner(fake_parse)
    item, needs_clarification = asyncio.run(
        r._resolve_agenda_item(
            "Put whatever I missed on my agenda today on my agenda tomorrow",
            "whatever I missed",
        )
    )
    assert needs_clarification
    assert item is None


def test_resolve_agenda_item_uses_llm_result_when_suspect():
    async def fake_parse(text):
        return {"item": "call the dentist"}

    r = _router_with_planner(fake_parse)
    item, needs_clarification = asyncio.run(
        r._resolve_agenda_item(
            "put whatever I forgot on my agenda on my agenda", "whatever I forgot"
        )
    )
    assert not needs_clarification
    assert item == "call the dentist"


def test_resolve_agenda_item_skips_llm_for_the_clean_case():
    async def fake_parse(text):
        raise AssertionError("must not call the LLM for an unambiguous extraction")

    r = _router_with_planner(fake_parse)
    item, needs_clarification = asyncio.run(
        r._resolve_agenda_item("add finish the deck to my agenda", "finish the deck")
    )
    assert not needs_clarification
    assert item == "finish the deck"


def test_resolve_agenda_item_logs_a_correction_when_llm_resolves_it():
    """The escalation must land in label_events so future examples accumulate —
    this is the corrections substrate the audit's multishot/eval-loop design
    depends on, reusing the same table the classifier's retrain loop feeds from."""

    async def fake_parse(text):
        return {"item": "call the dentist"}

    logs = _FakeLogs()
    r = _router_with_planner(fake_parse, logs=logs)
    asyncio.run(
        r._resolve_agenda_item(
            "put whatever I forgot on my agenda on my agenda", "whatever I forgot"
        )
    )
    assert len(logs.events) == 1
    ref_entry_id, event_type, from_label, to_label, kw = logs.events[0]
    assert ref_entry_id == 0
    assert event_type == "reclassify"
    assert from_label == "whatever I forgot"
    assert to_label == "call the dentist"
    assert kw["call_site"] == "agenda_extraction"
    assert kw["source"] == "auto_escalation"


def test_resolve_agenda_item_logs_unresolved_when_llm_also_cant_tell():
    async def fake_parse(text):
        return {"clarification_needed": True}

    logs = _FakeLogs()
    r = _router_with_planner(fake_parse, logs=logs)
    asyncio.run(
        r._resolve_agenda_item(
            "Put whatever I missed on my agenda today on my agenda tomorrow",
            "whatever I missed",
        )
    )
    assert len(logs.events) == 1
    _, event_type, from_label, to_label, kw = logs.events[0]
    assert event_type == "unresolved"
    assert from_label == "whatever I missed"
    assert to_label == ""
    assert kw["call_site"] == "agenda_extraction"


def test_resolve_agenda_item_does_not_log_for_the_clean_case():
    async def fake_parse(text):
        raise AssertionError("must not call the LLM for an unambiguous extraction")

    logs = _FakeLogs()
    r = _router_with_planner(fake_parse, logs=logs)
    asyncio.run(
        r._resolve_agenda_item("add finish the deck to my agenda", "finish the deck")
    )
    assert logs.events == []


def _add_habit_name(text: str) -> str | None:
    m = _ADD_HABIT_PREFIX_RE.match(text.strip()) or _ADD_HABIT_SUFFIX_RE.match(
        text.strip()
    )
    return m.group(1).strip(" .,:;-") if m else None


def _remove_habit_name(text: str) -> str | None:
    m = (
        _REMOVE_HABIT_PREFIX_RE.match(text.strip())
        or _REMOVE_HABIT_SUFFIX_RE.match(text.strip())
        or _STOP_TRACKING_HABIT_RE.match(text.strip())
    )
    return m.group(1).strip(" .,:;-") if m else None


def test_add_habit_phrasing_extracts_name():
    """'add X habit' and 'add habit X' (and create/start/new variants) are both
    recognised, whichever side of the name the word 'habit' lands on."""
    for text, name in [
        ("add stretch habit", "stretch"),
        ("add a stretch habit", "stretch"),
        ("Start meditation habit.", "meditation"),
        ("new habit: cold shower", "cold shower"),
        ("add habit called drink water", "drink water"),
        ("create habit water drinking", "water drinking"),
    ]:
        assert _add_habit_name(text) == name, text


def test_add_habit_phrasing_does_not_fire_on_unrelated_add_requests():
    """An ordinary 'add X to my agenda'/'add to calendar' utterance must not be
    misread as a habit-creation request just because it starts with 'add'."""
    for text in [
        "add milk to my agenda",
        "add task to buy groceries",
        "add to calendar: dentist tomorrow",
        "new event: dentist",
    ]:
        assert _add_habit_name(text) is None, text


def test_remove_habit_phrasing_extracts_name():
    """'remove/delete/drop/untrack habit X', its 'X habit' mirror, and
    'stop tracking X' are all recognised as the natural-language delete."""
    for text, name in [
        ("remove habit stretch", "stretch"),
        ("remove stretch habit", "stretch"),
        ("delete the stretch habit", "stretch"),
        ("stop tracking cold shower", "cold shower"),
        ("untrack habit: meditation", "meditation"),
    ]:
        assert _remove_habit_name(text) == name, text


def test_remove_habit_phrasing_does_not_fire_on_unrelated_text():
    assert _remove_habit_name("remove the milk from the shopping list") is None
    assert _remove_habit_name("stretch habit was hard today") is None


def test_exact_habit_match_is_conservative(tmp_path):
    """A bare known-habit string resolves to the canonical name (no LLM); a sentence that
    merely contains the words does not — avoids false positives in the classifier."""
    store = HabitStore(Logs(str(tmp_path)).db, Context(tmp_path))
    store.add("Daily walk")
    db = store.db
    assert exact_habit_match("daily walk", db) == "Daily walk"
    assert exact_habit_match("Daily Walk", db) == "Daily walk"
    assert exact_habit_match("I should do my daily walk later", db) is None
    assert exact_habit_match("some unrelated note", db) is None


# --- Food intent gate: narrative mention vs. a report of eating ---


def test_negative_signal_blocks_order_and_arrival_narrative():
    """'ordered a pizza and it got here cold' is a complaint, not a log event."""
    assert _food_negative_signal("ordered a pizza and it got here cold") is True


def test_negative_signal_blocks_descriptive_predicate_despite_leading_had():
    """'had a rough day, pizza was cold' starts with 'had' (a positive-looking verb)
    but is narrative — the gate must catch this via the descriptive-predicate pattern,
    not a naive positive-verb match that would misfire on 'had a rough day'."""
    assert _food_negative_signal("had a rough day, pizza was cold") is True


def test_negative_signal_does_not_block_a_real_consumption_report():
    """'just ate a protein shake' has no narrative/complaint cue — must log normally."""
    assert _food_negative_signal("just ate a protein shake") is False


def test_negative_signal_blocks_third_person_mentions():
    assert _food_negative_signal("she ordered a burger for lunch") is True


def test_negative_signal_blocks_hypothetical_framing():
    assert _food_negative_signal("thinking about ordering pizza tonight") is True


# --- Explicit-only food retraction: pattern matching ---


def test_retract_bare_forms_match():
    for text in ("#unlog", "unlog it", "scratch that", "Scratch that."):
        assert _RETRACT_BARE_RE.match(text.strip()), text


def test_retract_bare_forms_do_not_match_named_or_unrelated_text():
    for text in ("unlog the shake", "didn't finish the report", "I scratched my arm"):
        assert not _RETRACT_BARE_RE.match(text.strip()), text


def test_retract_named_forms_extract_item():
    assert _RETRACT_NAMED_RE.match("unlog the shake").group(1) == "shake"
    assert _RETRACT_NAMED_RE.match("didn't finish the pizza").group(1) == "pizza"
    assert _RETRACT_NAMED_RE.match("didn't finish my homework").group(1) == "homework"


def test_retract_partial_form_extracts_fraction_word_and_item():
    m = _RETRACT_PARTIAL_RE.match("only ate about a third of the pizza")
    assert m is not None
    assert m.group(1) == "third"
    assert m.group(2) == "pizza"
    assert _PARTIAL_FRACTION_WORDS[m.group(1)] == 1 / 3


def test_retract_partial_form_requires_only_ate_or_had_prefix():
    assert _RETRACT_PARTIAL_RE.match("I finished a third of my book") is None


# --- General intent-dispatch step (indirectly-phrased known actions) ---


def test_intent_gate_matches_relevant_keywords():
    assert _intent_gate_matches(
        "today is friday, so if you don't have candle lighting time set, please set it"
    )
    assert _intent_gate_matches("don't let me forget to call the dentist")
    assert _intent_gate_matches("I have a meeting thursday at 2, add it")


def test_intent_gate_does_not_match_plain_log_text():
    assert not _intent_gate_matches("i notice i feel calmer on days i walk before shul")
    assert not _intent_gate_matches("finished the deck today, feels great")


class _Replies:
    def __init__(self):
        self.messages = []

    async def __call__(self, text, **kw):
        self.messages.append(text)


def _router_full(planner=None, shabbat=None, reminders=None, gcal=None, logs=None):
    r = TextRouter.__new__(TextRouter)
    r.planner = planner
    r.shabbat = shabbat
    r.reminders = reminders
    r.gcal = gcal
    r.logs = logs
    r._awaiting_candles = {}
    r._awaiting_time = {}
    return r


def test_dispatch_candle_lighting_confirms_when_already_set():
    import datetime as dt

    shabbat = types.SimpleNamespace(
        has_manual_candle_lighting=lambda: True,
        load_candle_lighting=lambda: dt.time(19, 5),
    )
    r = _router_full(shabbat=shabbat)
    replies = _Replies()
    asyncio.run(r._dispatch_candle_lighting(1, replies))
    assert replies.messages == ["🕯️ Candle lighting is already set for 19:05 today."]
    assert 1 not in r._awaiting_candles


def test_dispatch_candle_lighting_prompts_when_unset():
    shabbat = types.SimpleNamespace(has_manual_candle_lighting=lambda: False)
    r = _router_full(shabbat=shabbat)
    replies = _Replies()
    asyncio.run(r._dispatch_candle_lighting(1, replies))
    assert r._awaiting_candles[1] is True
    assert replies.messages == ["🕯️ What time is candle lighting?"]


def test_try_dispatch_known_intent_routes_to_candle_lighting():
    async def detect(text):
        return "candle_lighting"

    shabbat = types.SimpleNamespace(has_manual_candle_lighting=lambda: False)
    logs = _FakeLogs()
    r = _router_full(
        planner=types.SimpleNamespace(detect_action_intent=detect),
        shabbat=shabbat,
        logs=logs,
    )
    replies = _Replies()
    dispatched = asyncio.run(
        r._try_dispatch_known_intent(
            "if you don't have candle lighting time set, please set it",
            "if you don't have candle lighting time set, please set it",
            1,
            replies,
        )
    )
    assert dispatched
    assert r._awaiting_candles[1] is True
    assert len(logs.events) == 1
    ref_entry_id, event_type, from_label, to_label, kw = logs.events[0]
    assert event_type == "dispatched"
    assert to_label == "candle_lighting"
    assert kw["call_site"] == "intent_router"
    assert kw["source"] == "auto_intent"


def test_try_dispatch_known_intent_routes_to_reminder():
    async def detect(text):
        return "reminder"

    async def parse_reminder(text):
        return {
            "text": "call the dentist",
            "type": "once",
            "time": "15:00",
            "date": "2026-08-28",
        }

    added = []

    def add(**kw):
        added.append(kw)
        return {
            "type": "once",
            "date": "2026-08-28",
            "time": "15:00",
            "text": kw["text"],
        }

    r = _router_full(
        planner=types.SimpleNamespace(
            detect_action_intent=detect, parse_reminder=parse_reminder
        ),
        reminders=types.SimpleNamespace(add=add),
    )
    replies = _Replies()
    dispatched = asyncio.run(
        r._try_dispatch_known_intent(
            "don't let me forget to call the dentist tomorrow",
            "don't let me forget to call the dentist tomorrow",
            1,
            replies,
        )
    )
    assert dispatched
    assert len(added) == 1
    assert any("Reminder set" in m for m in replies.messages)


def test_try_dispatch_known_intent_routes_to_calendar_event():
    async def detect(text):
        return "calendar_event"

    async def parse_event(text):
        return {
            "summary": "Dentist",
            "date": "2026-08-28",
            "start_time": "10:00",
            "duration_minutes": 60,
        }

    def create_event(summary, start_dt, duration, description):
        return {"htmlLink": "https://calendar.example/evt"}

    r = _router_full(
        planner=types.SimpleNamespace(
            detect_action_intent=detect, parse_event=parse_event
        ),
        gcal=types.SimpleNamespace(create_event=create_event),
    )
    replies = _Replies()
    dispatched = asyncio.run(
        r._try_dispatch_known_intent(
            "I have a dentist appointment thursday at 2, put it on my calendar",
            "i have a dentist appointment thursday at 2, put it on my calendar",
            1,
            replies,
        )
    )
    assert dispatched
    assert any("Created" in m for m in replies.messages)


def test_try_dispatch_known_intent_skips_llm_when_gate_misses():
    async def detect(text):
        raise AssertionError("must not call the LLM when no keyword matched")

    r = _router_full(planner=types.SimpleNamespace(detect_action_intent=detect))
    replies = _Replies()
    dispatched = asyncio.run(
        r._try_dispatch_known_intent(
            "finished the deck today, feels great",
            "finished the deck today, feels great",
            1,
            replies,
        )
    )
    assert not dispatched
    assert replies.messages == []


def test_try_dispatch_known_intent_falls_through_when_llm_says_none():
    async def detect(text):
        return None

    logs = _FakeLogs()
    r = _router_full(
        planner=types.SimpleNamespace(detect_action_intent=detect), logs=logs
    )
    replies = _Replies()
    dispatched = asyncio.run(
        r._try_dispatch_known_intent(
            "there's a meeting in the show I'm watching",
            "there's a meeting in the show i'm watching",
            1,
            replies,
        )
    )
    assert not dispatched
    assert replies.messages == []
    assert logs.events == []
