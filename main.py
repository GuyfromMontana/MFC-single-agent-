"""
Montana Feed Company - Retell AI Webhook with Zep Memory Integration
Version 3.0.9 - WIDGET CALL SUPPORT
- Widget calls (no phone number) now handled gracefully with call_id fallback
- Widget conversations saved to Supabase (skips Zep which needs phone-based IDs)
- Cache, transfer, and agent webhook all support widget callers
- Previous: v3.0.8 call cache + email by name
"""

from datetime import datetime
import os
import httpx
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Import configuration and clients
from config import (
    supabase,
    ZEP_API_KEY,
    ZEP_BASE_URL,
    ZEP_HEADERS,
    get_zep_client,
    normalize_phone,
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
)

# Main office fallback number for the voice agent. Single source of truth —
# used by lookup_staff, lookup_staff_by_name, and schedule_callback when a
# request can't be routed to a specific person.
MFC_MAIN_OFFICE_PHONE = "406-728-7020"

# ============================================================================
# CALL CACHE - Store Zep lookups from call_started for reuse at call_ended
# ============================================================================
# Keyed by phone number, stores memory_data dict
# Cleaned up after call_ended processing completes

_call_cache: dict[str, dict] = {}

# ============================================================================
# EMAIL CONFIGURATION
# ============================================================================

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "notifications@montanafeed.com")

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
        
        # Format the email
        subject = f"New Call from {caller_name or caller_phone}"
        
        html_content = f"""
        <h2>New Call Received</h2>
        <p><strong>Specialist:</strong> {specialist_name}</p>
        <hr>
        <p><strong>Caller:</strong> {caller_name or 'Unknown'}</p>
        <p><strong>Phone:</strong> {caller_phone}</p>
        <p><strong>Location:</strong> {caller_location or 'Not specified'}</p>
        <p><strong>Duration:</strong> {duration_str}</p>
        <p><strong>Time:</strong> {datetime.now().strftime('%Y-%m-%d %I:%M %p MT')}</p>
        <hr>
        <h3>Conversation:</h3>
        <p style="white-space: pre-wrap; font-family: monospace; background: #f5f5f5; padding: 10px; border-radius: 5px;">{call_summary or 'No transcript available'}</p>
        <hr>
        <p><small>This is an automated notification from Montana Feed Company voice system.</small></p>
        """
        
        # Send via Resend API
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "from": FROM_EMAIL,
                    "to": [specialist_email],
                    "subject": subject,
                    "html": html_content
                },
                timeout=10.0
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
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "montana-feed-retell-webhook",
        "version": "3.0.9",
        "lps_count": 7,
        "memory_enabled": bool(ZEP_API_KEY),
        "supabase_enabled": supabase is not None,
        "email_enabled": bool(RESEND_API_KEY),
        "persistent_client": get_zep_client() is not None,
        "active_calls_cached": len(_call_cache),
        "timestamp": datetime.utcnow().isoformat()
    }


