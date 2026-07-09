"""hypotheses.py — persistence + follow-up for logged hypotheses.

A hypothesis is a small empirical test: a claim, what would confirm/falsify it,
the metric keys to watch, and a follow-up date. The deterministic core here owns
the storage and the follow-up report (pulling the metric readings logged since the
hypothesis was raised). The LLM only shapes the initial evaluation, at the edge.

Table lives beside the other core tables in ops.db but is created here through the
generic `ensure_schema` surface so this module owns its own DDL.
"""

from datetime import date

_CREATE = """
CREATE TABLE IF NOT EXISTS hypotheses (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created        TEXT NOT NULL,
    text           TEXT NOT NULL,
    restatement    TEXT NOT NULL DEFAULT '',
    confirm_if     TEXT NOT NULL DEFAULT '',
    falsify_if     TEXT NOT NULL DEFAULT '',
    metric_keys    TEXT NOT NULL DEFAULT '',
    follow_up_date TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_hypotheses_status ON hypotheses(status);
"""

# status lifecycle: active → prompted (follow-up sent) → confirmed | falsified | dropped
_OPEN = ("active", "prompted")


class Hypotheses:
    def __init__(self, db):
        self.db = db
        self.db.ensure_schema(_CREATE)

    def add(
        self,
        text: str,
        *,
        restatement: str = "",
        confirm_if: str = "",
        falsify_if: str = "",
        metric_keys: list[str] | None = None,
        follow_up_date: str = "",
        created: str | None = None,
    ) -> int:
        keys = ",".join(metric_keys or [])
        created = created or date.today().isoformat()
        cur = self.db._conn().execute(
            """INSERT INTO hypotheses
               (created, text, restatement, confirm_if, falsify_if, metric_keys, follow_up_date)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (created, text, restatement, confirm_if, falsify_if, keys, follow_up_date),
        )
        self.db._conn().commit()
        return cur.lastrowid

    def get(self, hyp_id: int):
        rows = self.db.query("SELECT * FROM hypotheses WHERE id = ?", (hyp_id,))
        return rows[0] if rows else None

    def open(self) -> list:
        """Active + prompted hypotheses, newest first — the /hypotheses list."""
        return self.db.query(
            "SELECT * FROM hypotheses WHERE status IN (?, ?) ORDER BY id DESC", _OPEN
        )

    def due(self, today: str | None = None) -> list:
        """Active hypotheses whose follow-up date has arrived and not yet prompted."""
        today = today or date.today().isoformat()
        return self.db.query(
            "SELECT * FROM hypotheses WHERE status = 'active' "
            "AND follow_up_date != '' AND follow_up_date <= ? ORDER BY id",
            (today,),
        )

    def set_status(self, hyp_id: int, status: str) -> None:
        self.db.execute(
            "UPDATE hypotheses SET status = ? WHERE id = ?", (status, hyp_id)
        )

    def _readings(self, key: str, since: str) -> list[float]:
        rows = self.db.query(
            "SELECT value FROM metrics WHERE key = ? AND date >= ? ORDER BY date, ts",
            (key, since),
        )
        vals = []
        for r in rows:
            try:
                vals.append(float(r["value"]))
            except (TypeError, ValueError):
                continue
        return vals

    def followup_report(self, hyp) -> str:
        """Build the follow-up check-in: the test, restated, plus the metric readings
        logged since it was raised. Deterministic — the payoff of persisting the test."""
        created = hyp["created"]
        lines = [f"🔬 <b>Hypothesis check-in</b> — raised {created}"]
        if hyp["restatement"]:
            lines.append(hyp["restatement"])
        if hyp["confirm_if"]:
            lines.append(f"✅ Confirm: {hyp['confirm_if']}")
        if hyp["falsify_if"]:
            lines.append(f"❌ Falsify: {hyp['falsify_if']}")

        keys = [k for k in hyp["metric_keys"].split(",") if k]
        if keys:
            lines.append("")
            lines.append("📊 <b>Data since:</b>")
            for key in keys:
                vals = self._readings(key, created)
                if not vals:
                    lines.append(f"• {key}: no readings")
                else:
                    avg = sum(vals) / len(vals)
                    lines.append(
                        f"• {key}: {len(vals)} readings, "
                        f"latest {_fmt(vals[-1])} (avg {_fmt(avg)})"
                    )
        return "\n".join(lines)


def _fmt(n: float) -> str:
    """Trim trailing .0 so integers read as integers."""
    return str(int(n)) if n == int(n) else f"{n:.1f}"
