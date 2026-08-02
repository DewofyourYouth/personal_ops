import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
from habit_tracker import (SHABBAT, _is_due, anchor_flow_state, compute_streak,
                           load_habit_logs, missed_last_due_day, recent_chain)
from logs import Logs


def _write_habit_days(logs: Logs, name: str, days_back: range) -> None:
    """Write a habit entry directly into each day's JSONL (compute_* read JSONL)."""
    today = date.today()
    for i in days_back:
        d = today - timedelta(days=i)
        path = Path(logs.log_dir) / f"{d}.jsonl"
        with open(path, "a") as f:
            f.write(
                json.dumps(
                    {"ts": f"{d}T09:00:00+03:00", "tag": "habit", "content": name}
                )
                + "\n"
            )


def test_is_due():
    """Due-day logic treats Shabbat as skipped and honors weekday filters."""
    # Find a known Monday and Saturday to test deterministically.
    monday = date(2026, 6, 1)  # a Monday
    saturday = date(2026, 6, 6)  # a Saturday (Shabbat)
    assert _is_due(monday, None) is True  # every-day habit, non-Shabbat
    assert _is_due(saturday, None) is False  # Shabbat never counts
    assert _is_due(monday, [0, 2, 4]) is True  # Monday is in Mon/Wed/Fri
    assert _is_due(monday, [1, 3]) is False  # not a Tue/Thu day


def test_is_due_respects_pause_window():
    """A day inside an active [from, until] pause is non-due, exactly like Shabbat;
    days outside the window (before it starts or after it ends) are unaffected."""
    sunday = date(2026, 5, 31)  # day before the pause starts
    monday = date(2026, 6, 1)  # inside the pause
    tuesday = date(2026, 6, 2)  # inside the pause (inclusive end)
    wednesday = date(2026, 6, 3)  # day after the pause ends
    window = (monday, tuesday)
    assert _is_due(sunday, None, paused=window) is True  # before the window
    assert _is_due(monday, None, paused=window) is False  # inclusive start
    assert _is_due(tuesday, None, paused=window) is False  # inclusive end
    assert _is_due(wednesday, None, paused=window) is True  # after the window


def test_is_due_pause_without_a_lower_bound_would_exempt_all_history():
    """Regression: a paused_until with no paused_from must never be passed through
    as if it bounded anything — this is exactly the bug where 'pause until next
    week' silently zeroed out a habit's entire pre-pause history. _is_due only
    accepts a (from, until) pair, so there is no way to pass an unbounded end."""
    ancient = date(2020, 1, 1)
    monday = date(2026, 6, 1)
    tuesday = date(2026, 6, 2)
    # A day from years before the pause was ever set must still be due.
    assert _is_due(ancient, None, paused=(monday, tuesday)) is True


def test_recent_chain_all_done(tmp_path):
    """recent_chain returns a full hit chain when every recent due day was logged."""
    logs = Logs(str(tmp_path))
    _write_habit_days(logs, "Daily walk", range(0, 20))
    chain = recent_chain(logs, "Daily walk", due_weekdays=None, n=14)
    assert len(chain) == 14
    assert all(chain)  # logged every day → every due day is a hit


def test_recent_chain_shows_gaps(tmp_path):
    """recent_chain marks an unlogged recent due day as a miss."""
    logs = Logs(str(tmp_path))
    _write_habit_days(logs, "Daily walk", range(0, 20))  # all done...
    # ...then blank out today's log file so the most recent due day is a miss.
    (Path(logs.log_dir) / f"{date.today()}.jsonl").write_text("")
    chain = recent_chain(logs, "Daily walk", due_weekdays=None, n=14)
    if _is_due(date.today(), None):  # only meaningful when today is a due day
        assert chain[-1] is False


def test_missed_last_due_day(tmp_path):
    """missed_last_due_day distinguishes never-logged from recently completed habits."""
    logs = Logs(str(tmp_path))
    assert missed_last_due_day(logs, "Daily walk", None) is True  # nothing logged ever
    _write_habit_days(logs, "Daily walk", range(0, 20))
    assert missed_last_due_day(logs, "Daily walk", None) is False  # prior day was done


def test_compute_streak_respects_due_days(tmp_path):
    """compute_streak counts only due-day history when calculating streaks."""
    logs = Logs(str(tmp_path))
    _write_habit_days(logs, "Daily walk", range(0, 30))
    current, longest = compute_streak(logs, "Daily walk", due_weekdays=None)
    assert current >= 20 and longest >= current


