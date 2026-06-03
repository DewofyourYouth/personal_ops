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
Two write resources to start:

1. **`POST /metrics`** — weight, steps (and any keyed metric).
2. **`POST /jobs`** — job applications + status changes ("career ops from the phone").

`POST /jobs` also serves as the **defined job_tracker ↔ personal_ops interface** the VPS
migration requires — replacing the temporary CSV coupling with an explicit contract.

```
iPhone Shortcut ──HTTPS POST──▶ FastAPI app ──▶ logs.write_metric / jobs.add_application ──▶ ops.db (+ JSONL)
                                    │
              (bot keeps running in its own container, same ops.db)
```

## Architecture

- New service `api/` (FastAPI + uvicorn), its own container in `docker-compose.yml`,
  mounting the **same** `./ops/log` volume so it shares `ops.db` with the bot.
- Routes call existing functions — `logs.write_metric(...)` and `jobs.add_application(...)`
  — never raw SQL. So they inherit current behavior (metrics get JSONL-first durability;
  jobs get the company+title upsert) for free.
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

## `POST /jobs`

Add a job application, or advance an existing one's status — one endpoint, because
`jobs.add_application()` **upserts on `company + title`**: same pair updates
`url/applied_date/status/notes`; a new pair inserts.

```jsonc
{
  "company": "Acme",            // required
  "title": "Backend Engineer",  // required (also part of the upsert key)
  "status": "phone_screen",     // enum below; default "applied"
  "url": "https://…",           // optional
  "source": "LinkedIn",         // optional
  "notes": "recruiter reached out",  // optional
  "applied_date": "2026-06-02"  // optional ISO date; default today
}
```

`status` ∈ `applied | phone_screen | interview | offer | rejected | withdrew | unknown`
(unrecognized values map to `unknown`, matching `_parse_status`).

| Status | Body | Meaning |
|--------|------|---------|
| 200 | `{"job":{"id":42,"company":"Acme","title":"Backend Engineer","status":"phone_screen",…},"created":false}` | upserted |
| 401 | `{"detail":"unauthorized"}` | bad/missing token |
| 422 | (FastAPI validation) | missing company/title, bad status |
| 500 | `{"detail":"db write failed"}` | write failed |

Implementation: `jobs.add_application(company, title, url, source, notes, status, applied_date)`.
`created` = whether a new row was inserted vs. an existing company+title updated.

Typical phone flows:
- **New application** — POST company+title+source (status defaults to `applied`).
- **Status change** — POST the same company+title with the new `status` (e.g. `interview`),
  optionally a note. No id needed; the pair is the key.

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

1. `api/main.py` — FastAPI app, `POST /metrics` + `POST /jobs`, Bearer-token dependency,
   reusing `Logs` and `jobs.add_application`. Add `fastapi` + `uvicorn` to `requirements.txt`.
2. Compose `api` service (off unless `INGEST_TOKEN` set), localhost port.
3. Verify locally with `curl` → 200 + rows in `ops.db` (one metric, one job).
4. Point Shortcuts at it (one for steps/weight, one for job status); confirm end-to-end.
5. On VPS: reverse proxy + TLS. Then add dashboard **read** routes to the same app.

## Dashboard read routes (later)

`GET` endpoints feeding the public, anonymized dashboard: habit streak graphs, productivity
patterns, weekly digest summaries, job-search stats. No company names or personal details.

## Open questions

1. Local transport to the Mac before VPS — LAN IP, Tailscale, or wait for the VPS?
2. Metrics: auto-push from Apple Health on a schedule, or fire the Shortcut manually?
3. Jobs: is company+title a strong enough key, or could two roles at the same company
   collide (then add `PATCH /jobs/{id}` for status, keyed by id)?
