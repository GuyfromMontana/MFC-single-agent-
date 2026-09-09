# MFC Voice Agent — Store Line Deployment Guide

**Goal:** each store's phone rings a few times; if nobody picks up, the call rolls to that store's own Retell number and the AI answers **knowing which store was dialed** — so it gives Miles City's hours to a Miles City caller and emails Tamra, not the global catch-all.

Written 2026-09-09. Audited against live Retell + Supabase the same day.

---

## 1. Where things actually stand today

Measured, not assumed:

| Thing | State |
|---|---|
| MFC's live DID | **+1 406-510-2925** → `agent_b1331271e65ded31ad45657e96`, version `latest_published` |
| Its webhook | `https://mfc-single-agent-production.up.railway.app/retell-inbound-webhook` OK |
| Other Retell number | +1 406-926-0235 → `agent_95e9478f…` = **the Axmen store agent. Not available for MFC.** |
| `warehouses.retell_did` | **NULL on all 5 stores** |
| `is_store_line` ever true | **0 of 185 calls.** This path has never once run. |
| Prompt reference to store vars | **NONE.** See the blocker below. |

The backend work is already done, and has been since 2026-08-04. `lookup_warehouse_by_did()` in `skills/warehouses.py` matches the inbound `to_number` against `warehouses.retell_did` (last 10 digits, so any format works), and `main.py` sets five dynamic variables from it:

```
is_store_line   store_name   store_manager   store_hours   store_phone
```

It also stashes `store_manager_email` on the caller cache so `schedule_callback` and `call_ended` prefer the **store manager** over the global catch-all.

### Blocker — do step 2 first or none of this does anything

**Prompt v17 never references any of those five variables.** Retell only substitutes a variable where `{{var}}` literally appears in the prompt. Right now the backend computes all five and throws them away. If you wire the DIDs and skip step 2, every store call behaves exactly like a main-line call and you will think the DIDs are broken.

### One DID per store is required — you cannot share one

I checked whether the SIP `Diversion` header could identify which store forwarded a call, which would have allowed a single shared number. It cannot. On all 15 forwarded calls in the history the header reads:

```
diversion = <sip:+14065102925@twilio.com>;reason=unconditional
```

That is the Retell number echoed back, not the store's number. So `to_number` is the only reliable discriminator — hence a dedicated DID per store.

Also note `reason=unconditional`: whoever tested forwarding used **immediate** forwarding, not no-answer. That is the opposite of what you want here.

---

## 2. Add store-line mode to the prompt (v18)

Copy v17 first, then make two edits.

**(a)** Add to the `## THIS CALLER'S INFO (READ THIS FIRST)` block:

```
**Dialed a store line:** {{is_store_line}} — {{store_name}}
**That store's hours:** {{store_hours}}
**That store's phone:** {{store_phone}}
**That store's manager:** {{store_manager}}
```

**(b)** Add a new section, just before `## TURN-TAKING — CRITICAL`:

```markdown
## STORE LINE MODE — WHEN {{is_store_line}} IS "true"
This caller dialed the **{{store_name}}** store directly and nobody there picked
up, so it rolled to you. They were not calling a call center — they were calling a
building they have probably stood inside. Act like the front counter, not the
switchboard.

1. **Say the store's name in the greeting.** "Montana Feed {{store_name}} — the
   crew's away from the counter, so you got the AI. What do you need?" Never open
   with a generic "Montana Feed Company" on a store line; it makes the caller think
   the forward is broken.
2. **{{store_name}} is their store.** Do not ask what town they're calling from and
   do not ask which store they want — you already know. Do not read the five-store
   list to them.
3. **{{store_hours}} and {{store_phone}} are already in your hands.** Answer hours
   and callback-number questions from those variables directly. No `get_warehouse`
   call needed for THIS store — still call it for any OTHER store.
4. **Messages go to {{store_manager}}, not the catch-all.** Someone who rang the
   store wants the store, so `schedule_callback` should reach that manager. The
   backend already prefers the store manager's email when the call arrived on a
   store line — just capture a real `message_content` as always.
5. **Feed and product questions still route to the LPS**, exactly as on the main
   line. Store line changes WHO hears about it, not what you're allowed to answer.
6. **If they want a different store or their LPS**, handle it normally — the
   territory rules and `lookup_town` are unchanged.
```

