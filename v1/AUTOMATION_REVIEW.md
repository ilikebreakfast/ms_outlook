# Automation Review — Technical Analysis

Produced on branch `automation-review`.  
No code was modified during the audit pass.  
All implementation work is tracked in this branch.

---

## Executive Summary

The pipeline is architecturally sound and well-structured for a v1 prototype.
Security is deliberate and layered. However **every single stage of the pipeline
requires active human presence to function**. The pipeline cannot run unattended:
startup requires keyboard input, authentication requires a browser, onboarding new
customers requires JSON editing, reviewing failed documents requires directory
browsing, and the database layer does not exist.

Seven automation categories need addressing, ordered by dependency:

1. **Foundation** — implement the missing database (nothing else is reliable without it)
2. **Headless operation** — remove interactive prompts, add CLI args, add a scheduler
3. **Auth resilience** — proactive token refresh, avoid re-authentication failures
4. **Onboarding automation** — auto-promote high-confidence suggested templates; CLI approve flow
5. **Review queue** — make `needs_review` documents actionable with notifications
6. **Monitoring** — detect silent failures, confidence drift, missed runs
7. **Scalability** — async processing, parallelism, queue-backed design

---

## Current Architecture

```
[Human runs python main.py]
        ↓ types days
[Interactive auth (device code)]
        ↓ browser login
[Email fetch → Sender allowlist → Download → Security → OCR]
        ↓
[Customer classify → Template parse → JSON write]
        ↓ falls into directory, no notification
[SQLite record] ← STUB: does nothing
        ↓
[Email moved]
```

**Key runtime characteristics:**
- Single-threaded, synchronous, fully sequential
- Requires a human at the keyboard to start
- Requires a human at a browser for first-run authentication
- Outputs land on disk with no downstream notification
- Database layer is a no-op stub (`main.py:33-39`)
- All configuration is flat files edited by hand

---

## Manual Intervention Points

### MIP-01 — Interactive day-count prompt blocks all scheduling

| Attribute | Detail |
|---|---|
| **File** | `main.py:45-56` — `_ask_days()` |
| **Why it happens** | `input()` call on line 47 blocks until a human types a number |
| **Impact** | Cannot be called from cron, Task Scheduler, CI, Docker entrypoint, or any scheduler without a controlling TTY. Every run requires a human at the keyboard. |
| **Automation solution** | Replace `_ask_days()` with `argparse`; accept `--days N` CLI argument with default of `1`. Keep `input()` only when running interactively (TTY detection via `sys.stdin.isatty()`). |

---

### MIP-02 — MSAL device-code authentication requires browser interaction

| Attribute | Detail |
|---|---|
| **File** | `auth/graph_client.py:73-110` — `get_access_token()` |
| **Why it happens** | `app.acquire_token_by_device_flow(flow)` blocks until the user visits `microsoft.com/devicelogin` and enters a code. Happens on first run and whenever the token cache expires. |
| **Impact** | The pipeline cannot be scheduled or run headlessly without human browser interaction. Token expiry brings the pipeline down silently. |
| **Automation solution** | (a) Add token expiry monitoring — check the token cache expiry timestamp, log a warning or send an alert before it expires. (b) For work accounts: migrate to client credentials flow. (c) Add a pre-flight check that validates token freshness and sends an alert if re-auth is needed. |

---

### MIP-03 — Customer onboarding is entirely manual JSON editing

| Attribute | Detail |
|---|---|
| **File** | `config/address_book.json` (file), `main.py:59-71` — `_load_contacts()` |
| **Why it happens** | New senders must be added by a human editing `address_book.json`. `manage.py list-senders` generates pasteable JSON but still requires manual copy-paste. |
| **Impact** | New vendors are silently dropped until someone notices the log and manually adds them. Creates a backlog and missed documents. |
| **Automation solution** | (a) Implement `manage.py add-sender <email>` that atomically writes to `address_book.json`. (b) Auto-approve high-confidence suggested templates (≥ 0.85). (c) Add `manage.py approve-suggestion <sender_email>` for one-command approval. |

---

### MIP-04 — Suggested templates silently accumulate with no notification

| Attribute | Detail |
|---|---|
| **File** | `pipeline/template_suggester.py:147-183` — `suggest()`, `main.py:144-152` |
| **Why it happens** | `suggest()` writes draft YAML to `config/suggested_templates/` and logs a path. No notification is sent. |
| **Impact** | Operators never know a new sender has appeared unless they watch logs or scan the directory. Documents from new vendors are never parsed. |
| **Automation solution** | Emit a structured webhook notification after each new suggestion. Add `PENDING_REVIEW_WEBHOOK_URL` env var. Add `manage.py list-suggestions` command. |

---

### MIP-05 — Low-confidence documents flagged `needs_review` with no notification or queue

