# MFC Agent — Working Memory

Living notes for Guy + Claude. Update this file as we work so future sessions
have context without re-reading the whole codebase.

---

## Project at a glance

**What it is:** Voice AI agent for Montana Feed Company (MFC).
**Stack:** FastAPI (Python) → Retell (voice platform) + Zep V3 (memory) +
Supabase (DB) + OpenAI embeddings (via Supabase RPC, **not** the Python service)
+ Resend (email).
**Deploy target:** Railway. Auto-deploys from `main`.
**Repo:** https://github.com/GuyfromMontana/MFC-single-agent-
**Railway project:** `MFC-single-agent-production.up.railway.app` (`4fe681a5-ccbe-4c10-911c-0cda7c8d1272`)

**Use cases (clarified 2026-04-09 — load-bearing):**
1. Website widget on `mtfeedco.com` (Squarespace) for self-serve product Qs + message-taking.
2. After-hours answering machine — picks up when nobody answers at a store, takes a message, emails it to the store manager.

**NOT the main inbound channel during business hours.** Real humans answer phones while stores are open. The agent is overflow + off-hours. Realistic callers do not know employee names by heart — build for "place an order / message somebody at Dillon," not for "I need Sheryl." This is why name-lookup is de-prioritized even though it works end-to-end.

### Layout

```
mfcagent/
├── main.py                 # FastAPI app + Retell webhooks (~1050 lines)
├── retell_auth.py          # HMAC signature verify + admin-token guard
├── config.py               # env loading, Supabase client, httpx pools, PII redact
├── env.template            # All required env vars (keep in sync with code)
├── skills/
│   ├── memory.py           # Zep V3 caller lookup + transcript save
│   ├── leads.py            # `leads` + `callbacks` table writes (async)
│   ├── specialists.py      # LPS lookup by name or town/county (async)
│   └── knowledge.py        # RAG search over knowledge base (async)
├── retell_mfc_config.json  # Reference Retell agent config — NOT auto-synced to Retell dashboard
├── retell_system_prompt_v{7,8,9,11}.md  # Versioned system prompts (v11 is current)
├── supabase/               # local CLI workspace
├── backfill_embeddings.py  # one-off Python embedding backfill
└── regenerate-embeddings.js # one-off Node embedding regen
```

### Key concepts

- **Caller key:** phone number, or `widget_<call_id>` if no phone (web widget).
- **`_call_cache`** in main.py keeps Zep lookup hot between `call_inbound` and `call_ended`. 1-hour TTL. Also stores the last-resolved specialist as Layer 1 fallback for `schedule_callback`.
- **LPS** = Livestock Performance Specialist. Only LPSs get live transfers (`is_lps()`); everyone else is message-only via the `callbacks` table.
- **Memory write path:** `save_call_to_zep()` in `skills/memory.py` extracts name/location from transcript, updates Zep user metadata, and upserts the caller into `leads`.
- **Phase 1 caller resolution (shipped 2026-05-13):** `call_inbound` looks up phone in `caller_contacts` (1,280 known callers), surfacing `{{warehouse}}`, `{{is_customer}}`, `{{customer_city}}`, `{{last_purchase}}` as dynamic vars to the agent.

---

## Critical production rules (don't unwind these)

### Retell tool-call body shape — use `_extract_args(body)`

