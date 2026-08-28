"""Small Habitify API v2 client used by migration and Telegram integration."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any


def normalize_habit_name(value: str) -> str:
    """Normalize local/remote names without losing meaningful words."""
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


# These four habits existed in Habitify before the migration and intentionally kept
# their Habitify names. Every newly-created habit kept its Personal Ops name.
REMOTE_NAME_OVERRIDES = {
    normalize_habit_name(
        "Daily walk (7000 steps minimum, includes walk to shul)"
    ): "Step Count",
    normalize_habit_name("Daily walk"): "Step Count",
    normalize_habit_name("Strength training — 3x/week minimum"): "Core Training",
    normalize_habit_name("Strength training"): "Core Training",
    normalize_habit_name("brush teeth"): "Brush teeth",
    normalize_habit_name("tefillin"): "Tefillin",
}

# Context.habit_display_name deliberately shortens these labels for Telegram. Map
# them back to the full Habitify names created by the migration.
DISPLAY_NAME_OVERRIDES = {
    normalize_habit_name(
        "Take morning meds"
    ): "Take morning meds (before anything else)",
    normalize_habit_name("Shacharit"): "Shacharit (07:00–08:00)",
    normalize_habit_name("Anki"): "Anki (minimum daily streak)",
    normalize_habit_name("Yoma chavrusa"): "10:00–11:00 Yoma chavrusa",
    normalize_habit_name(
        "Weigh in"
    ): "Weigh in — at least 3x/week, morning, log in Apple Health",
    normalize_habit_name(
        "Writing output"
    ): "Writing output — at least 3 times per week (genealogy, dailyderja, or technical)",
}


class HabitifyError(RuntimeError):
    """A Habitify request failed."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Habitify API returned {status}: {message}")
        self.status = status