_HABITS_TABLE = """
CREATE TABLE habits (
    id INTEGER PRIMARY KEY AUTOINCREMENT, section TEXT, name TEXT,
    days TEXT DEFAULT '', tracked INTEGER DEFAULT 1, position INTEGER DEFAULT 0,
    cue TEXT DEFAULT '', identity TEXT DEFAULT '',
    paused_from TEXT DEFAULT '', paused_until TEXT DEFAULT ''
)
"""


def test_struggling_habits(tmp_path):
    """struggling_habits finds started habits that missed the recent success threshold."""
    from habit_tracker import struggling_habits

    logs = Logs(str(tmp_path))
    logs.db.execute(_HABITS_TABLE)
    logs.db.execute(
        "INSERT INTO habits (section,name,tracked) VALUES ('S','Daily walk',1)"
    )
    logs.db.execute("INSERT INTO habits (section,name,tracked) VALUES ('S','Anki',1)")
    logs.db.execute(
        "INSERT INTO habits (section,name,tracked) VALUES ('S','Stretch',1)"
    )
    today = date.today()
    # Daily walk: done every recent day → healthy, not struggling.
    for i in range(0, 20):
        d = today - timedelta(days=i)
        logs.db.insert_entry(
            f"{d}T09:00:00+03:00", d.isoformat(), "habit", "Daily walk"
        )
    # Anki: done only 25–34 days ago → has a past streak but missed the recent window.
    for i in range(25, 35):
        d = today - timedelta(days=i)
        logs.db.insert_entry(f"{d}T09:00:00+03:00", d.isoformat(), "habit", "Anki")
    # Stretch: never done → excluded (not-yet-started, not failing).
    names = [s["name"] for s in struggling_habits(logs, window=14, threshold=0.5)]
    assert "Anki" in names
    assert "Daily walk" not in names
    assert "Stretch" not in names


def test_format_habits_for_prompt(tmp_path):
    """Prompt formatting groups tracked habits and includes cue metadata."""
    from habit_tracker import format_habits_for_prompt

    logs = Logs(str(tmp_path))
    logs.db.execute(_HABITS_TABLE)
    logs.db.execute(
        "INSERT INTO habits (section,name,days,tracked,cue) VALUES "
        "('Anchors','06:15 Yerushalmi chavrusa','',1,'solo-first')"
    )
    logs.db.execute(
        "INSERT INTO habits (section,name,days,tracked) VALUES ('Anchors','21:00 Daf Yomi','',1)"
    )
    logs.db.execute(
        "INSERT INTO habits (section,name,days,tracked) VALUES ('Off','Hidden',',',0)"
    )
    out = format_habits_for_prompt(logs.db)
    assert "### Anchors" in out
    assert "06:15 Yerushalmi chavrusa" in out
    assert "cue: solo-first" in out
    assert "Hidden" not in out  # untracked habits are excluded


def test_habit_notes(tmp_path):
    """Habit notes are stored case-insensitively and returned newest-first."""
    import sys
    from pathlib import Path as _P

    sys.path.insert(0, str(_P(__file__).parent.parent / "ops"))
    from context import Context
    from habit_handlers import HabitStore

    logs = Logs(str(tmp_path))
    store = HabitStore(logs.db, Context(tmp_path))
    store.add_note("Strength training", "shoulder felt off, went light")
    store.add_note("Strength training", "back to normal")
    store.add_note("Daf Yomi", "finished the masechta")

    s_notes = store.notes_for("strength training")  # case-insensitive
    assert len(s_notes) == 2
    assert s_notes[0]["note"] == "back to normal"  # newest first
    assert len(store.recent_notes(days=7)) == 3


def test_bonus_day_done_counts_toward_streak(tmp_path):
    """Doing a habit on a non-due day counts as a bonus day in the streak."""
    # Regression: doing a habit on a non-due day (Shabbat) should *extend* the streak,
    # not be skipped. Logging every day for two weeks always spans at least one Shabbat,
    # so all 14 days must count.
    logs = Logs(str(tmp_path))
    _write_habit_days(logs, "Daf Yomi", range(0, 14))
    current, _ = compute_streak(logs, "Daf Yomi", due_weekdays=None)
    assert current == 14


def test_quiet_non_due_day_does_not_break_streak(tmp_path):
    """A non-due day without a log is transparent and does not break the streak."""
    # The other half of the rule: a Shabbat with nothing logged is transparent — it
    # neither counts nor breaks. Done on every non-Shabbat day → streak spans the gaps.
    logs = Logs(str(tmp_path))
    today = date.today()
    due_days = [
        i for i in range(0, 21) if (today - timedelta(days=i)).weekday() != SHABBAT
    ]
    _write_habit_days(logs, "Daf Yomi", due_days)
    current, _ = compute_streak(logs, "Daf Yomi", due_weekdays=None)
    assert current == len(due_days)


