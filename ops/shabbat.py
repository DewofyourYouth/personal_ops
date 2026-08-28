"""Shabbat quiet-mode + candle lighting — a small domain service.

Deterministic (geocoding aside), no Telegram concerns. Candle lighting and
nightfall are computed automatically from sunset (via astral) at the active
location — Beit Shemesh by default, a geocoded place when visiting somewhere
for Shabbat ("candle lighting in Tzfat"), or wherever `location.py`'s general
travel override says you actually are right now, which takes priority (if
you're literally in Florida, that's more true than a stale in-Israel Shabbat
override). The Shabbat-only override auto-expires after that Shabbat's
Saturday so a forgotten override doesn't silently apply to a later week, and
never changes the clock — Israel is a single timezone, only sun-time math
shifts; the timezone (quiet-hours math, "now") only follows the general
travel override via `location.current_tz()`. A raw time (e.g. "candle
lighting 19:13") still overrides the computed candle-lighting time directly,
for one-off early lighting. Scheduled jobs and the text router consult
`quiet_now()`; the candle-lighting flow uses save/load/confirmation.

Geocoding for the location override is shared with `location.py`
(`location.resolve()`) rather than duplicated here — `apply_location_override`
only persists an already-resolved place, so text_router.py can resolve a
place, show the user what it resolved to, and only apply it once they
confirm.
"""

import json
import os
from datetime import date, datetime, time, timedelta

from astral import LocationInfo
from astral.sun import sun

import location

# Daily active window — the bot only sends event nudges between these times.
QUIET_END = time(8, 0)  # 08:00 — nothing before this
EVENT_QUIET_END = time(22, 0)  # 22:00 — nothing after this

_BEIT_SHEMESH = LocationInfo(
    "Beit Shemesh", "Israel", "Asia/Jerusalem", 31.7454, 34.9925
)
CANDLE_LIGHTING_OFFSET_MIN = 40  # minutes before sunset
NIGHTFALL_OFFSET_MIN = 72  # minutes after sunset — Shabbat ends


def _upcoming_saturday(d: date) -> date:
    """The Saturday of the Shabbat that d falls in or leads into."""
    return d + timedelta(days=(5 - d.weekday()) % 7)


class Shabbat:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir

    def _candles_path(self) -> str:
        return os.path.join(self.data_dir, f"{date.today()}-candles.txt")

    def save_candle_lighting(self, t: str) -> None:
        with open(self._candles_path(), "w") as f:
            f.write(t)

    def _manual_candle_lighting(self) -> time | None:
        path = self._candles_path()
        if not os.path.exists(path):
            return None
        try:
            raw = open(path).read().strip()
            h, m = map(int, raw.split(":"))
            return time(h, m)
        except Exception:
            return None

    # --- Location override (visiting somewhere for Shabbat) ---

    def _location_override_path(self) -> str:
        return os.path.join(self.data_dir, "shabbat-location-override.json")

    def apply_location_override(self, info: LocationInfo) -> None:
        """Persist an already-resolved (location.resolve()) location in place of
        Beit Shemesh for this Shabbat only (expires after the coming Saturday).
        Call resolve() first — this step is the one the user should have
        already confirmed."""
        data = {
            "name": info.name,
            "lat": info.latitude,
            "lon": info.longitude,
            "expires": _upcoming_saturday(date.today()).isoformat(),
        }
        with open(self._location_override_path(), "w") as f:
            json.dump(data, f)

    def save_location_override(self, place_name: str) -> LocationInfo | None:
        """Convenience one-shot resolve()+apply for callers that don't need a
        separate confirm step (tests, scripts). The chat-facing flow in
        text_router.py always resolves and applies separately instead, with a
        user confirmation in between. Returns the resolved LocationInfo, or
        None if the place couldn't be geocoded."""
        info = location.resolve(place_name)
        if info:
            self.apply_location_override(info)
        return info

    def clear_location_override(self) -> None:
        path = self._location_override_path()
        if os.path.exists(path):
            os.remove(path)

    def _active_location(self) -> LocationInfo:
        # General travel override takes priority — it reflects where you
        # actually are, which trumps a possibly-stale Shabbat-only override.
        travel = location.current()
        if travel.name != location.DEFAULT_NAME:
            return travel
        path = self._location_override_path()
        if os.path.exists(path):
            try:
                data = json.loads(open(path).read())
                if date.today() <= date.fromisoformat(data["expires"]):
                    return LocationInfo(
                        data["name"], "", "Asia/Jerusalem", data["lat"], data["lon"]
                    )
            except Exception:
                pass
        return _BEIT_SHEMESH

    # --- Computed candle lighting / nightfall ---

    def computed_candle_lighting(self, d: date | None = None) -> time:
        """Sunset at the active location minus CANDLE_LIGHTING_OFFSET_MIN, for
        the given date (default: today)."""
        s = sun(
            self._active_location().observer,
            date=d or date.today(),
            tzinfo=location.current_tz(),
        )
        candle_dt = s["sunset"] - timedelta(minutes=CANDLE_LIGHTING_OFFSET_MIN)
        return candle_dt.time().replace(second=0, microsecond=0)

    def load_candle_lighting(self) -> time | None:
        """Manual override for today if one was set, otherwise the computed time."""
        return self._manual_candle_lighting() or self.computed_candle_lighting()

    def computed_nightfall(self, d: date | None = None) -> datetime:
        """Sunset at the active location plus NIGHTFALL_OFFSET_MIN, for the given
        date (default: today) — when Shabbat ends."""
        s = sun(
            self._active_location().observer,
            date=d or date.today(),
            tzinfo=location.current_tz(),
        )
        return s["sunset"] + timedelta(minutes=NIGHTFALL_OFFSET_MIN)

    def candle_confirmation(self, t: str) -> str:
        """Message reporting the candle-lighting time (incl. when quiet mode kicks in)."""
        h, m = int(t[:2]), int(t[3:])
        quiet_m = m - 20 if m >= 20 else m + 40
        quiet_h = h if m >= 20 else h - 1
        quiet = f"{quiet_h:02d}:{quiet_m:02d}"
        now_t = datetime.now(location.current_tz())
        already = now_t.hour * 60 + now_t.minute >= quiet_h * 60 + quiet_m
        loc = self._active_location()
        where = f" ({loc.name})" if loc.name != _BEIT_SHEMESH.name else ""
        return f"🕯️ Candle lighting{where}: {t}. Shabbat Shalom — {'already in quiet mode.' if already else f'going quiet at {quiet}.'}"

    def quiet_now(self) -> bool:
        now = datetime.now(location.current_tz())
        weekday = now.weekday()
        if weekday == 5:  # Saturday — quiet until nightfall (sunset + offset)
            return now < self.computed_nightfall(now.date())
        if weekday == 4:  # Friday — quiet from 20 min before candle lighting
            candles = self.load_candle_lighting()
            if candles:
                quiet_dt = datetime.combine(
                    now.date(), candles, tzinfo=location.current_tz()
                ) - timedelta(minutes=20)
                if now >= quiet_dt:
                    return True
        return False

    def in_active_window(self) -> bool:
        now_t = (
            datetime.now(location.current_tz()).time().replace(second=0, microsecond=0)
        )
        return QUIET_END <= now_t <= EVENT_QUIET_END