| Attribute | Detail |
|---|---|
| **File** | `pipeline/json_output.py:72-76` — `build_output()`, `main.py:175-177` |
| **Why it happens** | When `combined_confidence < LOW_CONFIDENCE_THRESHOLD` (0.6), `needs_review=True` is set and a warning is logged. The JSON file sits in `parsed/`. No further action is taken. |
| **Impact** | Low-confidence documents — which may have wrong invoice numbers, wrong amounts — accumulate unreviewed indefinitely. In a financial context this is a compliance risk. |
| **Automation solution** | Implement the SQLite `review_queue` table. When `needs_review=True`, insert into queue. Add `manage.py review-queue` command. Add webhook notification. |

---

### MIP-06 — SQLite database module missing; no deduplication, no audit trail

| Attribute | Detail |
|---|---|
| **File** | `main.py:33-39` — `_DBStub`, `database/db.py` — does not exist |
| **Why it happens** | Noted in `CLAUDE.md`. The module was planned but not implemented. |
| **Impact** | (a) `db.already_processed()` always returns `False` — emails reprocessed on every run if move disabled. (b) No audit trail. (c) `db.record()` does nothing. (d) All downstream automation is blocked on this. |
| **Automation solution** | Implement `database/db.py` with three tables: `processed_documents`, `review_queue`, `template_stats`. |

---

### MIP-07 — Manual pipeline scheduling (no cron / scheduler)

| Attribute | Detail |
|---|---|
| **File** | `main.py:195-246` — `main()`, `docker-compose.yml` |
| **Why it happens** | No scheduler is wired in. `docker-compose.yml` runs the pipeline once and exits. |
| **Impact** | The pipeline processes emails only when a human manually runs it. Documents pile up in the inbox. |
| **Automation solution** | Add `--schedule INTERVAL` CLI argument. Add Docker Compose `scheduler` service profile. Provide cron configuration examples. |

---

### MIP-08 — No token refresh before pipeline run

| Attribute | Detail |
|---|---|
| **File** | `auth/graph_client.py:113-117` — `GraphClient.__init__()` |
| **Why it happens** | If the refresh token is also expired (~90 days inactivity), it falls through to device flow — which blocks unattended runs. |
| **Impact** | After ~90 days of inactivity, an unattended scheduled run blocks indefinitely without sending any alert. |
| **Automation solution** | If device flow is triggered in a non-interactive context, send a webhook notification before blocking. Add `--check-auth` flag for pre-flight validation. |

---

### MIP-09 — Regex template patterns are hand-written with no automated improvement

| Attribute | Detail |
|---|---|
| **File** | `config/templates/*.yaml`, `pipeline/template_parser.py:52-81` — `parse()` |
| **Why it happens** | Patterns are static YAML written by a human. No mechanism to detect format changes. |
| **Impact** | When a vendor updates their invoice template, field extraction silently fails. Confidence drops unnoticed. |
| **Automation solution** | Record per-template confidence in DB. Add `manage.py analyze-template <name>` for trend analysis. Alert when rolling average drops >15% below historical baseline. |

---

### MIP-10 — No retry logic on Microsoft Graph API calls

| Attribute | Detail |
|---|---|
| **File** | `auth/graph_client.py:122-139` — `GraphClient.get()`, `GraphClient.post()` |
| **Why it happens** | `requests.get()` / `requests.post()` have a timeout but no retry decorator. |
| **Impact** | Any transient HTTP 429 (throttle), 503, or network hiccup fails the entire pipeline run. |
| **Automation solution** | Wrap `get()` and `post()` with exponential backoff retry: 1s→2s→4s on 429/503, max 3 retries. Respect `Retry-After` header. |

---

### MIP-11 — No monitoring, no health endpoint, no alerting

| Attribute | Detail |
|---|---|
| **Files** | Entire codebase — no metrics module, no health endpoint, no alerting hook |
| **Impact** | No way to know if the pipeline hasn't run in 48 hours; overall parse confidence is declining; disk is filling with unreviewed documents. |
| **Automation solution** | Add `pipeline/metrics.py` writing a JSON metrics file after each run. Add `WEBHOOK_URL` env var to POST run summary. Add Prometheus metrics file option. |

---

### MIP-12 — `_PERSONAL_DOMAINS` list is duplicated across modules

| Attribute | Detail |
|---|---|
| **Files** | `pipeline/template_suggester.py:43-47`, `manage.py:18-21` |
| **Impact** | Maintenance drift: a domain added to one file but not the other causes misclassification. |
| **Automation solution** | Move `_PERSONAL_DOMAINS` to `config/settings.py`. Import from there in both modules. |

---

### MIP-13 — `address_book.json` missing warning silently opens all senders

