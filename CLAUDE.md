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
├── retell_mfc_config.json  # Reference Retell agent config — transfer destinations script-synced, rest manual
├── deploy_retell_config.py # LPS roster sync: Supabase specialists -> live transfer prompt + config JSON
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

### Every Supabase call goes through `config.sb_exec`, never bare `asyncio.to_thread`

The `supabase` client is a module-level singleton whose httpx client outlives the pod, and postgrest-py hardcodes `http2=True`. Supabase's edge periodically retires those long-lived connections with a graceful GOAWAY (`ConnectionTerminated error_code:0`), and httpcore does not replay the request that raced it — it raises `RemoteProtocolError`. On **2026-09-12** that silently dropped a completed call from `conversations`: webhook returned 200, transcript email still sent, nothing looked broken until Sentry fired. `sb_exec` retries only errors the server proved it never processed; `idempotent=True` additionally replays read-style timeouts. Adding a new Supabase query? Use `sb_exec`, and think about which flag it deserves — an INSERT without a client-side PK must stay `idempotent=False`.

### KB rows are embedded as `Question: … 

Answer: …`, queries as bare text — that asymmetry is the main retrieval defect

Both writers agree on the format: the `generate-embedding` edge function and the SQL `regenerate_embedding`. So a row's vector is a 200-500 char *document* while a caller query is four words, and cosine between them is structurally low. A row scores **0.826** against its own question text, not ~1.0. The old note claiming "embeddings cover `question` only" was wrong, and it hid this.

Consequences worth knowing before you touch retrieval:
- The "strong matches top out ~0.65-0.70" comment in `skills/knowledge.py` is an artifact of this compression, not a property of `text-embedding-3-small`.
- **A long answer sinks its own row.** Keep new rows' answers short — it is a retrieval parameter, not just style.
- Adding a row phrased exactly like the caller still may not clear 0.4: "Where are you located?" exists verbatim and scores **0.3742**. Embedded question-only it would score **0.9148**.

The real fix, when you want it: embed `question` only in both writers and re-embed ~1,140 rows (one OpenAI call each). Cost is pennies. The tradeoff is that answer-only facts stop being searchable — which is what caller-phrasing sibling rows are for.

### `match_knowledge_base` timings are coupled to the client timeout — don't move one alone

The RPC generates its query embedding by calling OpenAI **synchronously from inside Postgres** via the `http` extension, because OpenAI is unreachable from Railway's network (see above). That leg is the only part that can hang, and pgsql-http's 5s default was blowing intermittently — surfacing to callers as `SEARCH_ERROR`.

Now: a `kb_query_embedding_cache` hit skips OpenAI entirely; a miss gets **2 attempts at 3.5s each**. `2 x 3.5s = 7s` is deliberately under the **10s** `SUPABASE_TIMEOUT` in `config.py`. Raise either number without the other and the client starts timing out mid-retry. Note `sb_exec` will NOT save you here: a blown RPC returns a PostgREST `XX000`, which is a server error, not a transport error — it retries the pipe, not the query.

The cache is keyed on the **exact** query text, not a normalized form. Lowercasing or collapsing whitespace would change the embedding and shift every similarity score, and KB scores already sit near the 0.4 threshold. Verified as a pure memo: cold and cached runs of the same query return identical similarity (0.7950), 1445ms vs 13ms. **If the embedding model ever changes, TRUNCATE that table.**

`SUPABASE_TIMEOUT` (`config.py`) pins the postgrest client to **10s read / 2s connect**. The library default is 120s, which parks a Retell tool call for two minutes on a single hung connection. The largest read this service issues is ~19 product rows, so anything still running at 10s is hung, not slow. Note the interaction with retries: an `idempotent=True` call that keeps hitting `ReadTimeout` can stack to ~30s across 3 attempts.


### `retell_mfc_config.json` is reference-only — EXCEPT transfer destinations

The live Retell agent config lives in Retell's dashboard. The JSON file in this repo documents intended state but **is not auto-synced** — with one exception: `call_transfer_destinations` and the live transfer_call tool's inferred-destination prompt are regenerated from the Supabase `specialists` table by `deploy_retell_config.py` (2026-07-31 rewrite). Run it after ANY specialists-table change:

```
py deploy_retell_config.py            # dry run, shows diffs
py deploy_retell_config.py --apply    # rewrites JSON + drafts/patches/publishes the agent
```

Roster names/numbers come from Supabase (`is_lps()` rows, phones via the same `_to_e164` rules as main.py); hand-written territory descriptions in the JSON are preserved by full-name match. Direct PATCH of a published LLM returns 400 — the script uses create-agent-version → update-retell-llm (draft) → publish-agent-version. The agent's SYSTEM prompt territory sections are NOT script-managed; the script prints a reminder to review them manually when roster membership changed. All other tool schemas in the JSON still require manual dashboard mirroring.

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
- [ ] ~~Verify `leads.phone` has UNIQUE~~ — **verified 2026-09-14: it does NOT.** Only `leads_pkey` on `id`. The two-query SELECT-then-INSERT/UPDATE path in `skills/leads.py` must stay until a UNIQUE constraint is actually added; `upsert(on_conflict="phone")` would raise today.
- [ ] Handle apostrophe/hyphen names in the regex (O'Brien, Jean-Luc).
- [ ] Move `MONTANA_TOWN_TO_COUNTY` (160-line dict in `skills/specialists.py`) to a DB table.
- [ ] Split `main.py` (~1080 lines) into routers: admin, inbound, functions.

## Done (recent)

- **2026-09-14 (4)** — Added 16 caller-phrasing KB rows across the six allowlisted categories (`source='caller_phrasing_2026-09-14'`; `delete from knowledge_base where source=…` reverts). Short answers on purpose (dilution), and they name towns instead of restating addresses/phones so the store-fact drift surface doesn't grow. End-to-end answer rate on a 21-question caller set: **20/21**. Only "where are you located" still misses (0.3788) — that one needs the embedding change above, not more phrasing.
- **2026-09-14 (3)** — KB search timeout fixed server-side. Added `kb_query_embedding_cache` + rewrote `match_knowledge_base` to memoize the embedding, cut the per-attempt HTTP timeout 5s -> 3.5s (transaction-local, can't leak across the pooler), and retry once on transport failure or a retryable status (408/429/5xx) while still failing fast on 401/4xx. Measured: cold 1445ms, cached **13ms**, identical similarity. Empty/blank/null queries now short-circuit to zero rows instead of embedding an empty string. Also silenced the `function_search_path_mutable` advisor for this function.
- **2026-09-14 (2)** — Pinned `postgrest_client_timeout` to `httpx.Timeout(10.0, connect=2.0)` via `ClientOptions`, replacing the 120s library default. Verified the value actually reaches `supabase.postgrest.session.timeout` (ClientOptions can silently no-op).
- **2026-09-14** — Supabase transport retry. Sentry caught a GOAWAY (`RemoteProtocolError`) on the `conversations` insert that silently lost the 2026-09-12 09:41 MDT call (confirmed: zero rows for 9/12). Added `sb_exec` to `config.py` and converted **all 19** Supabase call sites across `main.py` + `skills/` off bare `asyncio.to_thread`. Verified: 5 unit cases (GOAWAY replayed, write does NOT replay `ReadTimeout`, read does, exhaustion re-raises, non-transport errors pass straight through) + live reads against prod (territory RPC, KB RPC, warehouses, specialists, products).
- **2026-07-31** — LPS-list drift fix: rewrote `deploy_retell_config.py` as a Supabase→Retell roster sync (old version's assumptions were dead: destinations are now INFERRED not predefined, and published LLMs reject direct PATCH). Reads active LPSs from `specialists`, regenerates the transfer prompt + `retell_mfc_config.json` destinations, pushes via draft→patch→publish, verifies post-publish. Dry-run verified against live agent v55; not yet applied (pending diffs are cosmetic: alphabetical ordering + generic non-LPS refusal line replacing the Sheryl-by-name one).
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
- **`kb_query_embedding_cache` is disposable.** Safe to `TRUNCATE` at any time — it rebuilds on demand, at the cost of one OpenAI round trip per distinct query. It is also the thing that caps the blast radius of `match_knowledge_base` being callable by `anon`: repeat queries cost nothing, novel ones still hit OpenAI.
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