Publish it:

```bash
py deploy_prompt.py retell_system_prompt_v18.md
```

```bash
py deploy_prompt.py retell_system_prompt_v18.md --apply --title store-line-mode-v18
```

Reminder from `deploy_prompt.py`: a published Retell LLM cannot be PATCHed, so it goes `create-agent-version` → patch draft → `publish-agent-version`. The inbound number tracks `latest_published`, so it is live the moment it publishes.

---

## 3. Buy 5 Retell numbers — your call, this costs money

I did not purchase these. Five numbers, one per active store. Match the local area code so it looks right on caller ID:

| Store | Code | Area code to request |
|---|---|---|
| Dillon (HQ) | DL | 406 |
| Miles City | MC | 406 |
| Lewistown | LT | 406 |
| Columbus | CB | 406 |
| Riverton, WY | RV | **307** |

Buy in the Retell dashboard, or one at a time:

```bash
curl -X POST https://api.retellai.com/create-phone-number -H "Authorization: Bearer $RETELL_API_KEY" -H 'Content-Type: application/json' -d '{"area_code":406,"nickname":"MFC Miles City store line","inbound_agent_id":"agent_b1331271e65ded31ad45657e96","inbound_agent_version":"latest_published","inbound_webhook_url":"https://mfc-single-agent-production.up.railway.app/retell-inbound-webhook"}'
```

Set a `nickname` on every one. The current main number has an empty nickname and it is already annoying to tell them apart.

---

## 4. Bind each number (if you bought them in the dashboard)

Both fields matter. **The Axmen number has no `inbound_webhook_url` set — if you miss this, the agent answers with zero caller context and no store awareness.**

```bash
curl -X PATCH https://api.retellai.com/update-phone-number/+1406XXXXXXX -H "Authorization: Bearer $RETELL_API_KEY" -H 'Content-Type: application/json' -d '{"inbound_agent_id":"agent_b1331271e65ded31ad45657e96","inbound_agent_version":"latest_published","inbound_webhook_url":"https://mfc-single-agent-production.up.railway.app/retell-inbound-webhook"}'
```

Verify them all at once:

```bash
curl -s https://api.retellai.com/list-phone-numbers -H "Authorization: Bearer $RETELL_API_KEY" | py -m json.tool
```

---

## 5. Populate `warehouses.retell_did`

This is the switch that turns store mode on. Any format works — the lookup strips to the last 10 digits.

```sql
update warehouses set retell_did = '+1406XXXXXXX' where warehouse_code = 'DL';
update warehouses set retell_did = '+1406XXXXXXX' where warehouse_code = 'MC';
update warehouses set retell_did = '+1406XXXXXXX' where warehouse_code = 'LT';
update warehouses set retell_did = '+1406XXXXXXX' where warehouse_code = 'CB';
update warehouses set retell_did = '+1307XXXXXXX' where warehouse_code = 'RV';
```

Confirm no store is left out and no DID is duplicated:

```sql
select warehouse_code, city, phone, retell_did from warehouses where is_active order by warehouse_code;
```

`lookup_warehouse_by_did` filters on `is_active = true` and `retell_did is not null`, so a NULL row silently falls back to main-line behavior — no error, just nothing happening. That is the failure mode to watch for.

---

## 6. Forward each store cell on no-answer

**The store lines are Verizon cell phones**, not a PBX — per the `lookup_warehouse_by_did` docstring, and consistent with the numbers below. So this is a Verizon star code dialed **from each store's own phone**.

| Store | Store cell to dial from | Manager |
|---|---|---|
| Dillon | 406-499-9642 | Kase Stoddard |
| Miles City | 406-851-1833 | Tamra Hodgins |
| Lewistown | 406-380-2099 | Brenda Atchison-Curry |
| Columbus | 406-931-0030 | Dan Otis |
| Riverton | 307-840-5469 | Kristena Dickinson |

