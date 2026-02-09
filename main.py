"""
Montana Feed Company - Retell AI Webhook with Zep Memory Integration
Version 3.0.7 - FIXED EMAIL LOOKUP BY SPECIALIST NAME
- Fixed: Email lookup now queries specialists table by name (not by town)
- This ensures emails are sent even when location data is garbled
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
    # Knowledge
    search_knowledge_base,
    # Leads
    capture_lead,
    update_lead_with_name,
)

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
        "version": "3.0.7",
        "lps_count": 7,
        "memory_enabled": bool(ZEP_API_KEY),
        "supabase_enabled": supabase is not None,
        "email_enabled": bool(RESEND_API_KEY),
        "persistent_client": get_zep_client() is not None,
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

            logger.info(f"Inbound: {from_number} -> {to_number} (agent: {agent_id})")

            if not from_number:
                logger.warning("No from_number - returning empty")
                return JSONResponse(content={"call_inbound": {}})

            # Look up caller in memory
            memory_data = await lookup_caller_fast(from_number)
            caller_name = memory_data.get("caller_name")
            caller_location = memory_data.get("caller_location")
            caller_specialist = memory_data.get("caller_specialist")
            conversation_history = memory_data.get("conversation_history", "")

            # Always include all variables as strings (no None values)
            # Variable names must match {{name}}, {{location}}, etc. in system prompt
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

            # Return response with dynamic variables in correct format for inbound webhook
            response = {
                "call_inbound": {
                    "dynamic_variables": dynamic_vars
                }
            }

            return JSONResponse(content=response)

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
                duration_seconds = int((end_time - start_time) / 1000)  # Convert ms to seconds
                start_datetime = datetime.fromtimestamp(start_time / 1000)
                end_datetime = datetime.fromtimestamp(end_time / 1000)
            
            logger.info(f"[CALL_ENDED] {from_number}, duration: {duration_seconds}s")

            if not from_number:
                logger.warning("No from_number in call_ended")
                return JSONResponse(content={})

            # Look up caller info from memory
            memory_data = await lookup_caller_fast(from_number)
            caller_name = memory_data.get("caller_name")
            caller_location = memory_data.get("caller_location")
            specialist_name = memory_data.get("caller_specialist")

            logger.info(f"[MEMORY] Name: {caller_name or 'Unknown'}")
            logger.info(f"[MEMORY] Location: {caller_location or 'Unknown'}")
            logger.info(f"[MEMORY] Specialist: {specialist_name or 'Unknown'}")

            # Save transcript to Zep if available
            if transcript_object and len(transcript_object) > 0:
                logger.info(f"[SAVE] Saving {len(transcript_object)} messages to Zep")
                await save_call_to_zep(from_number, transcript_object, call_id, caller_name)

            # Create a formatted summary from transcript
            call_summary = ""
            if transcript_object:
                # Build formatted conversation
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
            # SAVE TO SUPABASE - conversations and conversation_messages tables
            # ====================================================================
            conversation_id = None
            
            if supabase:
                try:
                    # 1. Create conversation record
                    conversation_data = {
                        "id": str(uuid.uuid4()),
                        "phone_number": from_number,
                        "conversation_type": "voice_call",
                        "direction": "inbound",
                        "status": "completed",
                        "start_time": start_datetime.isoformat() if start_datetime else None,
                        "end_time": end_datetime.isoformat() if end_datetime else None,
                        "duration_seconds": duration_seconds,
                        "vapi_call_id": call_id,  # Store Retell call_id here
                        "ai_summary": call_summary[:500] if call_summary else None,  # Short summary
                        "created_at": datetime.utcnow().isoformat(),
                        "updated_at": datetime.utcnow().isoformat()
                    }
                    
                    conversation_result = supabase.table("conversations").insert(conversation_data).execute()
                    
                    if conversation_result.data and len(conversation_result.data) > 0:
                        conversation_id = conversation_result.data[0]["id"]
                        logger.info(f"✅ Created conversation: {conversation_id}")
                        
                        # 2. Save individual messages to conversation_messages
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
            # SEND EMAIL TO SPECIALIST (v3.0.7 FIX: lookup by name, not town)
            # ====================================================================
            if specialist_name and RESEND_API_KEY:
                specialist_email = None
                
                # Look up specialist email by NAME directly from specialists table
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

        # Only handle call_ended for Zep saving and analytics
        if event_type == "call_ended" and transcript and phone:
            logger.info(f"[SAVE] Saving {len(transcript)} messages to Zep")

            caller_name = body.get("retell_llm_dynamic_variables", {}).get("caller_name")
            if not caller_name or caller_name == "New caller":
                memory_data = await lookup_caller_fast(phone)
                caller_name = memory_data.get("caller_name")

            save_result = await save_call_to_zep(phone, transcript, call_id, caller_name)

            if save_result.get("extracted_name"):
                logger.info(f"[SAVE] Name extracted: {save_result['extracted_name']}")
            if save_result.get("extracted_location"):
                logger.info(f"[SAVE] Location extracted: {save_result['extracted_location']}")

            return JSONResponse(content={
                "call_id": call_id,
                "memory_saved": save_result.get("success", False)
            })

        # For other events, just acknowledge
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
        
        # Use the safe metadata update function
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
        
        # Get caller phone for metadata update
        call_data = body.get("call", {})
        phone = call_data.get("from_number", "")

        logger.info(f"[LOOKUP_TOWN] Searching for: '{town}'")

        specialist = lookup_specialist_by_town(town)

        if specialist and phone:
            # Save specialist to Zep for future calls
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
    """Schedule a callback for a caller."""
    try:
        body = await request.json()
        args = body.get("arguments", {})
        name = args.get("name", "")
        phone_num = args.get("phone", body.get("call", {}).get("from_number", ""))
        callback_time = args.get("callback_time", "")

        success = capture_lead(name, phone_num, "callback", f"Callback: {callback_time}")
        result = f"Scheduled callback for {callback_time}." if success else "Noted your request."

        return JSONResponse(content={"result": result, "success": success})
    except Exception as e:
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
    """Look up specialist by location and save to Zep."""
    try:
        body = await request.json()
        location = body.get("arguments", {}).get("location", "")
        phone = body.get("call", {}).get("from_number", "")
        
        specialist = lookup_specialist_by_town(location)

        if specialist and phone:
            # Save to Zep
            user_id = f"caller_{normalize_phone(phone)}"
            await zep_update_user_metadata(user_id, {
                "specialist": specialist["specialist_name"],
                "location": specialist.get("territory", location)
            })
            result = f"Your specialist is {specialist['specialist_name']} at {specialist['specialist_phone']}."
        else:
            result = "Let me connect you with our main office at 406-883-4290."

        return JSONResponse(content={"result": result, "success": bool(specialist)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/retell/functions/transfer_call_tool")
async def transfer_call_tool(request: Request):
    """Transfer call to specialist's phone number - used by Retell's Dynamic Routing."""
    try:
        body = await request.json()
        call_data = body.get("call", {})
        from_number = call_data.get("from_number", "")
        
        logger.info(f"[TRANSFER] Transfer requested for caller: {from_number}")
        
        # Look up caller's location and specialist from memory
        memory_data = await lookup_caller_fast(from_number)
        caller_location = memory_data.get("caller_location")
        specialist_name = memory_data.get("caller_specialist")
        
        logger.info(f"[TRANSFER] Caller location: {caller_location}, Specialist: {specialist_name}")
        
        # Look up specialist details
        specialist = lookup_specialist_by_town(caller_location or "")
        
        if specialist and specialist.get("specialist_phone"):
            phone_number = specialist["specialist_phone"]
            specialist_name = specialist.get("specialist_name", "your specialist")
            
            logger.info(f"[TRANSFER] Transferring to {specialist_name} at {phone_number}")
            
            # Return phone number for Retell to transfer to
            return JSONResponse(content={
                "phone_number": phone_number,
                "specialist_name": specialist_name,
                "success": True
            })
        else:
            logger.warning(f"[TRANSFER] No specialist found for location: {caller_location}")
            # Fallback to main office
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
