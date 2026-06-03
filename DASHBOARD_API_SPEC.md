# Dashboard API — Spec

Status: **proposed** (not built). Authored 2026-06-03.
The FastAPI app planned for the VPS ("Public dashboard", `dashboard.dewofyouryouth.com`).
This doc covers its **write endpoints**; dashboard read routes are sketched at the end.

## Why this exists

Input sent from an iPhone Shortcut to `api.telegram.org/bot<TOKEN>/sendMessage` is posted
**by the bot itself**, and Telegram's `getUpdates` never returns a bot's own messages — so
those readings never reach the bot and vanish silently (confirmed 2026-06-02: `steps 11855`
and `weight 94.3` lost). No hardening inside the bot helps, because no update arrives.

Fix: the phone POSTs straight to **our own API**, which writes directly to `ops.db`.
One write resource to start:

1. **`POST /metrics`** — weight, steps (and any keyed metric).

> **Jobs dropped (decided 2026-06-03).** An earlier draft added `POST /jobs` as the
> job_tracker interface. Job tracking is being **retired from personal_ops** — handed off
> to a dedicated job-application agent (HyperAgent/Hermit). No jobs endpoint, and the agenda
> advisory/digests will not see the job funnel. See [VPS_MIGRATION.md](VPS_MIGRATION.md).

```
iPhone Shortcut ──HTTPS POST──▶ FastAPI app ──▶ logs.write_metric ──▶ ops.db (+ JSONL)
                                    │
              (bot keeps running in its own container, same ops.db)
```

## Architecture

- New service `api/` (FastAPI + uvicorn), its own container in `docker-compose.yml`,
  mounting the **same** `./ops/log` volume so it shares `ops.db` with the bot.
- Routes call existing functions — e.g. `logs.write_metric(...)` — never raw SQL, so they
  inherit current behavior (JSONL-first durability) for free.
- Two processes writing one `ops.db` is safe via the WAL + `busy_timeout=30000` change
  shipped 2026-06-03.

## Auth (all write routes)

Shared secret `INGEST_TOKEN` in `.env`, `Authorization: Bearer <token>`, constant-time
compare. App refuses to start if it is unset. A non-2xx is something the Shortcut shows as
a notification — so this path can't fail silently either.

---

## `POST /metrics`

```jsonc
{ "key": "steps",  "value": 11855, "unit": "" }
{ "key": "weight", "value": 94.3,  "unit": "kg" }
```

| Status | Body | Meaning |
|--------|------|---------|
| 200 | `{"logged":{"key":"steps","value":11855.0,"unit":""}}` | written to JSONL + DB |
| 401 | `{"detail":"unauthorized"}` | bad/missing token |
| 422 | (FastAPI validation) | bad/missing fields |
| 500 | `{"detail":"db write failed","kept_in_jsonl":true}` | JSONL has it; sync recovers |

Implementation: `logs.write_metric(key, value, unit)`.

---

## Security / deployment

- **VPS**: app binds localhost; reverse proxy (Caddy/nginx) terminates **TLS** at the
  dashboard domain. Token never travels in plaintext.
- **Local (pre-VPS)**: reachable over LAN/Tailscale to test Shortcuts before cutover.

## Docker / config

- New compose service `api`: same image base, `uvicorn api.main:app`,
  `ports: ["127.0.0.1:8081:8081"]`, shares `./ops/log` volume, `restart: unless-stopped`.
- New env vars: `INGEST_TOKEN` (required to enable; off when unset), `INGEST_PORT` (default 8081).

## Datastore

Stays **SQLite** (decided 2026-06-03). Revisit Postgres only on a real trigger: API on a
separate host from the DB, multi-user/productized, or as an ops-learning piece. `db.py` is
the only SQL layer, so a later swap is contained. See [VPS_MIGRATION.md](VPS_MIGRATION.md).

## Build order

1. `api/main.py` — FastAPI app, `POST /metrics`, Bearer-token dependency, reusing `Logs`.
   Add `fastapi` + `uvicorn` to `requirements.txt`.
2. Compose `api` service (off unless `INGEST_TOKEN` set), localhost port.
3. Verify locally with `curl` → 200 + a metric row in `ops.db`.
4. Point a Shortcut at it (steps/weight); confirm end-to-end.
5. On VPS: reverse proxy + TLS. Then add dashboard **read** routes to the same app.

## Dashboard read routes (later)

`GET` endpoints feeding the public, anonymized dashboard: habit streak graphs, productivity
patterns, weekly digest summaries. No personal details. (No job-search stats — jobs retired.)

## Open questions

1. Local transport to the Mac before VPS — LAN IP, Tailscale, or wait for the VPS?
2. Metrics: auto-push from Apple Health on a schedule, or fire the Shortcut manually?
