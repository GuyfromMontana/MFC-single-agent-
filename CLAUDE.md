# MFC Agent — Working Memory

Living notes for Guy + Claude. Update this file as we work so future sessions
have context without re-reading the whole codebase.

---

## Project at a glance

**What it is:** Voice AI agent for Montana Feed Company (MFC).
**Stack:** FastAPI (Python) → Retell (voice platform) + Zep V3 (memory) +
Supabase (DB) + OpenAI embeddings + Resend (email).
**Deploy target:** Railway.
**Repo:** https://github.com/GuyfromMontana/MFC-single-agent-

### Layout

```
mfcagent/
├── main.py                 # FastAPI app + Retell webhooks (~1000 lines)
├── retell_handlers.py      # Separate Retell router (signature verify, etc.)
├── config.py               # env loading, Supabase client, Zep httpx pool
├── env.template            # All required env vars (keep in sync with code)
├── skills/
│   ├── memory.py           # Zep V3 caller lookup + transcript save
│   ├── leads.py            # `leads` + `callbacks` table writes (async)
│   ├── specialists.py      # 7 LPS lookup by name or town/county (async)
│   └── knowledge.py        # RAG search over knowledge base
├── supabase/               # SQL migrations
├── supabase-functions/     # Edge functions
├── backfill_embeddings.py  # one-off Python embedding backfill
└── regenerate-embeddings.js # one-off Node embedding regen
```

### Key concepts

- **Caller key:** phone number, or `widget_<call_id>` if no phone (web widget).
- **`_call_cache`** in main.py keeps Zep lookup hot between `call_inbound`
  and `call_ended`. 1-hour TTL.
- **LPS** = Livestock Performance Specialist. Only LPSs get live transfers
  (`is_lps()`); everyone else is message-only via `callbacks` table.
- **Memory write path:** `save_call_to_zep()` in `skills/memory.py` extracts
  name/location from transcript, updates Zep user metadata, and upserts the
  caller into `leads`.

---

## Active todos / known work

- [ ] **Push commit `46bacb6` to origin/main** (Guy needs to push from local —
      sandbox has no GitHub creds).
- [ ] **Add `RETELL_API_KEY` to Railway env** before next deploy, or webhook
      will reject all Retell calls (signature verification is now enforced).
- [ ] **Test end-to-end inbound call** to confirm batched message insert works
      against the existing `conversation_messages` Supabase schema.
- [ ] Pin versions in `requirements.txt` (currently unpinned: `fastapi`,
      `uvicorn[standard]`, `httpx`, `supabase`, `openai`, `zep-cloud`).
- [ ] Pre-existing local edits in `config.py`, `requirements.txt`,
      `skills/__init__.py`, `skills/knowledge.py` are CRLF/LF noise only —
      either normalize line endings repo-wide (.gitattributes) or stash them.

## Done (recent)

- **2026-04-15** — Webhook security + async refactor. Commit `46bacb6`.
  - Re-enabled Retell HMAC signature verification (`RETELL_SIGNATURE_ENFORCE`
    env toggle, fails closed when `RETELL_API_KEY` is set).
  - Wrapped every blocking Supabase `.execute()` in `asyncio.to_thread` —
    main.py + skills/leads.py + skills/specialists.py.
  - Made `lookup_staff_by_name`, `lookup_specialist_by_town`,
    `capture_lead`, `update_lead_with_name`, `get_caller_name_from_leads`,
    `create_message_for_specialist` all `async`. Updated all call sites.
  - Batched transcript message inserts (was N+1 → 1 insert per call).
  - Moved specialist email send to FastAPI `BackgroundTasks`.
  - Added 1-hour TTL + eviction to `_call_cache`.
  - Fixed naive `datetime.fromtimestamp()` → `tz=timezone.utc`.
  - Removed dead `init_clients()` in `retell_handlers.py`.
  - Updated `env.template` to include all 8 vars used.

---

## Environment variables (production)

See `env.template` for the full set. Critical ones:

| Var | Why it matters |
|---|---|
| `SUPABASE_URL` / `SUPABASE_KEY` | DB writes for conversations, leads, callbacks |
| `ZEP_API_KEY` | Caller memory lookups |
| `RETELL_API_KEY` | **Required** for webhook signature verification |
| `RETELL_SIGNATURE_ENFORCE` | Set `false` only for local dev |
| `RESEND_API_KEY` + `FROM_EMAIL` | Specialist notification emails |
| `OPENAI_API_KEY` | Embeddings backfill scripts |

---

## Operational gotchas

- **Widget calls** (no phone number) skip Zep memory; they're saved to
  Supabase only with a `widget_<call_id>` key.
- **`conversations.vapi_call_id`** column actually stores Retell call IDs —
  legacy column name from the Vapi → Retell migration.
- **`schedule_callback` endpoint** writes to `callbacks` table, NOT `leads`.
  Falls back to `leads` only if callback insert fails.
- **Specialist territory routing** uses `MONTANA_TOWN_TO_COUNTY` dict in
  `skills/specialists.py` — add new towns there if a county shifts.

---

## Decisions log

- **2026-04-09** Only LPSs get live transfers; non-LPS staff are
  message-only (avoids waking up warehouse/corporate at random hours).
- **Feb 2026** Migrated Zep V2 → V3. All memory functions use
  `zep.user.get_sessions` / `zep.memory.add` patterns.
- **2026-04-15** Pure CPU helpers (`is_lps`, `resolve_town_to_county`)
  intentionally left synchronous — no benefit to offloading them.

---

## How to update this file

When we finish a chunk of work, append to **Done (recent)** with a date and
move any new follow-ups into **Active todos**. Keep entries terse — this is
a working memory, not a changelog.
