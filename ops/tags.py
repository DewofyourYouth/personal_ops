"""The canonical tag taxonomy — single source of truth for every tag the app knows.

Everything tag-shaped derives from TAGS below: the prefix→tag map the router matches,
the enum + descriptions the classifiers see, the reclassify picker, and the set of
text-bearing tags the log miner reads. Before this module those four lived in four
files (bot_constants, llm, reclassify_handlers, mine_logs) and could drift.

A tag's `definition` doubles as its classifier-prompt line, so tightening a
definition here immediately tightens both the LLM and embedding classifiers.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Tag:
    name: str
    definition: str
    prefixes: tuple[str, ...] = ()
    inferable: bool = False  # the classifier may emit it for un-prefixed messages
    in_picker: bool = False  # offered in the reclassify picker
    mine_text: bool = False  # its content is mined as personal free text


TAGS = [
    Tag(
        "insight",
        "a realization or lesson: something newly understood about yourself or how things work",
        prefixes=("insight:",),
        inferable=True,
        in_picker=True,
        mine_text=True,
    ),
    Tag(
        "hypothesis",
        "a testable empirical claim (I think X causes Y)",
        prefixes=("hypothesis:",),
        inferable=True,
        in_picker=True,
    ),
    Tag(
        "note",
        "reference information to look up later (facts, names, numbers, links) — not about your own state or behavior",
        prefixes=("note:",),
        inferable=True,
        in_picker=True,
        mine_text=True,
    ),
    Tag(
        "task",
        "a near-term actionable to-do",
        prefixes=("task:",),
        inferable=True,
        in_picker=True,
    ),
    Tag(
        "friction",
        "something that went badly, blocked you, or created drag",
        # "wrong:" kept as an alias — the tag's old name, still in muscle memory.
        prefixes=("friction:", "wrong:"),
        inferable=True,
        in_picker=True,
        mine_text=True,
    ),
    Tag(
        "win",
        "an accomplishment or positive outcome",
        prefixes=("did:",),
        inferable=True,
        in_picker=True,
        mine_text=True,
    ),
    Tag(
        "backlog",
        "a someday/maybe idea, not near-term",
        prefixes=("backlog:", "someday:"),
        inferable=True,
        in_picker=True,
    ),
    Tag(
        "checkin",
        "subjective state: mood, energy, body",
        prefixes=("checkin",),
        inferable=True,
        in_picker=True,
        mine_text=True,
    ),
    Tag(
        "log",
        "fallback: a plain record of what happened when nothing above fits",
        inferable=True,
        in_picker=True,
        mine_text=True,
    ),
    # --- Prefix- or rules-routed only (never inferred by the base classifier) ---
    Tag("habit", "a completed habit", prefixes=("habit:",), in_picker=True),
    Tag(
        "food",
        "a meal or food consumed",
        prefixes=("food:", "ate:", "ate "),
        in_picker=True,
    ),
    Tag(
        "injection", "a medication injection", prefixes=("injection:", "shot:", "jab:")
    ),
    Tag(
        "skip",
        "an excused skip of today's habits",
        prefixes=("skip:", "excuse:", "excused:"),
    ),
    Tag(
        "directive",
        "a standing instruction to the app",
        prefixes=("directive:", "policy:"),
    ),
    Tag("discrete", "a private entry", prefixes=("discrete:", "private:")),
]

# prefix → "#tag", matched by the router's rules-first pass.
PREFIXES = {p: f"#{t.name}" for t in TAGS for p in t.prefixes}

# (tag, definition) pairs the base classifiers may emit — the LLM enum and the
# embedding classifier's inference targets both derive from this.
BASE_CLASSIFICATION_TAGS = [(t.name, t.definition) for t in TAGS if t.inferable]

# Categories offered in the reclassify picker: the classifier enum plus the
# rules-routed tags a message realistically gets misfiled into or out of.
PICKER_TAGS = [t.name for t in TAGS if t.in_picker]

# Tags whose content is the user's own free text — what the log miner reads.
TEXT_MINING_TAGS = tuple(t.name for t in TAGS if t.mine_text)
