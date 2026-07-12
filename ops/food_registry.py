"""food_registry.py — personal defaults for foods logged constantly, plus
auto-promotion of repeated corrections into new defaults.

Deterministic core, no Telegram/LLM concerns: `text_router.py` looks up a food
description here before ever calling the estimator, and records a correction
here whenever the user adjusts an LLM estimate. Tables live beside the other
core tables in ops.db but are created here through the generic `ensure_schema`
surface so this module owns its own DDL (same pattern as hypotheses.py).
"""

import difflib
import json
import re
from datetime import date, datetime

_CREATE = """
CREATE TABLE IF NOT EXISTS food_registry (
    alias        TEXT PRIMARY KEY,          -- normalized lowercase
    synonyms     TEXT NOT NULL DEFAULT '[]', -- JSON array of normalized synonym strings
    kcal         REAL NOT NULL,
    protein_g    REAL NOT NULL,
    fat_g        REAL NOT NULL,
    carbs_g      REAL NOT NULL,
    serving_note TEXT NOT NULL DEFAULT '',
    created_ts   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_corrections (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    alias     TEXT NOT NULL,
    kcal      REAL NOT NULL,
    protein_g REAL NOT NULL,
    fat_g     REAL NOT NULL,
    carbs_g   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_food_corrections_alias ON food_corrections(alias);
CREATE TABLE IF NOT EXISTS food_registry_prompts (
    alias   TEXT PRIMARY KEY,   -- cooldown suppression after a decline
    last_ts TEXT NOT NULL
);
"""

# A correction alias is suppressed from re-prompting for this many days after a decline.
_SUPPRESS_DAYS = 30

# Two corrections for the same alias count as "materially the same" if every macro is
# within this relative tolerance (a 1 kcal/g floor avoids over-strict comparisons near
# zero, e.g. 0g vs 0.2g fat shouldn't hinge on a tiny absolute difference).
_MATCH_REL_TOL = 0.10

_MULT_RE = re.compile(r"^(.*?)\s*[x×]\s*(\d+(?:\.\d+)?)$", re.IGNORECASE)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower()).strip(" .,!?;:")


def _materially_same(a, b, rel_tol: float = _MATCH_REL_TOL) -> bool:
    for key in ("kcal", "protein_g", "fat_g", "carbs_g"):
        va, vb = float(a[key]), float(b[key])
        if va == 0 and vb == 0:
            continue
        denom = max(abs(va), abs(vb), 1.0)
        if abs(va - vb) / denom > rel_tol:
            return False
    return True


def parse_composition(text: str) -> list[tuple[str, float]]:
    """Split a food description into (item_text, multiplier) parts.

    'protein shake x2' -> [("protein shake", 2.0)]
    'protein shake + banana' -> [("protein shake", 1.0), ("banana", 1.0)]
    """
    parts = [p.strip() for p in re.split(r"\s*\+\s*", text) if p.strip()]
    result = []
    for p in parts:
        m = _MULT_RE.match(p)
        if m:
            result.append((m.group(1).strip(), float(m.group(2))))
        else:
            result.append((p, 1.0))
    return result


