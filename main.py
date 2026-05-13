"""
Montana Feed Company - Retell AI Webhook with Zep Memory Integration
Version 3.0.9 - WIDGET CALL SUPPORT
- Widget calls (no phone number) now handled gracefully with call_id fallback
- Widget conversations saved to Supabase (skips Zep which needs phone-based IDs)
- Cache, transfer, and agent webhook all support widget callers
- Previous: v3.0.8 call cache + email by name
"""

import asyncio
import html
import os
import time
import uuid
from datetime import datetime, timezone

# Initialize Sentry as early as possible so it captures import-time errors and
# all subsequent webhook activity. PII (phone numbers) is intentionally NOT
# sent — this codebase already routes phones through redact_phone() for logs.
import sentry_sdk

_sentry_dsn = os.getenv("SENTRY_DSN")
if _sentry_dsn:
    sentry_sdk.init(
        dsn=_sentry_dsn,
        traces_sample_rate=0.1,
        send_default_pii=False,
        environment=os.getenv("RAILWAY_ENVIRONMENT", "production"),
        release=os.getenv("RAILWAY_DEPLOYMENT_ID"),
    )

import httpx
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from retell_auth import (
    read_and_verify,
    unauthorized_response,
    verify_admin_token,
    forbidden_response,
)

# Import configuration and clients
from config import (
    supabase,
    ZEP_API_KEY,
    ZEP_BASE_URL,
    ZEP_HEADERS,
    get_zep_client,
    get_http_client,
    normalize_phone,
    redact_phone,
    lifespan,
    logger,
)

# Import skills
from skills import (
    # Memory
    lookup_caller_fast,
    save_call_to_zep,
    zep_update_user_metadata,
    # Specialists
    lookup_specialist_by_town,
    lookup_staff_by_name,
    is_lps,
    # Knowledge
    search_knowledge_base,
    # Leads
    capture_lead,
    update_lead_with_name,
    create_message_for_specialist,
    # Customers (Phase 1: caller_contacts phone -> customer + warehouse)
    lookup_customer_by_phone,
)

# Main office fallback number for the voice agent. Single source of truth —
# used by lookup_staff, lookup_staff_by_name, and schedule_callback when a
# request can't be routed to a specific person.
MFC_MAIN_OFFICE_PHONE = "406-728-7020"
MFC_MAIN_OFFICE_E164 = "+1" + MFC_MAIN_OFFICE_PHONE.replace("-", "")

# ============================================================================
# CALL CACHE - Store Zep lookups from call_started for reuse at call_ended
# ============================================================================
# Keyed by phone number (or widget_<call_id>), stores {"data": dict, "ts": float}.
# Entries auto-expire after CALL_CACHE_TTL_SECONDS so stalled/abandoned calls
# can't leak memory if call_ended never fires.
#
# PROCESS-LOCAL: This cache lives in the Python process. The Procfile pins
# uvicorn to --workers 1 for exactly this reason. If you ever need to scale
# out horizontally, swap this for a shared store (Redis) — otherwise
# call_inbound state won't match call_ended state across workers/pods.

CALL_CACHE_TTL_SECONDS = 60 * 60  # 1 hour — longer than any real call
_CACHE_SWEEP_INTERVAL = 300  # Evict expired entries at most every 5 minutes
_call_cache: dict[str, dict] = {}
_last_cache_sweep: float = 0.0


def _cache_set(key: str, data: dict) -> None:
    _call_cache[key] = {"data": data, "ts": time.time()}
    _cache_evict_expired()


def _cache_get(key: str) -> dict | None:
    entry = _call_cache.get(key)
    if not entry:
        _cache_evict_expired()  # opportunistic sweep on miss
        return None
    if time.time() - entry["ts"] > CALL_CACHE_TTL_SECONDS:
        _call_cache.pop(key, None)
        return None
    return entry["data"]


def _cache_evict_expired() -> None:
    """Drop entries past their TTL. Rate-limited so reads don't scan the
    whole dict on every webhook hit."""
    global _last_cache_sweep
    now = time.time()
    if now - _last_cache_sweep < _CACHE_SWEEP_INTERVAL:
        return
    _last_cache_sweep = now
    expired = [k for k, v in _call_cache.items() if now - v["ts"] > CALL_CACHE_TTL_SECONDS]
    for k in expired:
        _call_cache.pop(k, None)

# ============================================================================
# EMAIL CONFIGURATION
# ============================================================================

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "notifications@axmen.com")

async def send_specialist_email(specialist_email: str, specialist_name: str, caller_name: str, 
                                caller_phone: str, caller_location: str, call_summary: str,
                                duration: int = None):
    """Send email notification to specialist about new call"""
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set - skipping email")
        return False
    
    try:
        # Format duration nicely
        duration_str = f"{duration}s" if duration else "Unknown"
        if duration and duration >= 60:
            minutes = duration // 60
            seconds = duration % 60
            duration_str = f"{minutes}m {seconds}s"
        
        # Subject is a plain text field — Resend handles escaping.
        subject = f"New Call from {caller_name or caller_phone}"

        # Escape every caller-/ASR-supplied field before it lands in HTML.
        # Transcripts are arbitrary user speech and can contain `<`, `>`, `&`,
        # or even literal HTML/script fragments that most mail clients will
        # render. Treat them all as untrusted.
        safe_specialist = html.escape(specialist_name or "")
        safe_caller = html.escape(caller_name or "Unknown")
        safe_phone = html.escape(caller_phone or "")
        safe_location = html.escape(caller_location or "Not specified")
        safe_duration = html.escape(duration_str)
        safe_time = html.escape(datetime.now().strftime("%Y-%m-%d %I:%M %p MT"))
        safe_summary = html.escape(call_summary or "No transcript available")

        html_content = f"""
        <h2>New Call Received</h2>
        <p><strong>Specialist:</strong> {safe_specialist}</p>
        <hr>
        <p><strong>Caller:</strong> {safe_caller}</p>
        <p><strong>Phone:</strong> {safe_phone}</p>
        <p><strong>Location:</strong> {safe_location}</p>
        <p><strong>Duration:</strong> {safe_duration}</p>
        <p><strong>Time:</strong> {safe_time}</p>
        <hr>
        <h3>Conversation:</h3>
        <p style="white-space: pre-wrap; font-family: monospace; background: #f5f5f5; padding: 10px; border-radius: 5px;">{safe_summary}</p>
        <hr>
        <p><small>This is an automated notification from Montana Feed Company voice system.</small></p>
        """
        
        # Send via Resend API using the persistent outbound client — avoids
        # a TCP+TLS handshake on every send.
        client = get_http_client()
        if client is None:
            logger.error("❌ Outbound HTTP client not initialized — email skipped")
            return False

        response = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": FROM_EMAIL,
                "to": [specialist_email],
                "subject": subject,
                "html": html_content,
            },
        )

        if response.status_code == 200:
            logger.info(f"✅ Email sent to {specialist_email}")
            return True
        else:
            logger.error(f"❌ Email failed: {response.status_code} - {response.text}")
            return False
                
    except Exception as e:
        logger.error(f"❌ Email error: {e}", exc_info=True)
        return False

