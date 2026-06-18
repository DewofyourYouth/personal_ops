"""Minimal client for Telegram's sendRichMessage (Bot API 10.1, 2026-06-11).

This is the ONE place the bot bypasses python-telegram-bot: PTB 22.7 has no
binding for `sendRichMessage` yet, so we POST to the HTTP endpoint directly. The
send interface is just a string — `InputRichMessage` takes exactly one of `html`
or `markdown`, and Telegram parses it into the rich block tree (a `<ul>` of
`<input type="checkbox">` items becomes a checkbox list). Kept tiny and isolated
so it's trivial to delete once PTB ships a native method.

Callers should treat a raised exception as "fall back to a normal message" — the
endpoint is days old, so /status must never depend on it succeeding.
"""

import httpx

_API = "https://api.telegram.org/bot{token}/sendRichMessage"


async def send_rich_message(token: str, chat_id: int, html: str) -> dict:
    """Send a rich message whose body is the given rich-formatting HTML string.

    Raises on a network error or a non-OK Telegram response so the caller can fall
    back to a plain message. Returns the parsed `result` on success.
    """
    payload = {"chat_id": chat_id, "rich_message": {"html": html}}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(_API.format(token=token), json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"sendRichMessage not ok: {data}")
        return data["result"]