class HabitifyClient:
    BASE_URL = "https://api.habitify.me/v2"

    def __init__(self, api_key: str, timeout: float = 30.0) -> None:
        if not api_key.strip():
            raise ValueError("Habitify API key is empty")
        self.api_key = api_key.strip()
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        retry_transient: bool | None = None,
    ) -> Any:
        url = f"{self.BASE_URL}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                # Cloudflare rejects Python's default urllib user-agent before the
                # request reaches Habitify, despite accepting the same API key via
                # curl. Identify the integration explicitly.
                "User-Agent": "personal-ops-habitify/1.0",
                "X-API-Key": self.api_key,
            },
        )
        # Retrying an ambiguous POST can duplicate a create or log if Habitify
        # committed the first request but lost its response. Reads and PUT/DELETE
        # are safe to retry; individual POST wrappers opt in only when their
        # operation has idempotent conflict handling.
        if retry_transient is None:
            retry_transient = method in {"GET", "PUT", "DELETE"}
        attempts = 5 if retry_transient else 1
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                message = exc.read().decode(errors="replace")
                if exc.code in {429, 500, 502, 503, 504} and attempt < attempts - 1:
                    time.sleep(2**attempt)
                    continue
                raise HabitifyError(exc.code, message) from exc
            except urllib.error.URLError as exc:
                if attempt < attempts - 1:
                    time.sleep(2**attempt)
                    continue
                raise HabitifyError(0, str(exc.reason)) from exc
        raise AssertionError("request retry loop exited unexpectedly")

    def list_habits(self) -> list[dict[str, Any]]:
        """Return active and archived habits, following pagination."""
        found: dict[str, dict[str, Any]] = {}
        for archived in (False, True):
            offset = 0
            while True:
                result = self._request(
                    "GET",
                    "/habits",
                    query={
                        "archived": str(archived).lower(),
                        "limit": 50,
                        "offset": offset,
                    },
                )
                rows = (result or {}).get("data", [])
                for row in rows:
                    found[row["id"]] = row
                pagination = (result or {}).get("pagination", {})
                offset += len(rows)
                if not rows or offset >= pagination.get("total", offset):
                    break
        return list(found.values())

    def create_habit(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._request("POST", "/habits", payload=payload)
        if isinstance(result, dict) and isinstance(result.get("data"), dict):
            return result["data"]
        if not isinstance(result, dict):
            raise HabitifyError(201, "create response did not contain a habit")
        return result

    def update_habit(self, habit_id: str, payload: dict[str, Any]) -> None:
        self._request(
            "PUT",
            f"/habits/{urllib.parse.quote(habit_id)}",
            payload=payload,
        )

    def statistics(self, habit_id: str, start: str, end: str) -> dict[str, Any]:
        result = self._request(
            "GET",
            f"/habits/{urllib.parse.quote(habit_id)}/statistics",
            query={"startDate": start, "endDate": end},
        )
        return (result or {}).get("data", result or {})

    def journal(self, target_date: str) -> list[dict[str, Any]]:
        result = self._request("GET", "/habits/journal", query={"date": target_date})
        return (result or {}).get("data", [])

    def complete(self, habit_id: str, target_date: str) -> None:
        self._request(
            "POST",
            f"/habits/{urllib.parse.quote(habit_id)}/logs/complete",
            payload={"targetDate": target_date},
        )

    def undo(self, habit_id: str, target_date: str) -> None:
        self._request(
            "POST",
            f"/habits/{urllib.parse.quote(habit_id)}/logs/undo",
            payload={"targetDate": target_date},
        )

    def archive(self, habit_id: str) -> None:
        try:
            self._request(
                "POST",
                f"/habits/{urllib.parse.quote(habit_id)}/archive",
                retry_transient=True,
            )
        except HabitifyError as exc:
            # Archive is idempotent for our purposes. This also covers the case
            # where the first request succeeded but its response was lost and a
            # retry receives "already archived".
            if exc.status != 409:
                raise


class HabitifyHabitSync:
    """Resolve Personal Ops habit names and record idempotent completions."""

    def __init__(self, client: HabitifyClient, cache_seconds: float = 300.0) -> None:
        self.client = client
        self.cache_seconds = cache_seconds
        self._habit_ids: dict[str, str] = {}
        self._habits: list[dict[str, Any]] = []
        self._all_habits: list[dict[str, Any]] = []
        self._loaded_at = 0.0

    def _refresh(self) -> None:
        habits = self.client.list_habits()
        self._all_habits = [
            habit for habit in habits if habit.get("type", "good") == "good"
        ]
        self._habits = [
            habit
            for habit in self._all_habits
            if not habit.get("isArchived", False)
            and habit.get("type", "good") == "good"
        ]
        self._habit_ids = {
            normalize_habit_name(habit["name"]): habit["id"] for habit in self._habits
        }
        self._loaded_at = time.monotonic()

    def resolve_id(self, local_name: str) -> str:
        if (
            not self._habit_ids
            or time.monotonic() - self._loaded_at >= self.cache_seconds
        ):
            self._refresh()

        local_key = normalize_habit_name(local_name)
        remote_name = REMOTE_NAME_OVERRIDES.get(
            local_key, DISPLAY_NAME_OVERRIDES.get(local_key, local_name)
        )
        remote_key = normalize_habit_name(remote_name)
        habit_id = self._habit_ids.get(remote_key)
        if habit_id is None:
            # Telegram callback_data has a byte limit and may contain only the first
            # 48/52 characters. Accept a prefix only when it identifies one habit.
            prefix_matches = [
                value
                for key, value in self._habit_ids.items()
                if key.startswith(remote_key) or remote_key.startswith(key)
            ]
            if len(prefix_matches) == 1:
                habit_id = prefix_matches[0]
        if habit_id is None:
            # A habit may have been added in Habitify since the cache was filled.
            self._refresh()
            habit_id = self._habit_ids.get(remote_key)
        if habit_id is None:
            raise HabitifyError(404, f"no active habit matches {local_name!r}")
        return habit_id

    def active_habits(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        if (
            force_refresh
            or not self._habits
            or time.monotonic() - self._loaded_at >= self.cache_seconds
        ):
            self._refresh()
        return list(self._habits)

    def definition_habits(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        """All good-habit definitions, including archived rows for reconciliation."""
        if (
            force_refresh
            or not self._all_habits
            or time.monotonic() - self._loaded_at >= self.cache_seconds
        ):
            self._refresh()
        return list(self._all_habits)

    def completions_for_date(self, target_date: str) -> dict[str, Any]:
        """Return completed habits plus IDs whose daily state was resolved.

        A failed weekly-statistics read is deliberately left unresolved so a transient
        Habitify error cannot erase the last known projected completion.
        """
        if not self._habits:
            self._refresh()
        active = {str(habit["id"]): habit for habit in self._habits}
        completed: list[dict[str, str]] = []
        resolved_ids: set[str] = set()
        weekly: list[tuple[str, dict[str, Any]]] = []
        for row in self.client.journal(target_date):
            habit_id = str(row.get("id", ""))
            habit = active.get(habit_id)
            if habit is None:
                continue
            progress = row.get("progress") or {}
            periodicity = progress.get("periodicity", "daily")
            done_today = row.get("status") == "completed"
            if periodicity != "daily":
                if float(progress.get("current", 0) or 0) > 0:
                    weekly.append((habit_id, habit))
                else:
                    resolved_ids.add(habit_id)
                continue
            resolved_ids.add(habit_id)
            if done_today:
                completed.append({"id": habit_id, "name": habit["name"].strip()})

        def weekly_done(
            item: tuple[str, dict[str, Any]],
        ) -> tuple[str, dict[str, str] | None] | None:
            habit_id, habit = item
            try:
                stats = self.client.statistics(habit_id, target_date, target_date)
            except HabitifyError:
                return None
            today = next(
                (
                    day
                    for day in stats.get("dailyProgress", [])
                    if day.get("date") == target_date
                ),
                {},
            )
            if float(today.get("totalLog", 0) or 0) > 0:
                return habit_id, {"id": habit_id, "name": habit["name"].strip()}
            return habit_id, None

        if weekly:
            with ThreadPoolExecutor(max_workers=min(2, len(weekly))) as pool:
                for result in pool.map(weekly_done, weekly):
                    if result is None:
                        continue
                    habit_id, completion = result
                    resolved_ids.add(habit_id)
                    if completion:
                        completed.append(completion)
        return {"completed": completed, "resolved_ids": sorted(resolved_ids)}

    def complete(self, local_name: str, target_date: str) -> None:
        habit_id = self.resolve_id(local_name)
        try:
            self.client.complete(habit_id, target_date)
        except HabitifyError as exc:
            # Repeated Telegram taps and an already-completed Apple Health habit are
            # successful from the user's perspective.
            if exc.status != 409:
                raise