def test_single_missed_due_day_does_not_break_streak(tmp_path):
    """Regression: one missed due day must not break the streak (takes two consecutive)."""
    logs = Logs(str(tmp_path))
    today = date.today()
    # Collect 6 consecutive non-Shabbat due days going back from today.
    due_days_back: list[int] = []
    i = 0
    while len(due_days_back) < 6:
        if _is_due(today - timedelta(days=i), None):
            due_days_back.append(i)
        i += 1
    # Done on all of them except the 3rd most recent — a single miss in the middle.
    logged_by_day = {
        (today - timedelta(days=d)).isoformat(): ["daf yomi"]
        for idx, d in enumerate(due_days_back)
        if idx != 2
    }
    current, _ = compute_streak(
        logs, "daf yomi", due_weekdays=None, logged_by_day=logged_by_day
    )
    assert current > 0, "single missed due day must not break the current streak"


def test_two_consecutive_misses_break_streak(tmp_path):
    """Two consecutive missed due days break the streak."""
    logs = Logs(str(tmp_path))
    today = date.today()
    # Collect 7 consecutive non-Shabbat due days going back from today.
    due_days_back: list[int] = []
    i = 0
    while len(due_days_back) < 7:
        if _is_due(today - timedelta(days=i), None):
            due_days_back.append(i)
        i += 1
    # Done today (index 0) and days 3+ only; miss indices 1 and 2 (two consecutive).
    logged_by_day = {
        (today - timedelta(days=d)).isoformat(): ["daf yomi"]
        for idx, d in enumerate(due_days_back)
        if idx not in (1, 2)
    }
    current, _ = compute_streak(
        logs, "daf yomi", due_weekdays=None, logged_by_day=logged_by_day
    )
    assert current == 1, (
        "two consecutive misses must break the streak; only today should count"
    )


# --- SQLite path tests (production path: logs.write → load_habit_logs → compute_streak) ---
# These tests use a fixed reference Saturday and pin `today` so they're day-independent.

# A known Saturday to anchor all date arithmetic.
_SATURDAY = date(2026, 6, 13)  # Saturday
_SUNDAY = date(2026, 6, 14)  # Sunday (the day after motzei Shabbat)
_MONDAY = date(2026, 6, 15)  # Monday


def _write_habit_db(logs: Logs, name: str, dates: list[date]) -> None:
    """Write habit entries to SQLite (mimicking the production logs.write() path)."""
    for d in dates:
        logs.db.insert_entry(f"{d}T21:30:00+03:00", d.isoformat(), "habit", name)


def test_saturday_log_counts_in_sqlite_path(tmp_path):
    """Regression: motzei Shabbat logs via SQLite count toward the streak.

    Scenario: user logs on Saturday night (motzei Shabbat) and checks streak on Sunday
    before logging Sunday's habit. The Saturday entry is a bonus day, Sunday is one miss
    (forgiven), so the streak should include the full run ending Saturday.
    """
    logs = Logs(str(tmp_path))
    # Seven days: Sun 6/7, Mon 6/8, Tue 6/9, Wed 6/10, Thu 6/11, Fri 6/12, Sat 6/13.
    days = [_SATURDAY - timedelta(days=i) for i in range(7)]
    _write_habit_db(logs, "Daf Yomi", days)

    logged_by_day = {d.isoformat(): ["daf yomi"] for d in days}
    # Pin today = Sunday: one miss (Sunday not yet logged), Saturday is bonus → forgiven.
    current, _ = compute_streak(
        logs, "Daf Yomi", due_weekdays=None, logged_by_day=logged_by_day, today=_SUNDAY
    )
    assert current == 7, (
        "7 consecutive days ending Saturday, checking Sunday: streak must be 7"
    )