| Attribute | Detail |
|---|---|
| **File** | `main.py:61-66` — `_load_contacts()` |
| **Why it happens** | If `config/address_book.json` does not exist, code logs a warning and returns `[]`. Empty contacts list causes `is_allowed_sender()` to return `True` for all senders. |
| **Impact** | On a fresh installation or if the file is accidentally deleted, the security allowlist check is completely bypassed. This violates the stated design principle. |
| **Automation solution** | When `address_book.json` is missing, default to `DENY_ALL`. Log an error with instructions. Add `--allow-all-senders` flag for intentional permissive mode. |

---

## Exact Files to Change

| Priority | File | Change |
|---|---|---|
| P0 | `database/db.py` *(create)* | Implement SQLite module — dedup, audit, review queue, template stats |
| P0 | `main.py` | Replace `_ask_days()` with `argparse`; headless mode; fix MIP-13 security gap |
| P1 | `auth/graph_client.py` | Add tenacity retry; add pre-flight token check; add expiry notification |
| P1 | `pipeline/metrics.py` *(create)* | Run metrics emitter + optional webhook POST |
| P1 | `config/settings.py` | Centralise `PERSONAL_EMAIL_DOMAINS`; add `WEBHOOK_URL`, `ALERT_WEBHOOK_URL`, `AUTO_APPROVE_CONFIDENCE` |
| P2 | `pipeline/template_suggester.py` | Import `PERSONAL_EMAIL_DOMAINS` from settings; trigger webhook on new suggestion |
| P2 | `manage.py` | Add `approve-suggestion`, `review-queue`, `list-suggestions`, `add-sender`, `analyze-template` subcommands |
| P2 | `pipeline/json_output.py` | On `needs_review=True`, insert into review queue table |
| P3 | `docker-compose.yml` | Add `scheduler` service profile with configurable interval |
| P3 | `utils/logger.py` | Add structured JSON log output option for log aggregation |

---

## Implementation Plan (Phased)

### Phase 0 — Database Foundation

Implement `database/db.py` with three tables:

```
processed_documents
  id, message_id, attachment_filename, sender_email, customer_name,
  invoice_number, confidence, needs_review, status, json_path,
  attachment_path, received_at, processed_at, error, template_name

review_queue
  id, processed_doc_id, status (pending|resolved|dismissed),
  reason, created_at, resolved_at, resolved_by

template_stats
  id, template_name, run_at, confidence, required_fields_matched,
  required_fields_total
```

### Phase 1 — Headless Operation

- Replace `_ask_days()` with `argparse`: `python main.py --days 7 --schedule 1h`
- `--dry-run`: fetch and classify, write nothing
- `--check-auth`: validate token without processing
- Fix MIP-13: change missing `address_book.json` to `DENY_ALL`

### Phase 2 — Auth Resilience

- Add `tenacity` retry on `get()`/`post()`
- Respect `Retry-After` header on 429
- Add `check_token_health()` function
- Webhook alert if device flow triggered in non-interactive context

### Phase 3 — Customer Onboarding Automation

- Centralise `PERSONAL_EMAIL_DOMAINS` in settings
- Add `AUTO_APPROVE_CONFIDENCE` setting
- Auto-promote high-confidence suggestions to `address_book.json` + `config/templates/`
- Webhook notification on new suggestion
- `manage.py add-sender`, `approve-suggestion`, `list-suggestions`

### Phase 4 — Review Queue & Notifications

- DB `review_queue` table populated on `needs_review=True`
- `manage.py review-queue`, `resolve-review`, `analyze-template`
- Webhook POST on `needs_review` documents

### Phase 5 — Monitoring & Alerting

- `pipeline/metrics.py` writing `logs/metrics.json` after each run
- `WEBHOOK_URL` for run summaries
- `ALERT_WEBHOOK_URL` for error conditions
- Template drift detection in `manage.py analyze-template`
- `manage.py health` command

### Phase 6 — Scalability

- `--workers N` flag with `ThreadPoolExecutor` for parallel attachment processing
- Docker Compose `scheduler` service profile
- Atomic SQLite dedup before parallelising

---

## Risk Assessment

| Risk | Severity | Notes |
|---|---|---|
| MIP-13 security gap (missing address book → allow all) | **HIGH** | Fix in Phase 1 before any other change |
| SQLite stub (no dedup) | **HIGH** | Emails reprocessed on every run if move disabled |
| Auth blocking in scheduled mode | **HIGH** | Silent hang; operator never notified |
| Suggested templates never actioned | **MEDIUM** | Revenue/data loss from missed vendors |
| No retry on Graph API | **MEDIUM** | Transient failures abort entire runs |
| `_PERSONAL_DOMAINS` duplication | **LOW** | Maintenance bug, not a runtime failure |
| Template confidence drift undetected | **MEDIUM** | Silent degradation of data quality |
| Single-threaded processing | **LOW now, HIGH at scale** | Fine for <50 emails/day; blocks at higher volume |
| No monitoring | **MEDIUM** | Impossible to know when the pipeline is broken |
