"""Current location + timezone — the single source of truth for "where is
'here' right now," for the general *travel* override (as opposed to the
Shabbat-only sun-time override owned by `Shabbat` in shabbat.py, which
reuses `resolve()` here for its own geocoding rather than duplicating it).

Defaults to Beit Shemesh / Asia/Jerusalem. A travel override — set via a
"traveling to X" command or the LLM-driven natural-language fallback in
text_router.py, always confirmed by the user before it's applied (nothing
here applies a change on its own) — geocodes the place (geopy/Nominatim) and
resolves its timezone offline (tzfpy, no network call and no numba/llvmlite
dependency, which is broken in this environment). It persists until cleared
or a long safety-net expiry, and is read by every module that used to
hardcode ZoneInfo("Asia/Jerusalem"), so quiet hours, reminders, scheduling
windows, and log timestamps all agree on the same "here".

Resolving a place (`resolve`) and applying it (`apply`) are deliberately
separate: text_router.py resolves first to show the user what a command or
an LLM guess would actually do, and only calls apply() after they confirm —
so nothing changes the active location without the user seeing and approving
it first.
"""

import json
import os
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from astral import LocationInfo
from geopy.geocoders import Nominatim
from tzfpy import get_tz

DEFAULT_NAME = "Beit Shemesh"
DEFAULT_TZ = "Asia/Jerusalem"
DEFAULT_LOCATION = LocationInfo(DEFAULT_NAME, "Israel", DEFAULT_TZ, 31.7454, 34.9925)
TRAVEL_SAFETY_EXPIRY_DAYS = 21  # a forgotten override can't linger forever

_DEFAULT_DATA_DIR = os.path.join(os.path.dirname(__file__), "log")


def resolve(place_name: str) -> LocationInfo | None:
    """Geocode place_name (Nominatim) and resolve its timezone (tzfpy), with no
    side effects — a pure lookup used to preview a location change before it's
    applied. Returns None if the place couldn't be geocoded."""
    geolocator = Nominatim(user_agent="personal_ops_location")
    try:
        geocoded = geolocator.geocode(place_name)
    except Exception:
        return None
    if not geocoded:
        return None
    tz_name = get_tz(geocoded.longitude, geocoded.latitude) or DEFAULT_TZ
    return LocationInfo(place_name, "", tz_name, geocoded.latitude, geocoded.longitude)


class Location:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._listeners: list = []

    def _override_path(self) -> str:
        return os.path.join(self.data_dir, "travel-location-override.json")

    def add_listener(self, fn) -> None:
        """Register a callback invoked (with no args) after apply/clear_travel
        actually change the active location."""
        self._listeners.append(fn)

    def _notify(self) -> None:
        for fn in self._listeners:
            fn()

    def apply(self, info: LocationInfo) -> None:
        """Persist an already-resolved location as the active travel override,
        until clear_travel() or the safety-net expiry. Call resolve() first —
        this step is the one the user should have already confirmed."""
        data = {
            "name": info.name,
            "lat": info.latitude,
            "lon": info.longitude,
            "tz": info.timezone,
            "expires": (
                date.today() + timedelta(days=TRAVEL_SAFETY_EXPIRY_DAYS)
            ).isoformat(),
        }
        with open(self._override_path(), "w") as f:
            json.dump(data, f)
        self._notify()

    def set_travel(self, place_name: str) -> LocationInfo | None:
        """Convenience one-shot resolve()+apply() for callers that don't need a
        separate confirm step (tests, scripts). The chat-facing flow in
        text_router.py always calls resolve() and apply() separately instead,
        with a user confirmation in between."""
        info = resolve(place_name)
        if info:
            self.apply(info)
        return info

    def clear_travel(self) -> None:
        path = self._override_path()
        existed = os.path.exists(path)
        if existed:
            os.remove(path)
        if existed:
            self._notify()

    def current(self) -> LocationInfo:
        path = self._override_path()
        if os.path.exists(path):
            try:
                data = json.loads(open(path).read())
                if date.today() <= date.fromisoformat(data["expires"]):
                    return LocationInfo(
                        data["name"], "", data["tz"], data["lat"], data["lon"]
                    )
            except Exception:
                pass
        return DEFAULT_LOCATION

    def current_tz(self) -> ZoneInfo:
        return ZoneInfo(self.current().timezone)


# --- Process-wide singleton, so most callers can just `from location import
# current_tz` without threading a Location instance through their constructor
# (mirrors the bare `TZ = ZoneInfo(...)` module constant this replaces). ---

_instance = Location(os.environ.get("OPS_DATA_DIR", _DEFAULT_DATA_DIR))


def init(data_dir: str) -> Location:
    """Repoint the singleton at a specific data_dir (composition root, tests)."""
    global _instance
    _instance = Location(data_dir)
    return _instance


def current() -> LocationInfo:
    return _instance.current()


def current_tz() -> ZoneInfo:
    return _instance.current_tz()


def apply(info: LocationInfo) -> None:
    _instance.apply(info)


def set_travel(place_name: str) -> LocationInfo | None:
    return _instance.set_travel(place_name)


def clear_travel() -> None:
    _instance.clear_travel()


def add_listener(fn) -> None:
    _instance.add_listener(fn)