def test_saturday_night_log_extends_existing_streak(tmp_path):
    """Logging on Saturday night (motzei Shabbat) extends a streak that ended Friday.

    Scenario: user had a streak Mon–Fri, logged Saturday night, then checks on Sunday
    before logging Sunday. One miss (Sunday) is forgiven; streak should include Saturday.
    This is the exact user-reported scenario: 'logged after Shabbat, streak didn't count.'
    """
    logs = Logs(str(tmp_path))
    # Log Mon 6/8 through Sat 6/13 (6 days; Saturday is bonus).
    days = [_SATURDAY - timedelta(days=i) for i in range(6)]
    _write_habit_db(logs, "Daf Yomi", days)

    logged_by_day = {d.isoformat(): ["daf yomi"] for d in days}
    # Check on Sunday: Sunday is a single miss (forgiven), Saturday is bonus+done.
    current, _ = compute_streak(
        logs, "Daf Yomi", due_weekdays=None, logged_by_day=logged_by_day, today=_SUNDAY
    )
    # Fri (due) + Sat (bonus) + forgiven Sun → streak must be at least 5.
    assert current >= 5, (
        "Saturday night log must count toward streak when checked Sunday before logging"
    )


def test_pause_prevents_streak_break_during_vacation(tmp_path):
    """The motivating case: a habit paused for a stretch of unlogged days must not
    have its streak broken by them — the whole point of pausing is 'don't nag or
    penalize me while I'm intentionally not doing this'."""
    logs = Logs(str(tmp_path))
    today = date.today()
    # A 10-day streak, done every day, ending 10 days ago...
    _write_habit_days(logs, "5:30 wake", range(10, 20))
    # ...then a pause covering the gap from 9 days ago through today, nothing logged.
    paused = (today - timedelta(days=9), today)
    current, _ = compute_streak(logs, "5:30 wake", due_weekdays=None, paused=paused)
    # The gap is entirely non-due (paused), so it doesn't count as misses — the
    # pre-pause streak must survive intact once the paused gap is skipped over.
    assert current == 10, "a paused gap must not zero out the pre-pause streak"


def test_pause_window_has_a_lower_bound(tmp_path):
    """Regression for the actual bug this module had: a miss OLDER than the pause
    window (before paused_from) must still show up as a real miss — the pause
    only covers its own [from, until] dates, not everything before it too."""
    logs = Logs(str(tmp_path))
    tuesday = date(2026, 6, 2)
    monday = date(2026, 6, 1)
    sunday = date(2026, 5, 31)  # the day before the pause window starts
    chain = recent_chain(
        logs,
        "5:30 wake",
        due_weekdays=None,
        n=1,
        logged_by_day={},  # nothing logged anywhere
        paused=(monday, tuesday),
        today=tuesday,
    )
    # Tuesday and Monday are inside the pause + unlogged -> skipped, not misses.
    # Sunday is outside the pause window -> it's the one real due-day miss.
    assert chain == [False]


def test_recent_chain_skips_paused_days(tmp_path):
    """recent_chain excludes paused days from the hit/miss chain entirely (the
    same treatment Shabbat gets) — it reaches further back for real due days
    instead of counting an unlogged paused day as a miss."""
    logs = Logs(str(tmp_path))
    tuesday = date(2026, 6, 2)  # pinned, non-Shabbat reference date
    monday = date(2026, 6, 1)
    sunday = date(2026, 5, 31)
    # Logged on Sunday and Monday; Tuesday (today) is paused and unlogged.
    logged_by_day = {sunday.isoformat(): ["5:30 wake"], monday.isoformat(): ["5:30 wake"]}
    chain = recent_chain(
        logs,
        "5:30 wake",
        due_weekdays=None,
        n=2,
        logged_by_day=logged_by_day,
        paused=(tuesday, tuesday),  # pauses through today only
        today=tuesday,
    )
    # Tuesday is skipped (paused, unlogged) rather than showing up as a miss —
    # the last 2 due days found are Sunday and Monday, both hits.
    assert chain == [True, True]


def test_missed_last_due_day_ignores_paused_days(tmp_path):
    """A habit paused as of today (and yesterday) must not be flagged 'missed
    last time' just because the paused days have nothing logged — the check
    should reach past them to the last real due day, which was done."""
    logs = Logs(str(tmp_path))
    today = date.today()
    _write_habit_days(logs, "5:30 wake", range(2, 20))  # done up through 2 days ago
    paused = (today - timedelta(days=1), today)  # today + yesterday are paused
    assert (
        missed_last_due_day(logs, "5:30 wake", None, paused=paused, today=today)
        is False
    )


def _due_days_back(n: int, today: date) -> list[int]:
    """The offsets (days before `today`) of the next `n` due (non-Shabbat) days."""
    offsets: list[int] = []
    i = 0
    while len(offsets) < n:
        if _is_due(today - timedelta(days=i), None):
            offsets.append(i)
        i += 1
    return offsets