# ============================================================================
# FASTAPI APPLICATION
# ============================================================================

app = FastAPI(
    title="Montana Feed Retell Webhook",
    lifespan=lifespan
)

# ============================================================================
# WEBHOOK ENDPOINTS
# ============================================================================

@app.get("/health")
async def health_check():
    """Public health check. Keep the payload to boolean feature flags and
    static service metadata — do NOT leak runtime state like live call
    counts here (use /debug/state behind the admin token for that)."""
    return {
        "status": "healthy",
        "service": "montana-feed-retell-webhook",
        "version": "3.0.9",
        "lps_count": 7,
        "memory_enabled": bool(ZEP_API_KEY),
        "supabase_enabled": supabase is not None,
        "email_enabled": bool(RESEND_API_KEY),
        "persistent_client": get_zep_client() is not None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/debug/state")
async def debug_state(request: Request):
    """Admin-only runtime state (cache size, bg task count). Requires the
    same X-Admin-Token used by /fix-zep-user and /set-user-location."""
    if not verify_admin_token(request):
        return forbidden_response()
    from skills.memory import _background_tasks
    return {
        "active_calls_cached": len(_call_cache),
        "background_tasks": len(_background_tasks),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/retell-inbound-webhook")
async def retell_inbound_webhook(request: Request, background_tasks: BackgroundTasks):
    """Inbound webhook - handles call_started and call_ended events."""
    ok, _raw, body = await read_and_verify(request)
    if not ok:
        return unauthorized_response()
    try:
        event = body.get("event")

        logger.info(f"=== INBOUND WEBHOOK ===")
        logger.info(f"Event: {event}")

        # ========================================================================
        # CALL STARTED - Set dynamic variables with memory context
        # ========================================================================
        if event in ["call_inbound", "call_started"]:
            # Try both data structures
            call_data = body.get("call_inbound") or body.get("call", {})
            from_number = call_data.get("from_number", "")
            to_number = call_data.get("to_number", "")
            agent_id = call_data.get("agent_id", "")

            # Widget calls have no from_number — use call_id as fallback
            call_id = call_data.get("call_id", "")
            is_widget = not from_number
            caller_key = from_number or f"widget_{call_id}"

            logger.info(f"Inbound: {redact_phone(caller_key)} -> {redact_phone(to_number)} (agent: {agent_id}, {'widget' if is_widget else 'phone'})")

            # Check if we already cached this caller (call_inbound fires before call_started)
            redacted_key = redact_phone(caller_key)
            cached = _cache_get(caller_key)
            if cached is not None:
                memory_data = cached
                logger.info(f"[CACHE HIT] Using cached caller data for {redacted_key}")
            elif is_widget:
                # Widget caller — no Zep history possible, return new caller defaults
                memory_data = {
                    "found": False, "user_id": f"widget_{call_id}",
                    "caller_name": None, "caller_location": None,
                    "caller_specialist": None, "conversation_history": "",
                    "message": "Widget caller",
                    "customer_id": "", "primary_warehouse": "",
                    "is_existing_customer": False,
                }
                _cache_set(caller_key, memory_data)
                logger.info(f"[WIDGET] New widget caller, cached as {redacted_key}")
            else:
                # First event for this call — Zep lookup + customer_contacts lookup
                # in parallel, then merge. Customer data trumps "Caller"/"Unknown"
                # name placeholders from Zep but doesn't override a real Zep name
                # (a caller may have introduced themselves differently than the
                # billing record knows them).
                memory_data, customer_data = await asyncio.gather(
                    lookup_caller_fast(caller_key),
                    lookup_customer_by_phone(caller_key),
                )

                if customer_data:
                    # Prefer Zep name when set + non-placeholder; otherwise use
                    # customer name from caller_contacts.
                    zep_name = (memory_data.get("caller_name") or "").strip()
                    placeholder = zep_name.lower() in {"", "caller", "unknown", "new caller"}
                    if placeholder and customer_data.get("customer_name"):
                        memory_data["caller_name"] = customer_data["customer_name"]
                        logger.info(
                            f"[CUSTOMER] Filled caller_name from caller_contacts: "
                            f"{customer_data['customer_name']}"
                        )

                    # Always copy the operational fields the agent needs even when
                    # Zep already had a name.
                    memory_data["customer_id"] = customer_data.get("customer_id") or ""
                    memory_data["primary_warehouse"] = customer_data.get("primary_warehouse") or ""
                    memory_data["is_existing_customer"] = bool(customer_data.get("is_existing_customer"))
                    memory_data["customer_city"] = customer_data.get("city") or ""
                    memory_data["customer_last_purchase"] = customer_data.get("last_purchase") or ""
                else:
                    # Zero out the customer fields so downstream code can rely on
                    # them being present regardless of match status.
                    memory_data["customer_id"] = ""
                    memory_data["primary_warehouse"] = ""
                    memory_data["is_existing_customer"] = False
                    memory_data["customer_city"] = ""
                    memory_data["customer_last_purchase"] = ""

                _cache_set(caller_key, memory_data)
                logger.info(f"[CACHE MISS] Looked up Zep+customer_contacts, cached for {redacted_key}")

            caller_name = memory_data.get("caller_name")
            caller_location = memory_data.get("caller_location")
            caller_specialist = memory_data.get("caller_specialist")
            conversation_history = memory_data.get("conversation_history", "")
            primary_warehouse = memory_data.get("primary_warehouse", "")
            is_existing_customer = memory_data.get("is_existing_customer", False)

            # Always include all variables as strings (no None values). Booleans
            # become "true"/"false" so the agent prompt can compare them cleanly.
            dynamic_vars = {
                "name": caller_name if caller_name else "New caller",
                "is_returning": "true" if caller_name else "false",
                "conversation_history": conversation_history or "",
                "location": caller_location or "",
                "specialist": caller_specialist or "",
                # Phase 1 customer-aware routing additions
                "warehouse": primary_warehouse or "",
                "is_customer": "true" if is_existing_customer else "false",
                "customer_city": memory_data.get("customer_city", "") or "",
                "last_purchase": memory_data.get("customer_last_purchase", "") or "",
            }

            logger.info(
                f"[INBOUND] Dynamic vars: name={dynamic_vars['name']}, "
                f"location={dynamic_vars['location'] or 'None'}, "
                f"specialist={dynamic_vars['specialist'] or 'None'}, "
                f"warehouse={dynamic_vars['warehouse'] or 'None'}, "
                f"is_customer={dynamic_vars['is_customer']}"
            )
            
            if conversation_history:
                logger.info(f"[INBOUND] Context: {conversation_history[:100]}")

            return JSONResponse(content={
                "call_inbound": {
                    "dynamic_variables": dynamic_vars
                }
            })

        # ========================================================================
        # CALL ENDED - Save to Supabase conversations + messages tables
        # ========================================================================
        elif event == "call_ended":
            call_data = body.get("call", {})
            from_number = call_data.get("from_number", "")
            to_number = call_data.get("to_number", "")
            call_id = call_data.get("call_id", "")
            agent_id = call_data.get("agent_id", "")
            
            # Get transcript if available
            transcript = call_data.get("transcript", "")
            transcript_object = call_data.get("transcript_object", [])
            
            # Get call duration and timestamps
            start_time = call_data.get("start_timestamp")
            end_time = call_data.get("end_timestamp")
            duration_seconds = None
            start_datetime = None
            end_datetime = None
            
            if start_time and end_time:
                duration_seconds = int((end_time - start_time) / 1000)
                start_datetime = datetime.fromtimestamp(start_time / 1000, tz=timezone.utc)
                end_datetime = datetime.fromtimestamp(end_time / 1000, tz=timezone.utc)
            
            # Widget calls have no from_number — use call_id as fallback
            is_widget = not from_number
            caller_key = from_number or f"widget_{call_id}"

            logger.info(f"[CALL_ENDED] {redact_phone(caller_key)} ({'widget' if is_widget else 'phone'}), duration: {duration_seconds}s")

            # Use cached memory data if available, otherwise fall back to Zep
            cached = _cache_get(caller_key)
            if cached is not None:
                memory_data = cached
                logger.info(f"[CACHE HIT] Using cached Zep data for call_ended")
            elif is_widget:
                memory_data = {
                    "found": False, "caller_name": None,
                    "caller_location": None, "caller_specialist": None
                }
                logger.info(f"[WIDGET] No cached data for widget caller")
            else:
                logger.info(f"[CACHE MISS] No cached data - looking up Zep for call_ended")
                memory_data = await lookup_caller_fast(caller_key)

            caller_name = memory_data.get("caller_name")
            caller_location = memory_data.get("caller_location")
            specialist_name = memory_data.get("caller_specialist")

            logger.info(f"[MEMORY] Name: {caller_name or 'Unknown'}")
            logger.info(f"[MEMORY] Location: {caller_location or 'Unknown'}")
            logger.info(f"[MEMORY] Specialist: {specialist_name or 'Unknown'}")

            # Save transcript to Zep if available (use phone for Zep, skip for widget)
            if transcript_object and len(transcript_object) > 0:
                if not is_widget:
                    logger.info(f"[SAVE] Saving {len(transcript_object)} messages to Zep")
                    await save_call_to_zep(from_number, transcript_object, call_id, caller_name)
                else:
                    logger.info(f"[WIDGET] Skipping Zep save (no phone number for memory)")

            # Create a formatted summary from transcript
            call_summary = ""
            if transcript_object:
                messages = []
                for msg in transcript_object:
                    role = "Caller" if msg.get("role") == "user" else "Agent"
                    content = msg.get("content", "")
                    if content:
                        messages.append(f"{role}: {content}")
                call_summary = "\n\n".join(messages)
            elif transcript:
                call_summary = transcript

            # ====================================================================
            # SAVE TO SUPABASE
            # ====================================================================
            conversation_id = None
            
            if supabase:
                try:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    conversation_data = {
                        "id": str(uuid.uuid4()),
                        "phone_number": from_number or f"widget_{call_id}",
                        "conversation_type": "voice_call",
                        "direction": "inbound",
                        "status": "completed",
                        "start_time": start_datetime.isoformat() if start_datetime else None,
                        "end_time": end_datetime.isoformat() if end_datetime else None,
                        "duration_seconds": duration_seconds,
                        "vapi_call_id": call_id,
                        "ai_summary": call_summary[:500] if call_summary else None,
                        "created_at": now_iso,
                        "updated_at": now_iso,
                    }

                    # Wrap blocking Supabase call so it doesn't block the event loop
                    conversation_result = await asyncio.to_thread(
                        lambda: supabase.table("conversations").insert(conversation_data).execute()
                    )

                    if conversation_result.data and len(conversation_result.data) > 0:
                        conversation_id = conversation_result.data[0]["id"]
                        logger.info(f"✅ Created conversation: {conversation_id}")

                        # Defensive: only iterate if we actually have a list of dicts
                        if isinstance(transcript_object, list) and transcript_object:
                            messages_payload = []
                            for msg in transcript_object:
                                if not isinstance(msg, dict):
                                    continue
                                content = msg.get("content", "")
                                if not content:
                                    continue
                                messages_payload.append({
                                    "id": str(uuid.uuid4()),
                                    "conversation_id": conversation_id,
                                    "content": content,
                                    "sender": "user" if msg.get("role") == "user" else "assistant",
                                    "message_type": "voice",
                                    "created_at": now_iso,
                                })

                            if messages_payload:
                                # Single batched insert instead of N inserts
                                await asyncio.to_thread(
                                    lambda: supabase.table("conversation_messages").insert(messages_payload).execute()
                                )
                                logger.info(f"✅ Saved {len(messages_payload)} messages to conversation_messages (batched)")

                except Exception as e:
                    logger.error(f"❌ Failed to save to Supabase: {e}", exc_info=True)

            # ====================================================================
            # SEND EMAIL TO SPECIALIST (lookup by name, not town)
            # ====================================================================
            if specialist_name and RESEND_API_KEY:
                specialist_email = None

                if supabase:
                    try:
                        # Sanity-cap inputs before sending to ilike()
                        name_parts = specialist_name.split(None, 1)
                        first_name = name_parts[0][:50]
                        last_name = (name_parts[1] if len(name_parts) > 1 else "")[:50]

                        logger.info(f"[EMAIL] Looking up email for: {first_name} {last_name}")

                        result = await asyncio.to_thread(
                            lambda: supabase.table("specialists")
                                .select("email, first_name, last_name")
                                .ilike("first_name", first_name)
                                .ilike("last_name", last_name)
                                .eq("is_active", True)
                                .execute()
                        )

                        if result.data and len(result.data) > 0:
                            specialist_email = result.data[0].get("email")
                            logger.info(f"[EMAIL] Found email: {specialist_email}")
                        else:
                            logger.warning(f"[EMAIL] No specialist found matching: {first_name} {last_name}")

                    except Exception as e:
                        logger.error(f"[EMAIL] Specialist lookup error: {e}")

                if specialist_email:
                    # Fire email in the background so the webhook can return immediately.
                    background_tasks.add_task(
                        send_specialist_email,
                        specialist_email=specialist_email,
                        specialist_name=specialist_name,
                        caller_name=caller_name or "Unknown Caller",
                        caller_phone=from_number,
                        caller_location=caller_location or "Unknown",
                        call_summary=call_summary or "No transcript available",
                        duration=duration_seconds,
                    )
                    logger.info("[EMAIL] Queued specialist notification email (background)")
                else:
                    logger.warning(f"[EMAIL] No email found for specialist: {specialist_name}")
            else:
                if not specialist_name:
                    logger.warning("[EMAIL] No specialist assigned to caller")
                if not RESEND_API_KEY:
                    logger.warning("[EMAIL] RESEND_API_KEY not configured")

            # Clean up cache for this caller
            _call_cache.pop(caller_key, None)
            logger.info(f"[CACHE] Cleaned up cache for {redact_phone(caller_key)}")

            return JSONResponse(content={
                "call_id": call_id,
                "conversation_id": conversation_id,
                "messages_saved": len(transcript_object) if transcript_object else 0,
                "email_sent": bool(specialist_name and RESEND_API_KEY)
            })

        # ========================================================================
        # CALL ANALYZED
        # ========================================================================
        elif event == "call_analyzed":
            logger.info(f"Call analyzed event received")
            return JSONResponse(content={})

        # ========================================================================
        # CHAT INBOUND (SMS)
        # ========================================================================
        elif event == "chat_inbound":
            chat_inbound = body.get("chat_inbound", {})
            logger.info(f"SMS inbound from: {chat_inbound.get('from_number', '')}")
            return JSONResponse(content={"chat_inbound": {}})

        else:
            logger.warning(f"Unknown inbound event: {event}")
            return JSONResponse(content={})

    except Exception as e:
        logger.error(f"Inbound webhook error: {e}", exc_info=True)
        return JSONResponse(content={})


@app.post("/retell-webhook")
async def retell_webhook(request: Request):
    """
    Agent webhook - ONLY handles call_ended for analytics.
    Function calls are handled directly by /retell/functions/* endpoints.
    """
    ok, _raw, body = await read_and_verify(request)
    if not ok:
        return unauthorized_response()
    try:
        event_type = body.get("event", "unknown")
        logger.info(f"[AGENT] Webhook: {event_type}")

        call_data = body.get("call", {})
        call_id = call_data.get("call_id", "unknown")
        phone = call_data.get("from_number", "")
        transcript = call_data.get("transcript_object", [])

        is_widget = not phone
        caller_key = phone or f"widget_{call_id}"

        if event_type == "call_ended" and transcript and caller_key:
            logger.info(f"[SAVE] Saving {len(transcript)} messages ({'widget' if is_widget else 'phone'})")

            caller_name = body.get("retell_llm_dynamic_variables", {}).get("caller_name")
            if not caller_name or caller_name == "New caller":
                # Try cache first, then Zep
                cached = _cache_get(caller_key)
                if cached is not None:
                    caller_name = cached.get("caller_name")
                    logger.info(f"[CACHE HIT] Got caller name from cache: {caller_name}")
                elif not is_widget:
                    memory_data = await lookup_caller_fast(phone)
                    caller_name = memory_data.get("caller_name")

            if is_widget:
                logger.info(f"[WIDGET] Skipping Zep save for widget call {call_id}")
                save_result = {"success": True, "message": "Widget call - no Zep save"}
            else:
                save_result = await save_call_to_zep(phone, transcript, call_id, caller_name)

            if save_result.get("extracted_name"):
                logger.info(f"[SAVE] Name extracted: {save_result['extracted_name']}")
            if save_result.get("extracted_location"):
                logger.info(f"[SAVE] Location extracted: {save_result['extracted_location']}")

            return JSONResponse(content={
                "call_id": call_id,
                "memory_saved": save_result.get("success", False)
            })

        return JSONResponse(content={"call_id": call_id})

    except Exception as e:
        logger.error(f"[AGENT] Webhook error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": "Internal server error"})


@app.post("/fix-zep-user")
async def fix_zep_user(request: Request):
    """Fix Zep user data. Admin-only — requires matching X-Admin-Token header."""
    if not verify_admin_token(request):
        return forbidden_response()
    try:
        body = await request.json()
        phone = body.get("phone", "")
        name = body.get("name", "")

        if not phone or not name:
            return {"error": "Provide phone and name"}

        user_id = f"caller_{normalize_phone(phone)}"

        _zep_client = get_zep_client()
        if not _zep_client:
            return {"error": "Zep client not available"}

        response = await _zep_client.patch(
            f"{ZEP_BASE_URL}/users/{user_id}",
            headers=ZEP_HEADERS,
            json={"first_name": name}
        )

        if response.status_code == 200:
            name_parts = name.split(None, 1)
            await update_lead_with_name(phone, name_parts[0], name_parts[1] if len(name_parts) > 1 else "")
            return {"success": True, "message": f"Updated {user_id} to {name}"}
        else:
            return {"success": False, "error": response.text}

    except Exception as e:
        return {"error": str(e)}


@app.post("/set-user-location")
async def set_user_location(request: Request):
    """Set user location by merging metadata. Admin-only — requires X-Admin-Token."""
    if not verify_admin_token(request):
        return forbidden_response()
    try:
        body = await request.json()
        phone = body.get("phone", "")
        location = body.get("location", "")

        if not phone or not location:
            return {"error": "Provide phone and location"}

        user_id = f"caller_{normalize_phone(phone)}"
        success = await zep_update_user_metadata(user_id, {"location": location})

        if success:
            return {"success": True, "message": f"Set location for {user_id} to {location}"}
        else:
            return {"success": False, "error": "Failed to update metadata"}

    except Exception as e:
        return {"error": str(e)}


@app.post("/clear-zep-metadata")
async def clear_zep_metadata(request: Request):
    """Strip one or more keys from a Zep user's metadata. Admin-only.

    Body shape:
        {"phone": "+14062402889", "keys": ["specialist", "location"]}

    Why this exists: `zep_update_user_metadata` only MERGES new values into
    existing metadata — it can't remove a key. Stale fields (e.g. a
    specialist assignment from a previous territory map) get stuck and keep
    flowing into dynamic vars on every call. This endpoint fetches the
    user, drops the named keys, and PATCHes the trimmed metadata back.
    """
    if not verify_admin_token(request):
        return forbidden_response()
    try:
        body = await request.json()
        phone = (body.get("phone") or "").strip()
        keys = body.get("keys") or []

        if not phone or not isinstance(keys, list) or not keys:
            return {"error": "Provide phone and a non-empty keys list"}

        user_id = f"caller_{normalize_phone(phone)}"

        _zep_client = get_zep_client()
        if not _zep_client:
            return {"error": "Zep client not available"}

        # Fetch current user metadata
        get_resp = await _zep_client.get(
            f"{ZEP_BASE_URL}/users/{user_id}", headers=ZEP_HEADERS
        )
        if get_resp.status_code != 200:
            return {
                "success": False,
                "error": f"Zep GET failed: {get_resp.status_code} {get_resp.text}",
            }
        user = get_resp.json()
        md_before = user.get("metadata") or {}

        # Compute trimmed metadata
        removed_keys = [k for k in keys if k in md_before]
        md_after = {k: v for k, v in md_before.items() if k not in keys}

        if not removed_keys:
            return {
                "success": True,
                "message": f"No-op — none of {keys} present in metadata",
                "user_id": user_id,
                "metadata": md_before,
            }

        # PATCH wholesale — Zep replaces metadata with whatever we send.
        patch_resp = await _zep_client.patch(
            f"{ZEP_BASE_URL}/users/{user_id}",
            headers=ZEP_HEADERS,
            json={"metadata": md_after},
        )
        if patch_resp.status_code != 200:
            return {
                "success": False,
                "error": f"Zep PATCH failed: {patch_resp.status_code} {patch_resp.text}",
            }

        # Invalidate the per-call cache for this caller so an in-flight call
        # picks up the fresh metadata. Safe no-op if not cached.
        for k in (phone, user_id, f"+{normalize_phone(phone)}"):
            _call_cache.pop(k, None)

        return {
            "success": True,
            "user_id": user_id,
            "removed": removed_keys,
            "metadata_after": md_after,
        }

    except Exception as e:
        logger.error(f"[CLEAR_ZEP_METADATA] error: {e}", exc_info=True)
        return {"error": str(e)}


# ============================================================================
# FUNCTION ENDPOINTS (Called directly by Retell)
# ============================================================================

@app.post("/retell/functions/lookup_town")
async def lookup_town(request: Request):
    """Look up specialist by town and save to Zep metadata."""
    ok, _raw, body = await read_and_verify(request)
    if not ok:
        return unauthorized_response()
    try:
        args = body.get("arguments", {})
        town = args.get("town", "") or args.get("location", "") or args.get("city", "")
        
        call_data = body.get("call", {})
        phone = call_data.get("from_number", "")

        logger.info(f"[LOOKUP_TOWN] Searching for: '{town}'")

        specialist = await lookup_specialist_by_town(town)

        if specialist and phone:
            user_id = f"caller_{normalize_phone(phone)}"
            await zep_update_user_metadata(user_id, {
                "specialist": specialist["specialist_name"],
                "location": specialist.get("territory", town)
            })

            # Tell the agent whether this specialist is live-transfer eligible so it
            # can pick between transfer_call_tool and schedule_callback. Non-LPS
            # staff (managers, operations) should never be live-transferred.
            if specialist.get("is_lps"):
                result = (
                    f"{specialist['specialist_name']} handles {town}. "
                    f"They take live transfers — offer the caller a transfer or a message."
                )
            else:
                role_phrase = f" ({specialist.get('role')})" if specialist.get("role") else ""
                result = (
                    f"{specialist['specialist_name']}{role_phrase} covers {town}, "
                    f"but they don't take live calls — offer to take a message and email it to them."
                )
            logger.info(
                f"[LOOKUP_TOWN] Found: {specialist['specialist_name']} "
                f"(is_lps={specialist.get('is_lps')}), saved to Zep"
            )
        else:
            result = f"No specialist found for {town}. Contact our main office at {MFC_MAIN_OFFICE_PHONE}."
            logger.info(f"[LOOKUP_TOWN] No match for '{town}'")

        return JSONResponse(content={
            "result": result,
            "success": bool(specialist),
            "is_lps": bool(specialist and specialist.get("is_lps")),
            "specialist_id": specialist.get("id") if specialist else None,
            "specialist_name": specialist.get("specialist_name") if specialist else None,
            "specialist_email": specialist.get("specialist_email") if specialist else None,
        })
    except Exception as e:
        logger.error(f"[LOOKUP_TOWN] Error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/retell/functions/schedule_callback")
async def schedule_callback(request: Request):
    """
    Schedule a callback OR leave a message for a specific staff member.

    Two call shapes:

    1. SCHEDULED CALLBACK (caller wants a return call at a future time):
       { "caller_name": "...", "callback_time": "...", "reason": "..." }

    2. LEAVE A MESSAGE FOR X (caller wants a specific person to get a message):
       { "caller_name": "...", "reason": "message",
         "specialist_name": "Sheryl Shea",
         "specialist_id": "<uuid>",
         "specialist_email": "sheryl@axmen.com",
         "message_content": "..." }

    Both shapes write to the `callbacks` table (NOT `leads`). If a specialist
    email is present, the message is immediately sent via Resend.
    """
    ok, _raw, body = await read_and_verify(request)
    if not ok:
        return unauthorized_response()
    try:
        args = body.get("arguments", {})
        call_data = body.get("call", {}) or {}

        caller_name = args.get("caller_name") or args.get("name", "")
        caller_phone = args.get("phone") or call_data.get("from_number", "")

        # Fallback: the agent occasionally forgets to pass caller_name even
        # when it has it as {{name}}. Reach into the per-call cache populated
        # at call_inbound (Zep lookup) so messages don't end up labeled
        # "unknown" when we already know who's calling.
        if not caller_name and caller_phone:
            cached = _cache_get(caller_phone)
            if cached:
                caller_name = cached.get("caller_name") or ""

        reason = (args.get("reason") or "callback").strip().lower()
        callback_time = args.get("callback_time", "")
        callback_date = args.get("callback_date", "")
        callback_timeframe = args.get("callback_timeframe", "")
        territory_id = args.get("territory_id", "")
        message_content = args.get("message_content") or args.get("notes", "")

        specialist_id = args.get("specialist_id")
        specialist_name = args.get("specialist_name")
        specialist_email = args.get("specialist_email")

        # Compose a human-readable "when" line out of whatever date/time/timeframe
        # fragments Retell supplied. Any combination is valid.
        when_parts = [p for p in (callback_date, callback_time, callback_timeframe) if p]
        when_str = " ".join(when_parts).strip()

        # Compose the notes field: message body + any timing info we have so the
        # specialist sees the full request in their email.
        if reason == "message" and message_content:
            notes = message_content
            if when_str:
                notes += f"\n\nRequested callback: {when_str}"
        elif when_str:
            notes = f"Requested callback: {when_str}"
            if message_content:
                notes += f"\n\n{message_content}"
        else:
            notes = message_content or "(no details provided)"

        if territory_id:
            notes += f"\n\n(territory_id: {territory_id})"

        # Write to callbacks table via the skill function
        callback_id = await create_message_for_specialist(
            specialist_id=specialist_id,
            specialist_name=specialist_name,
            specialist_email=specialist_email,
            caller_name=caller_name,
            caller_phone=caller_phone,
            message=notes,
            reason=reason,
        )

        if not callback_id:
            # Fallback: at least log a lead so nothing is lost
            await capture_lead(caller_name, caller_phone, "callback", notes[:500])
            return JSONResponse(content={
                "result": (
                    "I've noted your request. Our team will follow up with you at "
                    f"{MFC_MAIN_OFFICE_PHONE} or the number you're calling from."
                ),
                "success": False,
            })

        # If we have a specialist email, fire off an email notification right now
        email_sent = False
        if specialist_email:
            try:
                email_sent = await send_specialist_email(
                    specialist_email=specialist_email,
                    specialist_name=specialist_name or "Team",
                    caller_name=caller_name or "Unknown caller",
                    caller_phone=caller_phone or "unknown",
                    caller_location="",
                    call_summary=notes,
                    duration=None,
                )
            except Exception as e:
                logger.error(f"[SCHEDULE_CALLBACK] Email send failed: {e}")

        # Build a user-facing confirmation the voice agent can speak back
        if reason == "message" and specialist_name:
            spoken = (
                f"Got it. I'll make sure {specialist_name} gets your message"
                f"{' by email' if email_sent else ''}. "
                f"They'll reach out to you at the number you called from."
            )
        elif when_str and specialist_name:
            spoken = f"Scheduled a callback from {specialist_name} for {when_str}."
        elif when_str:
            spoken = f"Scheduled your callback for {when_str}."
        else:
            spoken = "Your request has been noted and the team will follow up."

        return JSONResponse(content={
            "result": spoken,
            "success": True,
            "callback_id": callback_id,
            "email_sent": email_sent,
        })
    except Exception as e:
        logger.error(f"[SCHEDULE_CALLBACK] Error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/retell/functions/create_lead")
async def create_lead_endpoint(request: Request):
    """Create a new lead record.

    Accepts both the historical shape (`name`, `phone`, `location`, `interests`)
    and the richer shape promised by retell_mfc_config.json (`first_name`,
    `last_name`, `email`, `ranch_name`, `county`, `zip_code`, `livestock_type`,
    `herd_size`, `primary_interest`, `specialist_name`). Either is allowed —
    extras are folded into the lead's `primary_interest` notes so the
    specialist sees the full picture.
    """
    ok, _raw, body = await read_and_verify(request)
    if not ok:
        return unauthorized_response()
    try:
        args = body.get("arguments", {})

        first_name = (args.get("first_name") or "").strip()
        last_name = (args.get("last_name") or "").strip()
        name = (args.get("name") or "").strip()
        if not first_name and name:
            parts = name.split(None, 1)
            first_name = parts[0]
            last_name = last_name or (parts[1] if len(parts) > 1 else "")
        display_name = f"{first_name} {last_name}".strip() or name or "Caller"

        phone_num = args.get("phone") or body.get("call", {}).get("from_number", "")
        location = args.get("location") or args.get("county", "")
        primary_interest = args.get("primary_interest") or args.get("interests", "")

        # Same call-cache fallback as schedule_callback — if the agent didn't
        # pass any name fields but Zep already knew the caller, use that.
        if display_name == "Caller" and phone_num:
            cached = _cache_get(phone_num)
            if cached:
                cached_name = cached.get("caller_name")
                if cached_name:
                    display_name = cached_name
                    if not first_name:
                        parts = cached_name.split(None, 1)
                        first_name = parts[0]
                        last_name = last_name or (parts[1] if len(parts) > 1 else "")

        # Compose extras (ranch_name, herd, livestock, email, etc.) into the
        # interest field so we don't lose them — the leads table doesn't have
        # dedicated columns for these and we'd rather have the data in notes
        # than discarded entirely.
        extras = []
        for key in ("ranch_name", "herd_size", "livestock_type", "zip_code", "email", "specialist_name"):
            val = args.get(key)
            if val:
                extras.append(f"{key}={val}")
        if extras:
            primary_interest = (primary_interest + " | " if primary_interest else "") + " ".join(extras)

        success = await capture_lead(display_name, phone_num, location, primary_interest)
        result = f"Saved your info, {display_name}." if success else "Noted your information."

        return JSONResponse(content={"result": result, "success": success})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/retell/functions/search_knowledge_base")
async def search_knowledge_base_endpoint(request: Request):
    """Search the knowledge base for relevant information."""
    ok, _raw, body = await read_and_verify(request)
    if not ok:
        return unauthorized_response()
    try:
        query = body.get("arguments", {}).get("query", "")
        result = await search_knowledge_base(query)
        return JSONResponse(content={"result": result, "success": True})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/retell/functions/end_call")
async def end_call(request: Request):
    """End the call gracefully."""
    ok, _raw, _body = await read_and_verify(request)
    if not ok:
        return unauthorized_response()
    return JSONResponse(content={"result": "Thanks for calling Montana Feed!", "success": True})


@app.post("/retell/functions/lookup_staff")
async def lookup_staff(request: Request):
    """
    Legacy endpoint — misnamed. Historically this took a `location` arg and
    called `lookup_specialist_by_town`. Kept for backwards compatibility with
    any existing Retell agent config referencing this URL, but the agent
    should prefer `lookup_staff_by_name` for actual name-based requests and
    `lookup_town` for territorial routing.
    """
    ok, _raw, body = await read_and_verify(request)
    if not ok:
        return unauthorized_response()
    try:
        location = body.get("arguments", {}).get("location", "")
        phone = body.get("call", {}).get("from_number", "")

        specialist = await lookup_specialist_by_town(location)

        if specialist and phone:
            user_id = f"caller_{normalize_phone(phone)}"
            await zep_update_user_metadata(user_id, {
                "specialist": specialist["specialist_name"],
                "location": specialist.get("territory", location)
            })
            result = f"Your specialist is {specialist['specialist_name']} at {specialist['specialist_phone']}."
        else:
            result = f"Let me connect you with our main office at {MFC_MAIN_OFFICE_PHONE}."

        return JSONResponse(content={"result": result, "success": bool(specialist)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/retell/functions/lookup_staff_by_name")
async def lookup_staff_by_name_endpoint(request: Request):
    """
    Look up a staff member by name. Handles single names ("Sheryl"),
    full names ("Sheryl Shea"), or partials ("shea"). Returns structured
    data the voice agent can use to decide how to route the caller.

    Response shape:
        {
          "result": "<natural-language summary the agent can speak>",
          "success": true/false,
          "match_count": N,
          "matches": [
            {
              "id": "<uuid>",
              "full_name": "Sheryl Shea",
              "role": "manager",
              "email": "sheryl@axmen.com",
              "phone": "406-610-2520",
              "is_lps": false,          # can we live-transfer?
              "specialties": [...],
            },
            ...
          ],
          "main_office": "406-728-7020"
        }

    Routing guidance for the agent:
      - match_count == 0  -> offer main office or lookup by town
      - match_count == 1  -> confirm with caller, then offer:
                              * live transfer if is_lps == true
                              * leave a message otherwise (via schedule_callback)
      - match_count >= 2  -> ask caller to clarify (first name only + last name)
    """
    ok, _raw, body = await read_and_verify(request)
    if not ok:
        return unauthorized_response()
    try:
        # Defensive arg parsing: Retell sends function arguments as the top-level
        # body (with an `execution_message` field alongside), NOT wrapped in an
        # `arguments` key. Fall back to both to be robust against either format.
        if "arguments" in body and isinstance(body["arguments"], dict):
            args = body["arguments"]
        else:
            args = body
        name_query = (args.get("name") or "").strip()

        # Always log the raw body at INFO so we can diagnose future failures
        # without needing to re-reproduce the exact call.
        logger.info(f"[LOOKUP_STAFF_BY_NAME] raw body keys: {list(body.keys())}, name='{name_query}'")

        if not name_query:
            logger.warning(f"[LOOKUP_STAFF_BY_NAME] empty name_query, body={body}")
            return JSONResponse(content={
                "result": "I need a name to search for. Who are you trying to reach?",
                "success": False,
                "match_count": 0,
                "matches": [],
                "main_office": MFC_MAIN_OFFICE_PHONE,
            })

        # First pass: try the exact query as given
        matches = await lookup_staff_by_name(name_query)

        # Fallback: if a multi-word query returns zero, the ASR probably mis-heard
        # part of the name (e.g. "Cheryl Shea" instead of "Sheryl Shea"). Retry
        # with each token individually and merge results. This is forgiving of
        # partial matches without losing correctness — if both tokens happened to
        # match different people, we return both and the agent asks to clarify.
        if not matches:
            tokens = [t.strip() for t in name_query.split() if len(t.strip()) >= 3]
            if len(tokens) >= 2:
                logger.info(f"[LOOKUP_STAFF_BY_NAME] zero matches for '{name_query}', retrying tokens: {tokens}")
                seen_ids = set()
                merged = []
                for tok in tokens:
                    for m in await lookup_staff_by_name(tok):
                        if m.get("id") not in seen_ids:
                            seen_ids.add(m.get("id"))
                            merged.append(m)
                matches = merged
                logger.info(f"[LOOKUP_STAFF_BY_NAME] token fallback found {len(matches)} match(es)")

        # Trim / sanitize for the voice agent — don't ship phone/email in the
        # spoken summary by default, but DO include them in the structured data
        # so the agent can act on them.
        cleaned = []
        for m in matches:
            cleaned.append({
                "id": m.get("id"),
                "full_name": m.get("full_name"),
                "role": m.get("role"),
                "email": m.get("email"),
                "phone": m.get("phone"),
                "is_lps": bool(m.get("is_lps")),
                "specialties": m.get("specialties") or [],
            })

        count = len(cleaned)
        if count == 0:
            spoken = (
                f"I can't find anyone matching '{name_query}' in our directory. "
                f"Would you like me to connect you with our main office at "
                f"{MFC_MAIN_OFFICE_PHONE}?"
            )
        elif count == 1:
            m = cleaned[0]
            if m["is_lps"]:
                spoken = (
                    f"I found {m['full_name']}, {m['role']}. "
                    f"Would you like me to connect you, or take a message?"
                )
            else:
                role_phrase = f"from our {m['role']} team" if m['role'] else "on our team"
                spoken = (
                    f"I found {m['full_name']} {role_phrase}. "
                    f"I can take a message and email it to them right now — "
                    f"would you like to leave one?"
                )
        else:
            names = ", ".join(m["full_name"] for m in cleaned[:4])
            spoken = (
                f"I found {count} people matching '{name_query}': {names}. "
                f"Which one are you trying to reach?"
            )

        return JSONResponse(content={
            "result": spoken,
            "success": count > 0,
            "match_count": count,
            "matches": cleaned,
            "main_office": MFC_MAIN_OFFICE_PHONE,
        })
    except Exception as e:
        logger.error(f"[LOOKUP_STAFF_BY_NAME] Error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/retell/functions/transfer_call_tool")
async def transfer_call_tool(request: Request):
    """Transfer call to specialist's phone number."""
    ok, _raw, body = await read_and_verify(request)
    if not ok:
        return unauthorized_response()
    try:
        call_data = body.get("call", {})
        from_number = call_data.get("from_number", "")
        
        is_widget = not from_number
        caller_key = from_number or f"widget_{call_data.get('call_id', '')}"
        logger.info(f"[TRANSFER] Transfer requested for caller: {redact_phone(caller_key)}")

        # Try cache first for caller info, then fall back to Zep
        cached = _cache_get(caller_key)
        if cached is not None:
            memory_data = cached
            logger.info(f"[TRANSFER] [CACHE HIT] Using cached data")
        elif not is_widget:
            memory_data = await lookup_caller_fast(from_number)
        else:
            memory_data = {"caller_location": None, "caller_specialist": None}
        
        caller_location = memory_data.get("caller_location")
        specialist_name = memory_data.get("caller_specialist")
        
        logger.info(f"[TRANSFER] Caller location: {caller_location}, Specialist: {specialist_name}")
        
        specialist = await lookup_specialist_by_town(caller_location or "")

        # Refuse to live-transfer non-LPS staff (managers, operations, warehouse).
        # The agent's prompt covers this for name-based lookups, but the
        # transfer tool itself is the last line of defense — if a Missoula
        # caller is routed to Sheryl Shea here and we'd happily dial her
        # number, she'd get a live call she's not staffed to take.
        if specialist and not specialist.get("is_lps"):
            logger.warning(
                f"[TRANSFER] REFUSED — {specialist.get('specialist_name')} "
                f"(role={specialist.get('role')}) is not an LPS. "
                f"Agent should take a message via schedule_callback instead."
            )
            return JSONResponse(content={
                "phone_number": MFC_MAIN_OFFICE_E164,
                "specialist_name": "main office",
                "success": False,
                "reason": "non_lps_specialist",
                "specialist_id": specialist.get("id"),
                "specialist_name_assigned": specialist.get("specialist_name"),
                "specialist_email": specialist.get("specialist_email"),
                "hint": (
                    f"{specialist.get('specialist_name')} doesn't take live calls. "
                    f"Use schedule_callback with reason='message' to leave a note instead."
                ),
            })

        if specialist and specialist.get("specialist_phone"):
            phone_number = specialist["specialist_phone"]
            specialist_name = specialist.get("specialist_name", "your specialist")

            logger.info(f"[TRANSFER] Transferring to {specialist_name} at {phone_number}")

            return JSONResponse(content={
                "phone_number": phone_number,
                "specialist_name": specialist_name,
                "success": True
            })
        else:
            logger.warning(f"[TRANSFER] No specialist found for location: {caller_location}")
            return JSONResponse(content={
                "phone_number": MFC_MAIN_OFFICE_E164,
                "specialist_name": "main office",
                "success": True
            })
        
    except Exception as e:
        logger.error(f"[TRANSFER] Error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