class FoodRegistry:
    def __init__(self, db) -> None:
        self.db = db
        self.db.ensure_schema(_CREATE)

    # --- Lookup ---

    def lookup(self, text: str) -> dict | None:
        """Exact/synonym match first (returns "exact": True — safe to skip
        confirmation), else a substring/difflib fuzzy match ("exact": False —
        callers should treat this as a partial signal, not an instant log).
        """
        norm = _normalize(text)
        if not norm:
            return None
        rows = self.db.query("SELECT * FROM food_registry")
        if not rows:
            return None

        for r in rows:
            synonyms = json.loads(r["synonyms"] or "[]")
            if norm == r["alias"] or norm in synonyms:
                return self._row_to_dict(r, exact=True)

        for r in rows:
            synonyms = json.loads(r["synonyms"] or "[]")
            candidates = [r["alias"], *synonyms]
            if any(c and (c in norm or norm in c) for c in candidates):
                return self._row_to_dict(r, exact=False)

        by_alias = {r["alias"]: r for r in rows}
        close = difflib.get_close_matches(norm, by_alias.keys(), n=1, cutoff=0.75)
        if close:
            return self._row_to_dict(by_alias[close[0]], exact=False)
        return None

    @staticmethod
    def _row_to_dict(r, exact: bool) -> dict:
        return {
            "alias": r["alias"],
            "kcal": r["kcal"],
            "protein_g": r["protein_g"],
            "fat_g": r["fat_g"],
            "carbs_g": r["carbs_g"],
            "serving_note": r["serving_note"],
            "exact": exact,
        }

    # --- Defaults ---

    def set_default(
        self,
        alias: str,
        kcal: float,
        protein_g: float,
        fat_g: float,
        carbs_g: float,
        serving_note: str = "",
        synonyms: list[str] | None = None,
    ) -> None:
        """Upsert a default. Used by auto-promotion confirm and the explicit
        #default override. Clears any cooldown suppression for the alias —
        an explicit save means the user wants this remembered now."""
        norm = _normalize(alias)
        self.db.execute(
            """INSERT INTO food_registry
                   (alias, synonyms, kcal, protein_g, fat_g, carbs_g, serving_note, created_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(alias) DO UPDATE SET
                   synonyms = excluded.synonyms,
                   kcal = excluded.kcal,
                   protein_g = excluded.protein_g,
                   fat_g = excluded.fat_g,
                   carbs_g = excluded.carbs_g,
                   serving_note = excluded.serving_note""",
            (
                norm,
                json.dumps(synonyms or []),
                kcal,
                protein_g,
                fat_g,
                carbs_g,
                serving_note,
                datetime.now().isoformat(),
            ),
        )
        self.db.execute("DELETE FROM food_registry_prompts WHERE alias = ?", (norm,))

    # --- Auto-promotion ---

    def record_correction(
        self, alias: str, kcal: float, protein_g: float, fat_g: float, carbs_g: float
    ) -> dict | None:
        """Record a correction sample. If the immediately prior correction for
        this alias is materially the same, no registry entry exists yet, and
        the alias isn't cooldown-suppressed, return a proposed-default dict
        (the caller shows a "Save as default?" prompt). Else None."""
        norm = _normalize(alias)
        self.db.execute(
            "INSERT INTO food_corrections (ts, alias, kcal, protein_g, fat_g, carbs_g) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), norm, kcal, protein_g, fat_g, carbs_g),
        )
        if self.db.query("SELECT 1 FROM food_registry WHERE alias = ?", (norm,)):
            return None
        if self.is_suppressed(norm):
            return None
        prior = self.db.query(
            "SELECT * FROM food_corrections WHERE alias = ? ORDER BY id DESC LIMIT 2",
            (norm,),
        )
        if len(prior) < 2:
            return None
        latest, previous = prior[0], prior[1]
        if not _materially_same(latest, previous):
            return None
        return {
            "alias": norm,
            "kcal": latest["kcal"],
            "protein_g": latest["protein_g"],
            "fat_g": latest["fat_g"],
            "carbs_g": latest["carbs_g"],
        }

    def suppress_prompt(self, alias: str) -> None:
        norm = _normalize(alias)
        self.db.execute(
            "INSERT INTO food_registry_prompts (alias, last_ts) VALUES (?, ?) "
            "ON CONFLICT(alias) DO UPDATE SET last_ts = excluded.last_ts",
            (norm, date.today().isoformat()),
        )

    def is_suppressed(self, alias: str) -> bool:
        norm = _normalize(alias)
        rows = self.db.query(
            "SELECT last_ts FROM food_registry_prompts WHERE alias = ?", (norm,)
        )
        if not rows:
            return False
        last = date.fromisoformat(rows[0]["last_ts"])
        return (date.today() - last).days < _SUPPRESS_DAYS