Retell now sends tool args at the **top level** as `body["args"]`, not nested under `body["arguments"]`. Reading the wrong shape was a silent failure for an entire build day — the endpoint was searching the staff table for the literal string `"lookup_staff_by_name"` (the tool's own name) and returning 0. The catch-all email layer was masking it perfectly.

Always go through the helper:

```python
def _extract_args(body: dict) -> dict:
    if isinstance(body.get("args"), dict):       return body["args"]
    if isinstance(body.get("arguments"), dict):  return body["arguments"]
    return body
```

Every Retell function endpoint must call it. Fix shipped in `46b0b93`.

### OpenAI is unreachable from Railway's network

Railway's egress cannot hit `api.openai.com` (root cause unknown — likely IP block or TLS handshake). The embedding call was moved into a Supabase RPC (`match_knowledge_base`) that calls OpenAI via the Supabase `http` extension. The Python service sends `query_text` (plain string) — **never `query_embedding`**.

Do not reintroduce `from openai import OpenAI` or any `OpenAI(...)` constructor anywhere in the FastAPI service. The new SDK ValueErrors on `api_key=None`, so even an unused dead import will crash the container on startup. This already happened once — root cause of the 2026-04-09 outage. Grep periodically.

`OPENAI_API_KEY` is **not** a Railway env var on this service.

### `lookup_staff_by_name` is a pure-Python filter, NOT a PostgREST query

PostgREST `or_()` filters with embedded spaces + nested `and(...)` clauses silently return zero for multi-word names. Don't try to "optimize" the endpoint by pushing the filter back to the DB. Pull all active rows (~13) and filter in Python.

### Zep PATCH `null` is a no-op — clear with `""`

Zep's PATCH `/users/{id}` body `{"metadata": {key: null}}` preserves the existing value (it merges). To "delete" a metadata key, set it to `""`. Downstream code already treats falsy as "no value." The comment in `skills/memory.py:zep_create_or_update_user` claiming Zep PATCH replaces wholesale is wrong; the function only works because it pre-merges locally before sending.

### Catch-all email always wins over silent message drops

`CATCHALL_MESSAGE_EMAIL` (env var) receives the full transcript when `schedule_callback` or `call_ended` can't resolve a specialist. Without it, messages vanish — the agent says "Sheryl will get it" and Sheryl gets nothing. The `_call_cache` Layer 1 + catch-all Layer 2 pair is the reason production message routing finally worked end-to-end.

### `retell_mfc_config.json` is reference-only

The live Retell agent config lives in Retell's dashboard. The JSON file in this repo documents intended state but **is not auto-synced**. Edits to the JSON do nothing in production until manually mirrored in the dashboard. Tooling to push automatically would need a Retell API key + a sync script.

---

## Active todos

- [ ] **Push pending local commits to origin/main** if any (sandbox has no GitHub creds). Most recent shipped: `46b0b93`.
- [ ] **Insert Mike Vanek into `specialists`** — `mvanek@landolakes.com`, `406-366-4668`, primary counties Petroleum + Garfield + parts of Phillips. He's already in Eagle as code `09` with one customer.
- [ ] **Delete Danielle Peterson row** from `specialists` (`is_active=false`, no longer with MFC; she still ranks first on inactive-included queries).
- [ ] **Populate `warehouses` table:** insert missing Missoula row; fill `manager_name`/`email`/`phone` per store; add `retell_did` column for Option B per-`to_number` routing.
- [ ] **Decide Fergus County rule (Brady vs Mike)** — geographic split breaks because both have real customer books there. Architectural answer: customer→salesrep lookup (Track B Phase 2), but that's blocked on data coverage (1.4% of customers have `cr_salesman_no` in Eagle).
- [ ] **Option B — 1 agent + 5 phone numbers + per-`to_number` dynamic vars** (location-specific greeting + recipient). Build scope ~2-3 hrs. Wait until widget data is in.
- [ ] **Sync `retell_mfc_config.json` → Retell dashboard:** new `lookup_staff_by_name` tool, fixed `schedule_callback` URL, Danielle removed from transfer destinations. Status unclear since the file diverged from prod months ago.
- [ ] **Widget deployment to `mtfeedco.com`:** Public Key allowlist must list `mtfeedco.com` + `www.mtfeedco.com` (NOT `montanafeed.com`). Squarespace Code Injection requires Business plan. Embed code is in [project_mfc_voice_agent.md memory].
- [ ] **Vercel `find-specialist.js` wrong fallback** — `406-683-2189` should be `406-728-7020`. Manual Vercel deploy of the `mfcagent` Vercel project needed; auto-deploy looks broken.
- [ ] **Dashboard `/api/calls` still queries the dead Vapi API** — rewrite to read Supabase `conversations` + `conversation_messages`.
- [ ] **Enable RLS on 5 AR tables** (`ar_customers`, `ar_invoices`, `ar_sync_runs`, `ar_statement_runs`, `ar_statements_sent`). Separate from voice agent but flagged by Supabase advisor.
- [ ] Pin versions in `requirements.txt` (currently unpinned: `fastapi`, `uvicorn[standard]`, `httpx`, `supabase`, `openai` — TODO drop this — `zep-cloud`).
- [ ] Verify `leads.phone` has UNIQUE before switching capture to `upsert(on_conflict="phone")`.
- [ ] Handle apostrophe/hyphen names in the regex (O'Brien, Jean-Luc).
- [ ] Move `MONTANA_TOWN_TO_COUNTY` (160-line dict in `skills/specialists.py`) to a DB table.
- [ ] Split `main.py` (~1080 lines) into routers: admin, inbound, functions.

## Done (recent)

- **2026-05-13 PM** — Found and fixed the bug. Retell body shape change (`body["args"]` vs `body["arguments"]`) was silently breaking every tool call. Sheryl finally received an email at `sheryl@axmen.com` end-to-end. Fix: `46b0b93`. Same session: Zep `null`-is-noop discovery + `""` fix (`691cd63`); pure-Python `lookup_staff_by_name` rewrite (`0089586`); per-call specialist cache + catch-all email layer (`e4f5471`, `55c152e`); `/clear-zep-metadata` and `/debug/staff-lookup` admin endpoints (`124fd8b`, `31febc2`).
- **2026-05-13 AM** — Phase 1 wired (`f69f4de`): `caller_contacts` → `{{warehouse}}` + `{{is_customer}}` + `{{customer_city}}` + `{{last_purchase}}` dynamic vars. v8 prompt published with NW MT Hwy 93 special-case handling.
- **2026-05-11** — Specialist routing audit: cleared Sheryl Shea's counties (floating helper, not territorial); identified Danielle Peterson stale row; planned Mike Vanek insert. Eagle salesperson discovery via pymysql confirmed only 220 / 15,277 customers have `cr_salesman_no` populated.
- **2026-04-24** — Code review round 1-3 (commit `35724e2`): Zep metadata merge fix, HTML-escape specialist emails, sanitized PostgREST filter tokens, async knowledge base, admin endpoints require `X-Admin-Token`, persistent httpx client, PII redaction.
- **2026-04-15** — Webhook security + async refactor (`46bacb6`): HMAC verification enforced; every blocking Supabase call wrapped in `asyncio.to_thread`; batched transcript inserts; Specialist email via `BackgroundTasks`; 1-hour TTL on `_call_cache`.
- **2026-04-09** — OpenAI removed from Python service after Railway-egress discovery (`014611d`). Strategic pivot: widget + after-hours focus, name-lookup deprioritized. Mfc-voice-dashboard admin panel + `specialist_audit_log` table shipped. Phase 2 `lookup_staff_by_name` endpoint + `schedule_callback` rework with `callbacks`-table writes + Resend email per call.

---

## Environment variables (production — Railway)

See `env.template` for the full set. Critical ones:

| Var | Why it matters |
|---|---|
| `SUPABASE_URL` / `SUPABASE_KEY` | DB. Use `SUPABASE_KEY`, not `SUPABASE_SERVICE_KEY` — config.py reads the former. |
| `ZEP_API_KEY` | Caller memory lookups |
| `RETELL_API_KEY` | **Required.** Webhook HMAC verification; fails closed without it. |
| `RETELL_SIGNATURE_ENFORCE` | Set `false` ONLY for local dev. |
| `RESEND_API_KEY` + `FROM_EMAIL` | Specialist + catch-all emails. `FROM_EMAIL` should be `notifications@axmen.com` — `axmen.com` is the only domain verified in Resend. |
| `CATCHALL_MESSAGE_EMAIL` | Unrouted-message inbox. Set to `guy@axmen.com`. Falls back to `FROM_EMAIL`. **Required** for message-loss protection. |
| `ADMIN_API_TOKEN` | Guards `/clear-zep-metadata`, `/fix-zep-user`, `/set-user-location`, `/debug/*`. Pass as `X-Admin-Token` header. Leave unset to disable those endpoints entirely. |
| `MFC_MAIN_OFFICE_PHONE` | `406-728-7020`. Constant at the top of `main.py`. |
| `PORT` | **Do not set.** Railway injects it. |
| ~~`OPENAI_API_KEY`~~ | **NOT used.** Removed 2026-04-09; reintroducing it will not restore OpenAI access from Railway — see the Critical production rule above. |

`SUPABASE_SERVICE_ROLE_KEY` is the same value as `SUPABASE_KEY`; some one-off scripts read the longer name.

---

## Admin endpoint catalog (require `X-Admin-Token`)

- `POST /clear-zep-metadata` body `{"phone":"+1...","keys":["specialist","location"]}` — sets keys to `""` so Zep effectively deletes them.
- `POST /debug/staff-lookup` body `{"name":"Sheryl Shea"}` — runs `lookup_staff_by_name` server-side, returns match count + details. Use to diagnose ASR vs matcher issues without a real call.
- `POST /set-user-location` body `{"phone":"+1...","location":"Missoula"}` — merge-set location field.
- `POST /fix-zep-user` body `{"phone":"+1...","name":"Guy Hanson"}` — set Zep first_name.
- `GET /debug/state` — return cache size + bg task count.

---

## Operational gotchas

- **Widget calls** (no phone number) skip Zep memory; saved to Supabase only with a `widget_<call_id>` key. `schedule_callback` email path was verified to work with widget origin.
- **`conversations.vapi_call_id`** column actually stores Retell call IDs — legacy column name from the Vapi → Retell migration. Don't rename without migrating all readers.
- **`schedule_callback`** writes to the `callbacks` table, NOT `leads`. Falls back to `leads` only if the callback insert fails.
- **`lookup_staff` is misnamed** — does territorial lookup, not name lookup. Kept as a backwards-compat shim alongside `lookup_staff_by_name`. Can be removed once Retell dashboard config is verified to no longer reference it.
- **Specialist territory routing** uses `MONTANA_TOWN_TO_COUNTY` dict in `skills/specialists.py`. Adding a town here means a code change + deploy — known tech debt.
- **Per-call specialist cache** is what makes the agent reliable when ASR mishears a name later in the same call. Don't shorten the TTL below 1 hour.
- **Pinned `--workers 1`** in `Procfile` is deliberate (Zep client + cache state isn't safe across workers yet).

---

## Decisions log

- **2026-05-13** Catch-all email is mandatory infrastructure, not optional. Without it, mis-resolved specialists = silent message loss.
- **2026-05-11** Phone → Eagle salesrep routing (Phase 2) is blocked on data coverage (1.4% populated). Warehouse-default routing via `caller_contacts` (Phase 1) is the live model until backfilled.
- **2026-04-28** Architectural recommendation: 1 agent + 5 phone numbers + per-`to_number` dynamic vars (Option B). Rejected 5 separate per-location agents (5× cost, drift).
- **2026-04-09** Only LPSs get live transfers; non-LPS staff are message-only (avoids waking up warehouse/corporate at random hours).
- **Feb 2026** Migrated Zep V2 → V3. All memory functions use `zep.user.get_sessions` / `zep.memory.add` patterns.

---

## How to update this file

When we finish a chunk of work, append to **Done (recent)** with a date and move any new follow-ups into **Active todos**. Keep entries terse — this is a working memory, not a changelog. The **Critical production rules** section is durable — only edit it when a rule changes for real (a vendor API changes, an architectural decision reverses, etc.).
