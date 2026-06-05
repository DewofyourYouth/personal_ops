"""
insights.py — A durable ledger of the qualitative reflections buried in the logs.

WHY THIS EXISTS
---------------
The baseline tracker keeps the *numbers* (completion %, mood, energy) and deliberately
bakes in NO notes, so history stays re-interpretable. But the richest material the user
logs is qualitative: hypotheses ("Friday anxiety comes from Shabbat-prep stress"), recurring
concerns ("I keep missing Shacharit"), and ideas ("the system should learn from how I
rearrange my agenda"). Those land in free-form `log:` entries and scroll out of the digest
window within a week — they have nowhere durable to live.

This ledger is that home. It is the qualitative complement to baseline.json: numbers there,
words here.

HOW IT WORKS (AI at the edges, deterministic core)
--------------------------------------------------
This module is pure storage — it owns the data and never calls a model. The extraction
(reading the logs and deciding "that sentence is a hypothesis") happens at the edge, in
`Planner.extract_insights`, at digest-generation time. That call returns proposed new items
and recurrences of existing ones; `merge()` here persists them deterministically.

WHY THE LLM ONLY PROPOSES, NEVER REWRITES
-----------------------------------------
An item's text is the USER's own observation, stored verbatim and dated. The extractor may
ADD a new item or LINK a recurrence to an existing id — it may never edit or delete past
text. This keeps the ledger an honest, re-readable record of what the user actually said,
not a mutable AI summary that could quietly drift (the same anti-"load-bearing-conclusion"
discipline baseline_tracker follows).

The recurrence counter is the payoff: each weekly run that sees a hypothesis again appends
that week's date, so the digest can say "you've raised this three weeks running."
"""

import json
from datetime import date
from pathlib import Path

# The buckets the extractor sorts free-form reflections into. Deliberately NOT including
# "futures" — those are opt-in accountability the user sets explicitly (see
# AGENDA_PARADIGM_SPEC.md); auto-creating them would impose accountability the user
# didn't choose.
KINDS = ("insight", "hypothesis", "idea", "concern")
_PLURAL = {
    "insight": "Insights",
    "hypothesis": "Hypotheses",
    "idea": "Ideas",
    "concern": "Concerns",
}


class Insights:
    def __init__(self, log_dir: str):
        self.path = Path(log_dir) / "insights.json"

    def load(self) -> dict:
        if not self.path.exists():
            return {"next_id": 1, "items": []}
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {"next_id": 1, "items": []}

    def save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def open_items(self) -> list[dict]:
        """The current ledger items, slimmed to what the extractor needs to spot recurrences."""
        return [
            {"id": it["id"], "kind": it["kind"], "text": it["text"]}
            for it in self.load()["items"]
        ]

    def merge(
        self,
        new_items: list[dict],
        recurrences: list[dict],
        on_date: date | None = None,
    ) -> dict:
        """Persist extractor output. Returns a summary of what changed.

        `new_items`  — [{"kind", "text"}] proposed additions; each gets a fresh id.
        `recurrences`— [{"id"}] existing items the extractor saw evidence of again.
        Idempotent within a day: re-running on the same date won't double-count occurrences.
        """
        on = (on_date or date.today()).isoformat()
        data = self.load()
        by_id = {it["id"]: it for it in data["items"]}

        added, recurred = [], []

        for item in new_items:
            kind = item.get("kind", "insight")
            text = (item.get("text") or "").strip()
            if not text or kind not in KINDS:
                continue
            new = {
                "id": data["next_id"],
                "kind": kind,
                "text": text,
                "first_seen": on,
                "last_seen": on,
                "occurrences": [on],
            }
            data["items"].append(new)
            by_id[new["id"]] = new
            data["next_id"] += 1
            added.append(new)

        for rec in recurrences:
            item = by_id.get(rec.get("id"))
            if item is None:  # ignore ids the extractor invented
                continue
            if item["occurrences"] and item["occurrences"][-1] == on:
                continue  # already logged today — stay idempotent
            item["occurrences"].append(on)
            item["last_seen"] = on
            recurred.append(item)

        self.save(data)
        return {"added": added, "recurred": recurred, "total": len(data["items"])}

    def format_for_prompt(self) -> str:
        """Render the ledger for the weekly-digest prompt. Empty string if nothing yet."""
        items = self.load()["items"]
        if not items:
            return ""

        lines = ["## Insight ledger (the user's own recurring reflections)\n"]
        lines.append(
            "These are observations, hypotheses, ideas and concerns the user has voiced "
            "in their logs, distilled and dated. 'raised N×' counts how many distinct review "
            "periods it has resurfaced in — a high or rising count is a real signal worth "
            "naming. These are the USER's words, not your conclusions; do not relabel or "
            "moralize them.\n"
        )
        for kind in KINDS:
            group = [it for it in items if it["kind"] == kind]
            if not group:
                continue
            group.sort(
                key=lambda it: (len(it["occurrences"]), it["last_seen"]), reverse=True
            )
            lines.append(f"### {_PLURAL[kind]}")
            for it in group:
                n = len(it["occurrences"])
                span = (
                    it["first_seen"]
                    if it["first_seen"] == it["last_seen"]
                    else f"{it['first_seen']}→{it['last_seen']}"
                )
                times = f"raised {n}×" if n > 1 else "new"
                lines.append(f"- ({times}, {span}) {it['text']}")
            lines.append("")

        return "\n".join(lines).strip()

    def format_for_telegram(self) -> str:
        """Human-readable ledger for the /insights command."""
        items = self.load()["items"]
        if not items:
            return "No insights captured yet. Run the weekly digest or /insights to distill your logs."

        icons = {"insight": "💡", "hypothesis": "🔬", "idea": "🛠", "concern": "⚠️"}
        out = []
        for kind in KINDS:
            group = [it for it in items if it["kind"] == kind]
            if not group:
                continue
            group.sort(
                key=lambda it: (len(it["occurrences"]), it["last_seen"]), reverse=True
            )
            out.append(f"\n{icons[kind]} <b>{_PLURAL[kind]}</b>")
            for it in group:
                n = len(it["occurrences"])
                tag = f" ×{n}" if n > 1 else ""
                out.append(f"• {it['text']}{tag}")
        return "\n".join(out).strip()