def test_anchor_flow_state_no_anchors_is_flow(tmp_path):
    """No tracked Anchors habits at all reads as 'flow' — nothing to steer."""
    logs = Logs(str(tmp_path))
    logs.db.execute(_HABITS_TABLE)
    state = anchor_flow_state(logs, today=_MONDAY)
    assert state == {"mode": "flow", "anchor": None, "chronic": False}


def test_anchor_flow_state_flow_when_landing(tmp_path):
    """Anchors landing at/above threshold reads as 'flow'."""
    logs = Logs(str(tmp_path))
    logs.db.execute(_HABITS_TABLE)
    logs.db.execute(
        "INSERT INTO habits (section,name,tracked) VALUES ('Anchors','Anki',1)"
    )
    offsets = _due_days_back(7, _MONDAY)
    logged_by_day = {
        (_MONDAY - timedelta(days=d)).isoformat(): ["anki"] for d in offsets
    }
    state = anchor_flow_state(
        logs, window=7, logged_by_day=logged_by_day, today=_MONDAY
    )
    assert state["mode"] == "flow"


def test_anchor_flow_state_lost_but_not_chronic(tmp_path):
    """Below-threshold completion is 'lost', but a recent hit means it isn't chronic yet."""
    logs = Logs(str(tmp_path))
    logs.db.execute(_HABITS_TABLE)
    logs.db.execute(
        "INSERT INTO habits (section,name,tracked) VALUES ('Anchors','Anki',1)"
    )
    offsets = _due_days_back(7, _MONDAY)
    # Done only today (offset 0) and the oldest day — 2/7, below the 0.5 threshold —
    # but today's hit means the last 3 due days aren't ALL misses.
    done = {offsets[0], offsets[-1]}
    logged_by_day = {
        (_MONDAY - timedelta(days=d)).isoformat(): ["anki"]
        for d in offsets
        if d in done
    }
    state = anchor_flow_state(
        logs, window=7, chronic_window=3, logged_by_day=logged_by_day, today=_MONDAY
    )
    assert state["mode"] == "lost"
    assert state["anchor"] == "Anki"
    assert state["chronic"] is False


def test_anchor_flow_state_chronic_when_dead_streak(tmp_path):
    """An anchor missed on every one of its last chronic_window due days is 'chronic'."""
    logs = Logs(str(tmp_path))
    logs.db.execute(_HABITS_TABLE)
    logs.db.execute(
        "INSERT INTO habits (section,name,tracked) VALUES ('Anchors','Anki',1)"
    )
    offsets = _due_days_back(7, _MONDAY)
    # Done only on the 3 oldest of the 7 due days — the most recent 3 are all misses.
    done = set(offsets[-3:])
    logged_by_day = {
        (_MONDAY - timedelta(days=d)).isoformat(): ["anki"]
        for d in offsets
        if d in done
    }
    state = anchor_flow_state(
        logs, window=7, chronic_window=3, logged_by_day=logged_by_day, today=_MONDAY
    )
    assert state["mode"] == "lost"
    assert state["anchor"] == "Anki"
    assert state["chronic"] is True


def test_anchor_flow_state_only_reads_anchors_section(tmp_path):
    """A struggling habit outside the Anchors section doesn't trigger the gate."""
    logs = Logs(str(tmp_path))
    logs.db.execute(_HABITS_TABLE)
    logs.db.execute(
        "INSERT INTO habits (section,name,tracked) VALUES ('Habits','Stretch',1)"
    )
    offsets = _due_days_back(7, _MONDAY)
    # Stretch is never done — would be "lost" if it were an anchor, but it isn't one.
    state = anchor_flow_state(logs, window=7, logged_by_day={}, today=_MONDAY)
    assert state["mode"] == "flow"


def test_two_due_day_misses_after_saturday_still_break_streak(tmp_path):
    """Two consecutive due-day misses (Sunday + Monday) break the streak even with a
    Saturday bonus: the 'never miss twice' rule applies to due days, not bonus days.
    """
    logs = Logs(str(tmp_path))
    # Log only through Saturday; Sunday and Monday are both unlogged.
    days = [_SATURDAY - timedelta(days=i) for i in range(6)]
    logged_by_day = {d.isoformat(): ["daf yomi"] for d in days}

    # Check on Monday: Sunday miss + Monday miss = two consecutive due-day misses → break.
    current, _ = compute_streak(
        logs, "Daf Yomi", due_weekdays=None, logged_by_day=logged_by_day, today=_MONDAY
    )
    assert current == 0, (
        "Two consecutive missed due days (Sun+Mon) must break the streak even with Sat bonus"
    )