@app.post("/retell-inbound-webhook")
async def retell_inbound_webhook(request: Request):
    """Inbound webhook - handles call_started and call_ended events."""
    try:
        body = await request.json()
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

            logger.info(f"Inbound: {caller_key} -> {to_number} (agent: {agent_id}, {'widget' if is_widget else 'phone'})")

            # Check if we already cached this caller (call_inbound fires before call_started)
            if caller_key in _call_cache:
                memory_data = _call_cache[caller_key]
                logger.info(f"[CACHE HIT] Using cached Zep data for {caller_key}")
            elif is_widget:
                # Widget caller — no Zep history possible, return new caller defaults
                memory_data = {
                    "found": False, "user_id": f"widget_{call_id}",
                    "caller_name": None, "caller_location": None,
                    "caller_specialist": None, "conversation_history": "",
                    "message": "Widget caller"
                }
                _call_cache[caller_key] = memory_data
                logger.info(f"[WIDGET] New widget caller, cached as {caller_key}")
            else:
                # First event for this call - do the Zep lookup and cache it
                memory_data = await lookup_caller_fast(caller_key)
                _call_cache[caller_key] = memory_data
                logger.info(f"[CACHE MISS] Looked up Zep and cached for {caller_key}")

            caller_name = memory_data.get("caller_name")
            caller_location = memory_data.get("caller_location")
            caller_specialist = memory_data.get("caller_specialist")
            conversation_history = memory_data.get("conversation_history", "")

            # Always include all variables as strings (no None values)
            dynamic_vars = {
                "name": caller_name if caller_name else "New caller",
                "is_returning": "true" if caller_name else "false",
                "conversation_history": conversation_history or "",
                "location": caller_location or "",
                "specialist": caller_specialist or "",
            }

            logger.info(f"[INBOUND] Dynamic vars: name={dynamic_vars['name']}, "
                       f"location={dynamic_vars['location'] or 'None'}, "
                       f"specialist={dynamic_vars['specialist'] or 'None'}")
            
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
                start_datetime = datetime.fromtimestamp(start_time / 1000)
                end_datetime = datetime.fromtimestamp(end_time / 1000)
            
            # Widget calls have no from_number — use call_id as fallback
            is_widget = not from_number
            caller_key = from_number or f"widget_{call_id}"

            logger.info(f"[CALL_ENDED] {caller_key} ({'widget' if is_widget else 'phone'}), duration: {duration_seconds}s")

            # Use cached memory data if available, otherwise fall back to Zep
            if caller_key in _call_cache:
                memory_data = _call_cache[caller_key]
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
                        "created_at": datetime.utcnow().isoformat(),
                        "updated_at": datetime.utcnow().isoformat()
                    }
                    
                    conversation_result = supabase.table("conversations").insert(conversation_data).execute()
                    
                    if conversation_result.data and len(conversation_result.data) > 0:
                        conversation_id = conversation_result.data[0]["id"]
                        logger.info(f"✅ Created conversation: {conversation_id}")
                        
                        if transcript_object:
                            for msg in transcript_object:
                                message_data = {
                                    "id": str(uuid.uuid4()),
                                    "conversation_id": conversation_id,
                                    "content": msg.get("content", ""),
                                    "sender": "user" if msg.get("role") == "user" else "assistant",
                                    "message_type": "voice",
                                    "created_at": datetime.utcnow().isoformat()
                                }
                                supabase.table("conversation_messages").insert(message_data).execute()
                            
                            logger.info(f"✅ Saved {len(transcript_object)} messages to conversation_messages")
                    
                except Exception as e:
                    logger.error(f"❌ Failed to save to Supabase: {e}", exc_info=True)

            # ====================================================================
            # SEND EMAIL TO SPECIALIST (lookup by name, not town)
            # ====================================================================
            if specialist_name and RESEND_API_KEY:
                specialist_email = None
                
                if supabase:
                    try:
                        name_parts = specialist_name.split(None, 1)
                        first_name = name_parts[0]
                        last_name = name_parts[1] if len(name_parts) > 1 else ""
                        
                        logger.info(f"[EMAIL] Looking up email for: {first_name} {last_name}")
                        
                        result = supabase.table("specialists")\
                            .select("email, first_name, last_name")\
                            .ilike("first_name", first_name)\
                            .ilike("last_name", last_name)\
                            .eq("is_active", True)\
                            .execute()
                        
                        if result.data and len(result.data) > 0:
                            specialist_email = result.data[0].get("email")
                            logger.info(f"[EMAIL] Found email: {specialist_email}")
                        else:
                            logger.warning(f"[EMAIL] No specialist found matching: {first_name} {last_name}")
                    
                    except Exception as e:
                        logger.error(f"[EMAIL] Specialist lookup error: {e}")
                
                if specialist_email:
                    await send_specialist_email(
                        specialist_email=specialist_email,
                        specialist_name=specialist_name,
                        caller_name=caller_name or "Unknown Caller",
                        caller_phone=from_number,
                        caller_location=caller_location or "Unknown",
                        call_summary=call_summary or "No transcript available",
                        duration=duration_seconds
                    )
                else:
                    logger.warning(f"[EMAIL] No email found for specialist: {specialist_name}")
            else:
                if not specialist_name:
                    logger.warning("[EMAIL] No specialist assigned to caller")
                if not RESEND_API_KEY:
                    logger.warning("[EMAIL] RESEND_API_KEY not configured")

            # Clean up cache for this caller
            _call_cache.pop(caller_key, None)
            logger.info(f"[CACHE] Cleaned up cache for {caller_key}")

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
    try:
        body = await request.json()
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
                if caller_key in _call_cache:
                    caller_name = _call_cache[caller_key].get("caller_name")
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
    """Fix Zep user data."""
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
            update_lead_with_name(phone, name_parts[0], name_parts[1] if len(name_parts) > 1 else "")
            return {"success": True, "message": f"Updated {user_id} to {name}"}
        else:
            return {"success": False, "error": response.text}

    except Exception as e:
        return {"error": str(e)}


@app.post("/set-user-location")
async def set_user_location(request: Request):
    """Set user location safely by merging metadata."""
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


# ============================================================================
# FUNCTION ENDPOINTS (Called directly by Retell)
# ============================================================================