On the store's phone, dial — **no dashes** — then press call:

```
*92 1406XXXXXXX
```

| Code | Does |
|---|---|
| `*92` | forward **only when unanswered** (and typically busy). This is the one you want. |
| `*72` | forward **immediately**. Do NOT use — the store phone would never ring at all. |
| `*93` | cancel the no-answer forward. This is your rollback. |

Listen for the confirmation tone, hang up, then call the store from another phone and let it ring through.

### "Three rings" is approximate on Verizon — read this

Verizon's no-answer ring count is **set by the carrier and is not adjustable through star codes**. Published behavior is roughly **3–6 rings (~20–25 seconds)** before it forwards. North American ring cadence is 2s on / 4s off, so a true three rings is about 18s. You will land near it, not on it.

If ~20s is acceptable, `*92` is the whole job — simple, free, reversible with `*93`.

**If exactly three rings is a hard requirement**, star codes cannot do it. The deterministic option is a Twilio number in front of each store:

```xml
<Response>
  <Dial timeout="18" callerId="{{From}}">
    <Number>+1406XXXXXXX</Number>              <!-- the store cell -->
  </Dial>
  <!-- unanswered after 18s falls through to here -->
  <Dial><Number>+1406YYYYYYY</Number></Dial>   <!-- that store's Retell DID -->
</Response>
```

`timeout` is in seconds and fully under your control. The cost is that the Twilio number becomes the store's published number, so you would either advertise the new number or port the existing one. Bigger change — only worth it if the ring count genuinely matters.

Before rolling out all five: **confirm `*92` with Verizon Business and test on ONE store.** Star codes vary by account type, and getting it wrong means store calls go nowhere. Dillon is the natural guinea pig since it is HQ and Kase is easy to reach.

---

## 7. Verify

Per store, from a phone that is **not** in `specialists` or `order_users` — a staff number triggers STAFF MODE and masks the store greeting:

1. Call the store number. Let it ring out.
2. The AI should answer naming **that store**.
3. Ask "what time do you close?" — it should answer from `{{store_hours}}` with no lookup pause.
4. Leave a message. Confirm **the store manager** gets the email, not the catch-all.

Then confirm in the data:

```bash
curl -s -X POST https://api.retellai.com/v2/list-calls -H "Authorization: Bearer $RETELL_API_KEY" -H 'Content-Type: application/json' -d '{"limit":10,"sort_order":"descending","filter_criteria":{"agent_id":["agent_b1331271e65ded31ad45657e96"]}}' | py -c "import json,sys;[print(c.get('to_number'),'| is_store_line=',(c.get('retell_llm_dynamic_variables') or {}).get('is_store_line'),'| store=',(c.get('retell_llm_dynamic_variables') or {}).get('store_name')) for c in json.load(sys.stdin)]"
```

`is_store_line=true` with the right `store_name` means it is working. Railway logs will also show `[WAREHOUSE] to_number matched store line: <name>`.

---

## 8. Rollback

Fastest first — each is independent:

| To undo | Do |
|---|---|
| Stop forwarding at one store | dial `*93` on that store's phone |
| Turn off store mode, keep forwarding | `update warehouses set retell_did = null where warehouse_code = 'XX';` — instantly reverts to main-line behavior |
| Revert the prompt | `py deploy_prompt.py retell_system_prompt_v17.md --apply --title rollback-v17` |

Nothing here touches the main line (+1 406-510-2925), the territory routing, or the staff-mode work. All three changes are independently reversible.

---

## Order of operations

1. **Prompt v18** — must be first, or the DIDs appear broken
2. Buy 5 numbers *(needs your approval — this is a purchase)*
3. Bind agent + webhook on each
4. Populate `warehouses.retell_did`
5. `*92` on **one** store, verify end to end
6. Roll out the remaining four

Steps 1 and 4 can be done on request. Step 2 needs a go-ahead. Steps 5–6 have to happen on the physical store phones.
