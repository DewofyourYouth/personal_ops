"""QuietWindow — generalized quiet-mode calendar.

Single source of truth for "should the bot prompt right now?" Covers Shabbat
(Fri sunset → Sat nightfall) via the existing Shabbat service, plus arbitrary
chag windows loaded from a JSON calendar file.

During a quiet window: no prompts, no auto-miss accumulation. Days fully inside
a quiet window are excluded from habit-coverage stats, not counted as failures.
"""

import json
from datetime import datetime, time, timedelta
from pathlib import Path

import location

# Bot sends proactive prompts only between these clock times.
_WAKING_START = time(8, 0)
_WAKING_END = time(22, 0)


class QuietWindow:
    """Determines whether the bot should be quiet at a given moment.

    Parameters
    ----------
    shabbat:      Existing Shabbat instance (owns the candle-lighting file).
    chagim_path:  Path to a JSON file listing additional quiet windows (optional).
                  Format: list of {"name": str, "quiet_start": ISO datetime,
                                   "quiet_end": ISO datetime}
    """

    def __init__(self, shabbat, chagim_path: "Path | str | None" = None) -> None:
        self._shabbat = shabbat
        self._chag_windows: list[tuple[str, datetime, datetime]] = []
        if chagim_path:
            self._load_chagim(Path(chagim_path))

    def _load_chagim(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            tz = location.current_tz()
            for entry in json.loads(path.read_text()):
                start = datetime.fromisoformat(entry["quiet_start"]).astimezone(tz)
                end = datetime.fromisoformat(entry["quiet_end"]).astimezone(tz)
                self._chag_windows.append((entry.get("name", "Chag"), start, end))
        except Exception:
            pass  # malformed file → fall back to Shabbat-only mode

    def _active_chag(self, dt: datetime) -> str | None:
        for name, start, end in self._chag_windows:
            if start <= dt < end:
                return name
        return None

    def is_quiet_at(self, dt: "datetime | None" = None) -> bool:
        """True if dt (default: now) is inside a quiet window (Shabbat or chag)."""
        return self.active_window_name(dt) is not None

    def active_window_name(self, dt: "datetime | None" = None) -> str | None:
        """Name of the quiet window covering dt — a chag's name from chagim.json,
        or 'Shabbat' — or None if dt isn't inside any quiet window. The single
        source of truth for both "is it quiet" and "what should the bot call
        it," so a chag window is never mislabeled as Shabbat in user-facing
        text."""
        if dt is None:
            dt = datetime.now(location.current_tz())
        else:
            dt = dt.astimezone(location.current_tz())
        chag = self._active_chag(dt)
        if chag is not None:
            return chag
        if self._is_shabbat_quiet(dt):
            return "Shabbat"
        return None

    def _is_shabbat_quiet(self, dt: datetime) -> bool:
        weekday = dt.weekday()
        if weekday == 5:  # Saturday — quiet until nightfall (sunset + offset)
            return dt < self._shabbat.computed_nightfall(dt.date())
        if weekday == 4:  # Friday — quiet from 20 min before candle lighting
            candles = self._shabbat.load_candle_lighting()
            if candles:
                quiet_dt = datetime.combine(
                    dt.date(), candles, tzinfo=location.current_tz()
                ) - timedelta(minutes=20)
                return dt >= quiet_dt
        return False

    def in_waking_hours(self, dt: "datetime | None" = None) -> bool:
        """True if dt falls inside the 08:00–22:00 active window."""
        if dt is None:
            dt = datetime.now(location.current_tz())
        t = dt.astimezone(location.current_tz()).time().replace(second=0, microsecond=0)
        return _WAKING_START <= t <= _WAKING_END

    def should_prompt(self, dt: "datetime | None" = None) -> bool:
        """True if the bot may send proactive prompts at dt (waking hours, not quiet)."""
        if dt is None:
            dt = datetime.now(location.current_tz())
        return self.in_waking_hours(dt) and not self.is_quiet_at(dt)

    # --- Backward-compat shims (drop once all callers migrate) ---

    def quiet_now(self) -> bool:
        return self.is_quiet_at()

    def in_active_window(self) -> bool:
        return self.in_waking_hours()
