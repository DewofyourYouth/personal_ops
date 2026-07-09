"""StalenessChecker — sends nudges when a tracked log tag hasn't been written.

Deterministic domain service; no Telegram concerns beyond `bot.send_message`.
`check_and_prompt` is the scheduled entry point; everything else is pure data.

Per-track thresholds (hours) are loaded from a JSON config file:
    {"checkin": 4, "food": 6}
Defaults are applied for any track not present in the file.

A track is "stale" when:
1. We are currently in a should-prompt window (waking hours, not a quiet day).
2. The last entry with that tag is older than the configured threshold.
3. We haven't already sent a staleness nudge for this track within the same window.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Jerusalem")

_DDL = """
CREATE TABLE IF NOT EXISTS staleness_prompts (
    track            TEXT PRIMARY KEY,
    last_prompted_at TEXT
);
"""

_DEFAULT_CONFIG: dict[str, int] = {"checkin": 4}

_NUDGES: dict[str, str] = {
    "checkin": "👋 How are you doing? (checkin:)",
    "food": "🍽 Have you eaten? (food:)",
}


class StalenessChecker:
    def __init__(self, db, quiet_window, config_path: "Path | str | None" = None) -> None:
        self._db = db
        self._qw = quiet_window
        self._config: dict[str, int] = dict(_DEFAULT_CONFIG)
        if config_path:
            p = Path(config_path)
            if p.exists():
                try:
                    self._config.update(json.loads(p.read_text()))
                except Exception:
                    pass
        db.ensure_schema(_DDL)

    def _last_entry_ts(self, track: str) -> "datetime | None":
        rows = self._db.query(
            "SELECT ts FROM entries WHERE tag = ? ORDER BY ts DESC LIMIT 1",
            (track,),
        )
        if not rows:
            return None
        try:
            return datetime.fromisoformat(rows[0]["ts"]).astimezone(_TZ)
        except Exception:
            return None

    def _last_prompted_ts(self, track: str) -> "datetime | None":
        rows = self._db.query(
            "SELECT last_prompted_at FROM staleness_prompts WHERE track = ?",
            (track,),
        )
        if not rows or not rows[0]["last_prompted_at"]:
            return None
        try:
            return datetime.fromisoformat(rows[0]["last_prompted_at"]).astimezone(_TZ)
        except Exception:
            return None

    def _record_prompted(self, track: str, now: datetime) -> None:
        self._db.execute(
            "INSERT INTO staleness_prompts(track, last_prompted_at) VALUES(?, ?) "
            "ON CONFLICT(track) DO UPDATE SET last_prompted_at = excluded.last_prompted_at",
            (track, now.isoformat()),
        )

    def stale_tracks(self) -> list[str]:
        """Tracks past their staleness threshold right now. Empty if not in a prompt window."""
        if not self._qw.should_prompt():
            return []
        now = datetime.now(_TZ)
        stale = []
        for track, threshold_h in self._config.items():
            cutoff = now - timedelta(hours=threshold_h)
            last_entry = self._last_entry_ts(track)
            if last_entry is not None and last_entry >= cutoff:
                continue  # logged recently enough
            last_prompted = self._last_prompted_ts(track)
            if last_prompted is not None and last_prompted >= cutoff:
                continue  # already nudged within this window
            stale.append(track)
        return stale

    async def check_and_prompt(self, bot, chat_id: int) -> None:
        """Send nudge messages for stale tracks and record the prompt time."""
        tracks = self.stale_tracks()
        if not tracks:
            return
        now = datetime.now(_TZ)
        for track in tracks:
            self._record_prompted(track, now)
            msg = _NUDGES.get(track, f"📝 No {track} log in a while — anything to note?")
            await bot.send_message(chat_id=chat_id, text=msg)
