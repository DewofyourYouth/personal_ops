"""Pytest configuration — applied before any test module is collected.

The `telegram` package requires the `cryptography` C-extension (via cffi) which
may not be available in all environments (e.g. CI without the native binary).
We pre-populate sys.modules with minimal stubs so tests that import telegram-
dependent modules (habit_handlers, tg_common, etc.) can load without the real
package being installed.
"""

import sys
import types


def _stub_telegram() -> None:
    """Insert minimal fakes for the telegram package and its submodules."""

    def _cls(name: str):
        def __init__(self, *args, **kwargs):
            self.args = args
            for key, value in kwargs.items():
                setattr(self, key, value)
            if name == "InlineKeyboardButton" and args:
                self.text = args[0]
            if name == "InlineKeyboardMarkup" and args:
                self.inline_keyboard = tuple(tuple(row) for row in args[0])

        t = type(name, (), {"__init__": __init__})
        t.DEFAULT_TYPE = None
        return t

    t = types.ModuleType("telegram")
    for name in [
        "Bot",
        "Update",
        "InlineKeyboardButton",
        "InlineKeyboardMarkup",
        "Message",
    ]:
        setattr(t, name, _cls(name))

    e = types.ModuleType("telegram.ext")
    for name in [
        "Application",
        "CallbackQueryHandler",
        "CommandHandler",
        "ContextTypes",
        "MessageHandler",
    ]:
        setattr(e, name, _cls(name))

    f = types.ModuleType("telegram.ext.filters")
    for name in ["TEXT", "VOICE", "PHOTO", "AUDIO", "Document", "ALL"]:
        setattr(f, name, None)

    err = types.ModuleType("telegram.error")
    setattr(err, "BadRequest", Exception)
    setattr(err, "NetworkError", Exception)
    setattr(err, "TimedOut", Exception)

    # Register all submodules that might be imported by name.
    stubs = [
        "telegram",
        "telegram.ext",
        "telegram.ext.filters",
        "telegram.error",
        "telegram.constants",
        "telegram._payment",
        "telegram._payment.stars",
        "telegram._payment.stars.startransactions",
    ]
    for mod_name in stubs:
        sys.modules.setdefault(mod_name, types.ModuleType(mod_name))

    # Overwrite with the richer stubs.
    sys.modules["telegram"] = t
    sys.modules["telegram.ext"] = e
    sys.modules["telegram.ext.filters"] = f
    sys.modules["telegram.error"] = err


_stub_telegram()
