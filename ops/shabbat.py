"""Shabbat quiet-mode + candle lighting — a small domain service.

Deterministic, no Telegram concerns. Owns the per-day candle-lighting file and
the rules for when the bot should stay quiet (Friday evening through Saturday
nightfall). Scheduled jobs and the text router consult `quiet_now()`; the
candle-lighting prompt flow uses save/load/confirmation.
"""

import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Jerusalem")

# Daily active window — the bot only sends event nudges between these times.
QUIET_END = time(8, 0)  # 08:00 — nothing before this
EVENT_QUIET_END = time(22, 0)  # 22:00 — nothing after this
SHABBAT_END_HOUR = 21  # assumed nightfall; replace with Zmanim API eventually


class Shabbat:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir

    def _candles_path(self) -> str:
        return os.path.join(self.data_dir, f"{date.today()}-candles.txt")

    def save_candle_lighting(self, t: str) -> None:
        with open(self._candles_path(), "w") as f:
            f.write(t)

    def load_candle_lighting(self) -> time | None:
        path = self._candles_path()
        if not os.path.exists(path):
            return None
        try:
            raw = open(path).read().strip()
            h, m = map(int, raw.split(":"))
            return time(h, m)
        except Exception:
            return None

    def candle_confirmation(self, t: str) -> str:
        """Confirmation message after setting candle lighting (incl. when quiet mode kicks in)."""
        h, m = int(t[:2]), int(t[3:])
        quiet_m = m - 20 if m >= 20 else m + 40
        quiet_h = h if m >= 20 else h - 1
        quiet = f"{quiet_h:02d}:{quiet_m:02d}"
        now_t = datetime.now(_TZ)
        already = now_t.hour * 60 + now_t.minute >= quiet_h * 60 + quiet_m
        return f"🕯️ Candle lighting set for {t}. Shabbat Shalom — {'already in quiet mode.' if already else f'going quiet at {quiet}.'}"

    def quiet_now(self) -> bool:
        now = datetime.now(_TZ)
        weekday = now.weekday()
        if weekday == 5:  # Saturday — quiet until assumed nightfall
            return now.hour < SHABBAT_END_HOUR
        if weekday == 4:  # Friday — quiet from 20 min before candle lighting
            candles = self.load_candle_lighting()
            if candles:
                quiet_dt = datetime.combine(
                    now.date(), candles, tzinfo=_TZ
                ) - timedelta(minutes=20)
                if now >= quiet_dt:
                    return True
        return False

    def in_active_window(self) -> bool:
        now_t = datetime.now(_TZ).time().replace(second=0, microsecond=0)
        return QUIET_END <= now_t <= EVENT_QUIET_END
