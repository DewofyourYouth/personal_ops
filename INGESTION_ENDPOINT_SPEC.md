# Metrics Ingestion Endpoint — Spec

Status: **proposed** (not built). Authored 2026-06-03.
Part of the [VPS migration](VPS_MIGRATION.md) → "Public dashboard" FastAPI app.

## Problem

Metrics sent from an iOS Shortcut hitting `api.telegram.org/bot<TOKEN>/sendMessage`
are posted **by the bot itself**. Telegram's `getUpdates` never returns a bot's own
messages, so `handle_message` never fires — the reading is lost with no exception, no
JSONL, no DB row, no ack. (Confirmed 2026-06-02: `steps 11855` and `weight 94.3` both
vanished.) No amount of hardening *inside* the bot helps, because no update ever arrives.

The fix: stop routing phone input through the Bot API. The Shortcut POSTs the reading
straight to our own HTTP API, which writes it directly to `ops.db`.

## Decision

This is **not** a side-channel bolted onto the bot. It is the **first route of the
FastAPI app** already planned for the VPS ("Future: Public dashboard",
`dashboard.dewofyouryouth.com`). The dashboard needs that app to exist anyway; metrics
ingestion is the first concrete reason to stand it up. Read/dashboard routes come later.

```
iPhone Shortcut ──HTTPS POST──▶ FastAPI app ──logs.write_metric()──▶ ops.db (+ JSONL)
                                    │
              (bot keeps running in its own container, same ops.db)
```

## Why this is safe now

Two processes (Telegram bot + FastAPI app) will write the same `ops.db`. The WAL +
`busy_timeout=30000` change shipped 2026-06-03 makes concurrent writers wait for the
lock instead of failing — so a second writer process is fine. Both reuse the same
`Logs`/`Database` classes, so ingestion inherits JSONL-first durability and the
startup self-heal automatically.

## The app

- New service `api/` (FastAPI + uvicorn), its own container in `docker-compose.yml`,
  mounting the **same** `./ops/log` volume so it shares `ops.db` with the bot.
- Imports `Logs(LOG_DIR)` and calls `logs.write_metric(key, value, unit)` — identical
  path to chat input. No direct SQL in the route.

## Endpoint

```
POST /metrics
Authorization: Bearer <INGEST_TOKEN>
Content-Type: application/json
```

Body (Pydantic model, intentionally tiny — built for steps & weight, generic by key):

```jsonc
{ "key": "steps",  "value": 11855, "unit": "" }
{ "key": "weight", "value": 94.3,  "unit": "kg" }
```

Responses:

| Status | Body | Meaning |
|--------|------|---------|
| 200 | `{"logged":{"key":"steps","value":11855.0,"unit":""}}` | written to JSONL + DB |
| 401 | `{"detail":"unauthorized"}` | missing/wrong token |
| 422 | (FastAPI validation) | bad/missing fields |
| 500 | `{"detail":"db write failed","kept_in_jsonl":true}` | JSONL has it; sync recovers |

A non-200 is something the Shortcut shows as a notification — no silent loss on this path.

## Security

- **Auth**: shared secret `INGEST_TOKEN` in `.env`, constant-time compare. App refuses to
  start if it is unset.
- **VPS**: app binds localhost; reverse proxy (Caddy/nginx) terminates **TLS** at
  `dashboard.dewofyouryouth.com` (or an `api.` subdomain). Token never travels in plaintext.
- **Local (pre-VPS)**: reachable over LAN/Tailscale for testing the Shortcut before cutover.

## Docker / config

- New compose service `api`: same image base, command `uvicorn api.main:app`,
  `ports: ["127.0.0.1:8081:8081"]`, shares `./ops/log` volume, `restart: unless-stopped`.
- New env vars: `INGEST_TOKEN` (required), `INGEST_PORT` (default 8081).

## Shortcut change

Replace the `sendMessage` action with "Get Contents of `https://<host>/metrics`":
POST, header `Authorization: Bearer <token>`, body `{"key":"steps","value":<Health>}`.
Show the response so failures surface on the phone. (Apple Health → Shortcuts can read the
day's step count / latest weight directly and POST them on a schedule.)

## Build order

1. `api/main.py` — FastAPI app, `POST /metrics`, token dep, reuses `Logs`. Add
   `fastapi`+`uvicorn` to `requirements.txt`.
2. Compose `api` service (off unless `INGEST_TOKEN` set), localhost port.
3. Verify locally: `curl -H "Authorization: Bearer …" -d '{"key":"steps","value":11855}' …`
   → 200 + row in `ops.db`.
4. Point one Shortcut at it; confirm end-to-end.
5. On VPS: reverse proxy + TLS; later, add dashboard read routes to the same app.

## Open questions

1. Local transport to the Mac before VPS — LAN IP, Tailscale, or just wait for the VPS?
2. Auto-push from Apple Health on a schedule, or fire the Shortcut manually?
