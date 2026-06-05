"""Per-instance configuration — the one place an instance's identity, storage
location, and tunables are resolved.

Today this is loaded once at startup from the environment (`Config.from_env()`)
and threaded into the composition root in `bot.py`. It exists so that an
instance has *no* implicit globals: storage paths and identity come from this
object rather than from `os.getcwd()` or scattered `os.environ` reads. That is
the seam that makes "run another instance" a matter of a second config, not a
code change.

API keys (ANTHROPIC_API_KEY / OPENAI_API_KEY) are intentionally NOT held here —
the Anthropic/OpenAI SDKs read them straight from the environment, so a
bring-your-own-key instance already works without us touching them.
"""

import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from zoneinfo import ZoneInfo

# Storage lives under ops/log/ relative to this file — robust to the working
# directory the process was launched from (the old getcwd() form silently
# pointed at a different DB if you ran the bot from elsewhere).
_DEFAULT_DATA_DIR = Path(__file__).parent / "log"
_DEFAULT_CONTEXT_DIR = Path(__file__).parent / "context"


@dataclass(frozen=True)
class Config:
    """Everything one bot instance needs that varies between instances."""

    bot_token: str
    allowed_user: int
    data_dir: Path
    context_dir: Path = _DEFAULT_CONTEXT_DIR
    # Reflective outputs run on Sonnet; cheap structured parsing is pinned to
    # Haiku in the methods that do it. See bot.py for the rationale.
    model: str = "claude-sonnet-4-6"
    timezone: str = "Asia/Jerusalem"
    plan_hour: int = 8
    plan_minute: int = 0

    @classmethod
    def from_env(cls) -> "Config":
        """Build a Config from environment variables. `OPS_BOT_TOKEN` and
        `OPS_CHAT_ID` are required; everything else has a sensible default."""
        return cls(
            bot_token=os.environ["OPS_BOT_TOKEN"],
            allowed_user=int(os.environ["OPS_CHAT_ID"]),
            data_dir=Path(os.environ.get("OPS_DATA_DIR", _DEFAULT_DATA_DIR)),
            context_dir=Path(os.environ.get("OPS_CONTEXT_DIR", _DEFAULT_CONTEXT_DIR)),
            model=os.environ.get("OPS_MODEL", "claude-sonnet-4-6"),
            timezone=os.environ.get("OPS_TIMEZONE", "Asia/Jerusalem"),
            plan_hour=int(os.environ.get("OPS_PLAN_HOUR", "8")),
            plan_minute=int(os.environ.get("OPS_PLAN_MINUTE", "0")),
        )

    @cached_property
    def tz(self) -> ZoneInfo:
        """The instance's timezone as a ZoneInfo, for datetime construction."""
        return ZoneInfo(self.timezone)
