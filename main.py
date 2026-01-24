"""
Montana Feed Company - Retell AI Webhook with Zep Memory Integration
Version 3.0.0 - MODULAR REFACTOR
- Danielle Peterson retired, Taylor took her territory
- Isabell covers Western MT (Missoula area)
- All performance improvements included
"""

from datetime import datetime

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
        "version": "3.0.0",
        "lps_count": 7,
        "memory_enabled": bool(ZEP_API_KEY),
        "supabase_enabled": supabase is not None,
        "persistent_client": get_zep_client() is not None,
        "timestamp": datetime.utcnow().isoformat()
    }


@app.post("/retell-inbound-webhook")
async def retell_inbound_webhook(request: Request):
    """Inbound webhook - sets dynamic variables with full memory context."""
    try:
        body = await request.json()
        event = body.get("event")

        logger.info(f"=== INBOUND WEBHOOK ===")
        logger.info(f"Event: {event}")

        if event == "call_inbound":
            call_inbound = body.get("call_inbound", {})
            from_number = call_inbound.get("from_number", "")
            to_number = call_inbound.get("to_number", "")
            agent_id = call_inbound.get("agent_id", "")

            logger.info(f"Inbound: {from_number} -> {to_number} (agent: {agent_id})")

            if not from_number:
                logger.warning("No from_number - returning empty")
                return JSONResponse(content={"call_inbound": {}})

            memory_data = await lookup_caller_fast(from_number)
            caller_name = memory_data.get("caller_name")
            caller_location = memory_data.get("caller_location")
            caller_specialist = memory_data.get("caller_specialist")
            conversation_history = memory_data.get("conversation_history", "")

            # Always include all variables as strings (no None values)
            dynamic_vars = {
                "caller_name": caller_name if caller_name else "New caller",
                "is_returning": "true" if caller_name else "false",
                "conversation_history": conversation_history or "",
                "caller_location": caller_location or "",
                "caller_specialist": caller_specialist or "",
            }

            logger.info(f"[INBOUND] Dynamic vars: name={dynamic_vars['caller_name']}, "
                       f"location={dynamic_vars['caller_location'] or 'None'}, "
                       f"specialist={dynamic_vars['caller_specialist'] or 'None'}")
            
            if conversation_history:
                logger.info(f"[INBOUND] Context: {conversation_history[:100]}")

            response = {
                "call_inbound": {
                    "dynamic_variables": dynamic_vars
                }
            }

            return JSONResponse(content=response)

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


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
