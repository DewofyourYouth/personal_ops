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
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Jerusalem")

# Bot sends proactive prompts only between these clock times.
_WAKING_START = time(8, 0)
_WAKING_END = time(22, 0)

# Shabbat nightfall approximated at 21:00 (update via Zmanim API eventually).
_SHABBAT_NIGHTFALL_HOUR = 21


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
        self._chag_windows: list[tuple[datetime, datetime]] = []
        if chagim_path:
            self._load_chagim(Path(chagim_path))

    def _load_chagim(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            for entry in json.loads(path.read_text()):
                start = datetime.fromisoformat(entry["quiet_start"]).astimezone(_TZ)
                end = datetime.fromisoformat(entry["quiet_end"]).astimezone(_TZ)
                self._chag_windows.append((start, end))
        except Exception:
            pass  # malformed file → fall back to Shabbat-only mode

    def _in_chag(self, dt: datetime) -> bool:
        return any(start <= dt < end for start, end in self._chag_windows)

    def is_quiet_at(self, dt: "datetime | None" = None) -> bool:
        """True if dt (default: now) is inside a quiet window (Shabbat or chag)."""
        if dt is None:
            dt = datetime.now(_TZ)
        else:
            dt = dt.astimezone(_TZ)
        return self._in_chag(dt) or self._is_shabbat_quiet(dt)

    def _is_shabbat_quiet(self, dt: datetime) -> bool:
        weekday = dt.weekday()
        if weekday == 5:  # Saturday — quiet until nightfall
            return dt.hour < _SHABBAT_NIGHTFALL_HOUR
        if weekday == 4:  # Friday — quiet from 20 min before candle lighting
            candles = self._shabbat.load_candle_lighting()
            if candles:
                quiet_dt = datetime.combine(dt.date(), candles, tzinfo=_TZ) - timedelta(
                    minutes=20
                )
                return dt >= quiet_dt
        return False

    def in_waking_hours(self, dt: "datetime | None" = None) -> bool:
        """True if dt falls inside the 08:00–22:00 active window."""
        if dt is None:
            dt = datetime.now(_TZ)
        t = dt.astimezone(_TZ).time().replace(second=0, microsecond=0)
        return _WAKING_START <= t <= _WAKING_END

    def should_prompt(self, dt: "datetime | None" = None) -> bool:
        """True if the bot may send proactive prompts at dt (waking hours, not quiet)."""
        if dt is None:
            dt = datetime.now(_TZ)
        return self.in_waking_hours(dt) and not self.is_quiet_at(dt)

    # --- Backward-compat shims (drop once all callers migrate) ---

    def quiet_now(self) -> bool:
        return self.is_quiet_at()

    def in_active_window(self) -> bool:
        return self.in_waking_hours()