@app.post("/retell/functions/lookup_town")
async def lookup_town(request: Request):
    """Look up specialist by town and save to Zep metadata."""
    try:
        body = await request.json()
        args = body.get("arguments", {})
        town = args.get("town", "") or args.get("location", "") or args.get("city", "")
        
        call_data = body.get("call", {})
        phone = call_data.get("from_number", "")

        logger.info(f"[LOOKUP_TOWN] Searching for: '{town}'")

        specialist = lookup_specialist_by_town(town)

        if specialist and phone:
            user_id = f"caller_{normalize_phone(phone)}"
            await zep_update_user_metadata(user_id, {
                "specialist": specialist["specialist_name"],
                "location": specialist.get("territory", town)
            })

            result = f"{specialist['specialist_name']} handles {town}. Reach them at {specialist['specialist_phone']}."
            logger.info(f"[LOOKUP_TOWN] Found: {specialist['specialist_name']}, saved to Zep")
        else:
            result = f"No specialist found for {town}. Contact our main office at 406-883-4290."
            logger.info(f"[LOOKUP_TOWN] No match for '{town}'")

        return JSONResponse(content={"result": result, "success": bool(specialist)})
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
    try:
        body = await request.json()
        args = body.get("arguments", {})
        call_data = body.get("call", {}) or {}

        caller_name = args.get("caller_name") or args.get("name", "")
        caller_phone = args.get("phone") or call_data.get("from_number", "")
        reason = (args.get("reason") or "callback").strip().lower()
        callback_time = args.get("callback_time", "")
        message_content = args.get("message_content") or args.get("notes", "")

        specialist_id = args.get("specialist_id")
        specialist_name = args.get("specialist_name")
        specialist_email = args.get("specialist_email")

        # Compose the notes field: either the message body or the callback timeframe
        if reason == "message" and message_content:
            notes = message_content
        elif callback_time:
            notes = f"Requested callback time: {callback_time}"
            if message_content:
                notes += f"\n\n{message_content}"
        else:
            notes = message_content or "(no details provided)"

        # Write to callbacks table via the skill function
        callback_id = create_message_for_specialist(
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
            capture_lead(caller_name, caller_phone, "callback", notes[:500])
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
        elif callback_time and specialist_name:
            spoken = (
                f"Scheduled a callback from {specialist_name} for {callback_time}."
            )
        elif callback_time:
            spoken = f"Scheduled your callback for {callback_time}."
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
    """Create a new lead record."""
    try:
        body = await request.json()
        args = body.get("arguments", {})
        name = args.get("name", "")
        phone_num = args.get("phone", body.get("call", {}).get("from_number", ""))

        success = capture_lead(name, phone_num, args.get("location", ""), args.get("interests", ""))
        result = f"Saved your info, {name}." if success else "Noted your information."

        return JSONResponse(content={"result": result, "success": success})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/retell/functions/search_knowledge_base")
async def search_knowledge_base_endpoint(request: Request):
    """Search the knowledge base for relevant information."""
    try:
        body = await request.json()
        query = body.get("arguments", {}).get("query", "")
        return JSONResponse(content={"result": search_knowledge_base(query), "success": True})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/retell/functions/end_call")
async def end_call(request: Request):
    """End the call gracefully."""
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
    try:
        body = await request.json()
        location = body.get("arguments", {}).get("location", "")
        phone = body.get("call", {}).get("from_number", "")

        specialist = lookup_specialist_by_town(location)

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
    try:
        body = await request.json()
        args = body.get("arguments", {})
        name_query = (args.get("name") or "").strip()

        if not name_query:
            return JSONResponse(content={
                "result": "I need a name to search for. Who are you trying to reach?",
                "success": False,
                "match_count": 0,
                "matches": [],
                "main_office": MFC_MAIN_OFFICE_PHONE,
            })

        matches = lookup_staff_by_name(name_query)

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
                    f"I found {m['full_name']}, your {m['role']}. "
                    f"Would you like me to connect you, or take a message?"
                )
            else:
                spoken = (
                    f"I found {m['full_name']} in {m['role'] or 'our team'}. "
                    f"They handle calls by message — I can take a message and "
                    f"email it to them right now if you'd like."
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
    try:
        body = await request.json()
        call_data = body.get("call", {})
        from_number = call_data.get("from_number", "")
        
        is_widget = not from_number
        caller_key = from_number or f"widget_{call_data.get('call_id', '')}"
        logger.info(f"[TRANSFER] Transfer requested for caller: {caller_key}")

        # Try cache first for caller info, then fall back to Zep
        if caller_key in _call_cache:
            memory_data = _call_cache[caller_key]
            logger.info(f"[TRANSFER] [CACHE HIT] Using cached data")
        elif not is_widget:
            memory_data = await lookup_caller_fast(from_number)
        else:
            memory_data = {"caller_location": None, "caller_specialist": None}
        
        caller_location = memory_data.get("caller_location")
        specialist_name = memory_data.get("caller_specialist")
        
        logger.info(f"[TRANSFER] Caller location: {caller_location}, Specialist: {specialist_name}")
        
        specialist = lookup_specialist_by_town(caller_location or "")
        
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
                "phone_number": "+14068834290",
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
