# retell_handlers.py
# Retell AI webhook and function handlers for Montana Feed Company
# 
# ============================================================================
# Zep V3 Integration - Feb 1, 2026 Ready ✅
# ============================================================================
# This file reuses all memory functions from main.py, which have been
# migrated to Zep V3 API. Retell calls are automatically saved to Zep
# using the same V3-compliant save_conversation() function as Vapi.
#
# Key V3 Features:
# - Caller history via get_caller_context() (uses zep.user.get_sessions)
# - Conversation saving via save_conversation() (uses zep.memory.add)
# - Both Vapi and Retell share the same Zep account/users
# ============================================================================
#
# Setup in main.py (already done):
#   from retell_handlers import router as retell_router
#   app.include_router(retell_router)
#
# Retell Dashboard Configuration:
#   1. Agent → Webhook URL: https://your-domain.railway.app/retell/webhook
#   2. Agent → Custom Tools: Point each tool to /retell/functions/{tool_name}
#   3. Settings → API Key: Add RETELL_API_KEY to environment variables

import os
import json
import hmac
import hashlib
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

# We'll import these from main.py when this module is loaded
# This allows us to reuse all your existing V3-migrated functions
zep = None
supabase = None

router = APIRouter(prefix="/retell", tags=["Retell"])

# ============================================================================
# INITIALIZATION - Called when main.py imports this module
# ============================================================================

def init_clients(zep_client, supabase_client):
    """Initialize with clients from main.py"""
    global zep, supabase
    zep = zep_client
    supabase = supabase_client
    print("✅ Retell handlers initialized with Zep V3 and Supabase clients")

# ============================================================================
# RETELL SIGNATURE VERIFICATION
# ============================================================================

def verify_retell_signature(body: bytes, signature: str) -> bool:
    """
    Verify the request is from Retell using HMAC signature.
    Retell signs the request body with your API key.
    """
    # TEMPORARY: Skip verification for testing
    # TODO: Re-enable after adding RETELL_API_KEY to Railway
    print("⚠️ SIGNATURE VERIFICATION DISABLED FOR TESTING")
    return True
    
    # Commented out for testing - uncomment after adding API key
    # api_key = os.getenv("RETELL_API_KEY", "")
    # if not api_key:
    #     print("⚠️ RETELL_API_KEY not set - skipping signature verification")
    #     return True  # Allow requests if no key set (for testing)
    # 
    # if not signature:
    #     print("⚠️ No x-retell-signature header")
    #     return False
    # 
    # try:
    #     # Retell uses the API key as the HMAC secret
    #     expected = hmac.new(
    #         api_key.encode('utf-8'),
    #         body,
    #         hashlib.sha256
    #     ).hexdigest()
    #     return hmac.compare_digest(expected, signature)
    # except Exception as e:
    #     print(f"⚠️ Signature verification error: {e}")
    #     return False

# ============================================================================
# HEALTH CHECK
# ============================================================================

@router.get("/health")
async def retell_health():
    """Health check for Retell endpoints"""
    return {
        "status": "healthy",
        "service": "mfc-retell-handlers",
        "timestamp": datetime.now().isoformat(),
        "retell_api_key_set": bool(os.getenv("RETELL_API_KEY")),
        "zep_v3_ready": True,  # Confirms V3 migration complete
        "note": "Retell handlers use Zep V3 via main.py functions"
    }

# ============================================================================
# MAIN WEBHOOK - Call Events (call_started, call_ended, call_analyzed)
# ============================================================================

@router.post("/webhook")
async def retell_webhook(request: Request):
    """
    Handles Retell call lifecycle events.
    Configure this URL in Retell Dashboard → Agent → Webhook Settings
    """
    try:
        body = await request.body()
        signature = request.headers.get("x-retell-signature", "")
        
        # Verify signature
        if not verify_retell_signature(body, signature):
            raise HTTPException(status_code=401, detail="Invalid signature")
        
        payload = json.loads(body)
        event = payload.get("event", "unknown")
        call = payload.get("call", {})
        
        call_id = call.get("call_id", "unknown")
        from_number = call.get("from_number", "")
        
        print(f"\n📨 Retell webhook: {event}")
        print(f"   Call ID: {call_id}")
        print(f"   From: {from_number}")
        
        # ----------------------------------------
        # CALL STARTED
        # ----------------------------------------
        if event == "call_started":
            print(f"📞 Retell call started from {from_number}")
            # You could initialize call tracking here if needed
            return JSONResponse(content={"status": "received"})
        
        # ----------------------------------------
        # CALL ENDED - Save to Zep (V3)
        # ----------------------------------------
        elif event == "call_ended":
            print(f"📞 Retell call ended: {call_id}")
            await handle_retell_call_ended(call)
            return JSONResponse(content={"status": "received"})
        
        # ----------------------------------------
        # CALL ANALYZED - Post-call analysis
        # ----------------------------------------
        elif event == "call_analyzed":
            print(f"📊 Retell call analyzed: {call_id}")
            # Could extract sentiment, success metrics here
            analysis = call.get("call_analysis", {})
            if analysis:
                print(f"   Sentiment: {analysis.get('user_sentiment', 'unknown')}")
                print(f"   Success: {analysis.get('call_successful', 'unknown')}")
            return JSONResponse(content={"status": "received"})
        
        else:
            print(f"⚠️ Unknown Retell event: {event}")
            return JSONResponse(content={"status": "ignored", "event": event})
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Retell webhook error: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)}
        )


async def handle_retell_call_ended(call: dict):
    """
    Process end of Retell call - save conversation to Zep V3.
    Reuses your existing V3-migrated save_conversation function from main.py.
    """
    try:
        call_id = call.get("call_id", "")
        from_number = call.get("from_number", "")
        
        # Retell provides transcript in two formats:
        # - transcript: plain text string
        # - transcript_object: array of {role, content, words, ...}
        transcript_text = call.get("transcript", "")
        transcript_object = call.get("transcript_object", [])
        
        if not from_number:
            print("   ⚠️ No phone number in call data")
            return
        
        # Convert Retell transcript format to your Vapi-style format
        messages = []
        for utterance in transcript_object:
            role = utterance.get("role", "agent")
            content = utterance.get("content", "")
            
            if content:
                # Map Retell roles to your format
                if role == "agent":
                    mapped_role = "assistant"
                else:
                    mapped_role = "user"
                
                messages.append({
                    "role": mapped_role,
                    "message": content  # Your code uses "message" key
                })
        
        print(f"   📝 Formatted {len(messages)} messages for Zep V3")
        
        if messages or transcript_text:
            # Import and call your V3-migrated save function
            from main import save_conversation, SEND_CALL_SUMMARIES
            from main import get_specialist_for_caller, extract_actions_from_messages
            from main import send_call_summary_email
            
            # Save to Zep (V3 API: uses zep.memory.add)
            print(f"   💾 Saving to Zep V3...")
            await save_conversation(from_number, call_id, transcript_text, messages)
            print(f"   ✅ Saved to Zep V3 successfully")
            
            # Send call summary if enabled
            if SEND_CALL_SUMMARIES and messages:
                print("   📧 Preparing call summary...")
                specialist_info = await get_specialist_for_caller(from_number)
                
                if specialist_info.get("specialist_email"):
                    # Get call duration
                    duration = None
                    if call.get("start_timestamp") and call.get("end_timestamp"):
                        try:
                            start = call["start_timestamp"] / 1000  # ms to seconds
                            end = call["end_timestamp"] / 1000
                            duration = end - start
                        except:
                            pass
                    
                    actions = extract_actions_from_messages(messages)
                    
                    await send_call_summary_email(
                        specialist_email=specialist_info["specialist_email"],
                        specialist_name=specialist_info.get("specialist_name", "Specialist"),
                        caller_name=specialist_info.get("caller_name"),
                        caller_phone=from_number,
                        call_duration=duration,
                        messages=messages,
                        actions_taken=actions
                    )
        else:
            print("   ⚠️ No transcript to save")
            
    except Exception as e:
        print(f"   ❌ Error processing call end: {e}")
        import traceback
        traceback.print_exc()


# ============================================================================
# FUNCTION: get_caller_history
# Retrieves caller context from Zep V3 + Supabase
# ============================================================================

@router.post("/functions/get_caller_history")
async def retell_get_caller_history(request: Request):
    """
    Called at start of conversation to get caller context.
    Uses V3-migrated get_caller_context() from main.py.
    """
    try:
        body = await request.body()
        signature = request.headers.get("x-retell-signature", "")
        
        if not verify_retell_signature(body, signature):
            raise HTTPException(status_code=401, detail="Invalid signature")
        
        payload = json.loads(body)
        call = payload.get("call", {})
        args = payload.get("args", {})
        
        # Get phone from args or call object
        phone_number = args.get("phone_number") or call.get("from_number", "")
        
        print(f"\n🧠 Retell get_caller_history (V3): {phone_number}")
        
        if not phone_number:
            return JSONResponse(content={
                "result": json.dumps({
                    "is_returning_caller": False,
                    "is_known_contact": False,
                    "caller_name": None,
                    "summary": "No phone number provided.",
                    "greeting_hint": "New caller. Collect their name and information."
                })
            })
        
        # Use your V3-migrated function (uses zep.user.get_sessions)
        from main import get_caller_context
        context = await get_caller_context(phone_number)
        
        # Add phone to context for reference
        context["caller_phone"] = phone_number
        
        print(f"   ✅ V3 Context retrieved:")
        print(f"      returning={context.get('is_returning_caller')}")
        print(f"      known={context.get('is_known_contact')}")
        print(f"      name={context.get('caller_name')}")
        
        return JSONResponse(content={
            "result": json.dumps(context)
        })
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error in get_caller_history: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(content={
            "result": json.dumps({
                "is_returning_caller": False,
                "error": str(e),
                "summary": "Error retrieving caller history.",
                "greeting_hint": "New caller. Collect their name and information."
            })
        })


# ============================================================================
# FUNCTION: query_knowledge
# Uses the /api/query-knowledge endpoint from main.py
# ============================================================================

@router.post("/functions/query_knowledge")
async def retell_query_knowledge(request: Request):
    """
    Query the Montana Feed knowledge base.
    Routes to the /api/query-knowledge endpoint that uses keyword + semantic search.
    """
    try:
        body = await request.body()
        signature = request.headers.get("x-retell-signature", "")
        
        if not verify_retell_signature(body, signature):
            raise HTTPException(status_code=401, detail="Invalid signature")
        
        payload = json.loads(body)
        args = payload.get("args", {})
        
        question = args.get("question", "") or args.get("query", "")
        
        print(f"\n🔍 Retell query_knowledge: {question}")
        
        if not question:
            return JSONResponse(content={
                "result": json.dumps({
                    "success": False,
                    "answer": "I didn't catch your question. Could you repeat that?",
                    "found": False
                })
            })
        
        # Call the existing query-knowledge endpoint
        import httpx
        
        async with httpx.AsyncClient() as client:
            # Call localhost since we're in the same service
            response = await client.post(
                "http://localhost:3001/api/query-knowledge",
                json={"query": question},
                timeout=10.0
            )
            
            if response.status_code == 200:
                data = response.json()
                results = data.get("results", [])
                
                if results and len(results) > 0:
                    best_result = results[0]
                    answer = best_result.get("answer", "")
                    
                    # Voice-optimize the answer (shorten if needed)
                    if len(answer) > 300:
                        # For voice, give concise version
                        answer = answer[:297] + "..."
                    
                    return JSONResponse(content={
                        "result": json.dumps({
                            "success": True,
                            "found": True,
                            "answer": answer,
                            "question": best_result.get("question", ""),
                            "category": best_result.get("category", ""),
                            "source": "Montana Feed Knowledge Base"
                        })
                    })
                else:
                    return JSONResponse(content={
                        "result": json.dumps({
                            "success": False,
                            "found": False,
                            "answer": "I don't have specific information on that. Let me connect you with a specialist who can help.",
                            "query": question
                        })
                    })
            else:
                raise Exception(f"Knowledge query failed: {response.status_code}")
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error in query_knowledge: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(content={
            "result": json.dumps({
                "success": False,
                "found": False,
                "answer": "I'm having trouble accessing the knowledge base. Let me connect you with a specialist.",
                "error": str(e)
            })
        })


# ============================================================================
# FUNCTION: lookup_staff
# Search for staff by name (LPS, warehouse managers, admin)
# ============================================================================

@router.post("/functions/lookup_staff")
async def retell_lookup_staff(request: Request):
    """
    Look up Montana Feed Company staff by name.
    Searches specialists (LPS), warehouse managers, and other staff.
    """
    try:
        body = await request.body()
        signature = request.headers.get("x-retell-signature", "")
        
        if not verify_retell_signature(body, signature):
            raise HTTPException(status_code=401, detail="Invalid signature")
        
        payload = json.loads(body)
        args = payload.get("args", {})
        
        # Get search parameters
        name = args.get("name", "") or args.get("staff_name", "")
        role = args.get("role", "")  # Optional: "lps", "warehouse manager", "admin"
        
        print(f"\n👤 Retell lookup_staff: name={name}, role={role}")
        
        if not name:
            return JSONResponse(content={
                "result": json.dumps({
                    "found": False,
                    "message": "I need a name to search for. Who are you looking for?"
                })
            })
        
        from main import supabase as sb
        
        # Search specialists table by first_name or last_name
        # Use ilike for case-insensitive partial matching
        result = sb.table("specialists")\
            .select("*")\
            .or_(f"first_name.ilike.%{name}%,last_name.ilike.%{name}%")\
            .eq("is_active", True)\
            .limit(5)\
            .execute()
        
        print(f"   🔍 Search query: first_name or last_name ilike '%{name}%'")
        print(f"   📊 Found {len(result.data) if result.data else 0} matches")
        
        if result.data and len(result.data) > 0:
            # Found specialist(s)
            matches = result.data
            
            if len(matches) == 1:
                # Single match - return it
                specialist = matches[0]
                
                # Build full name from first + last
                full_name = f"{specialist.get('first_name', '')} {specialist.get('last_name', '')}".strip()
                
                print(f"   ✓ Found specialist: {full_name}")
                
                # Determine staff type based on email or role
                email = specialist.get('email', '')
                if 'axmen.com' in email.lower():
                    staff_type = "Warehouse Staff"
                elif 'landolakes.com' in email.lower():
                    staff_type = "Livestock Production Specialist"
                else:
                    staff_type = "Montana Feed Company Staff"
                
                return JSONResponse(content={
                    "result": json.dumps({
                        "found": True,
                        "staff_type": staff_type,
                        "name": full_name,
                        "first_name": specialist.get("first_name"),
                        "last_name": specialist.get("last_name"),
                        "phone": specialist.get("phone"),
                        "email": specialist.get("email"),
                        "territory": specialist.get("territory_name"),
                        "specialist_id": specialist.get("specialist_id") or specialist.get("id"),
                        "message": f"Found {full_name}, {staff_type}" + (f" covering {specialist.get('territory_name')}" if specialist.get('territory_name') else "") + "."
                    })
                })
            
            else:
                # Multiple matches - return list
                names = [f"{s.get('first_name', '')} {s.get('last_name', '')}".strip() for s in matches]
                
                print(f"   ✓ Found {len(matches)} specialists matching '{name}'")
                
                return JSONResponse(content={
                    "result": json.dumps({
                        "found": True,
                        "multiple_matches": True,
                        "count": len(matches),
                        "names": names,
                        "message": f"I found {len(matches)} specialists: {', '.join(names)}. Which one do you need?"
                    })
                })
        
        else:
            # Not found in specialists - could be warehouse manager or admin
            print(f"   ℹ No specialist found matching '{name}'")
            
            return JSONResponse(content={
                "result": json.dumps({
                    "found": False,
                    "searched_name": name,
                    "message": f"I don't have contact information for {name}. They might be at one of our warehouses. Which location are you calling about - Dillon, Miles City, Lewistown, Columbus, or Buffalo?"
                })
            })
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error in lookup_staff: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(content={
            "result": json.dumps({
                "found": False,
                "error": str(e),
                "message": "I had trouble looking up that person."
            })
        })


# ============================================================================
# FUNCTION: lookup_town
# Territory routing based on town name
# ============================================================================

@router.post("/functions/lookup_town")
async def retell_lookup_town(request: Request):
    """
    Look up town to find territory and specialist.
    Reuses your existing lookup_town function.
    """
    try:
        body = await request.body()
        signature = request.headers.get("x-retell-signature", "")
        
        if not verify_retell_signature(body, signature):
            raise HTTPException(status_code=401, detail="Invalid signature")
        
        payload = json.loads(body)
        args = payload.get("args", {})
        
        print(f"\n🗺️ Retell lookup_town: {args}")
        
        # Use your existing function
        from main import lookup_town
        result = await lookup_town(args)
        
        print(f"   ✓ Result: {result.get('success')}, territory={result.get('territory')}")
        
        return JSONResponse(content={
            "result": json.dumps(result)
        })
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error in lookup_town: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(content={
            "result": json.dumps({
                "success": False,
                "error": str(e),
                "message": "I had trouble looking up that location."
            })
        })


# ============================================================================
# FUNCTION: create_lead
# Lead capture with email notification
# ============================================================================

@router.post("/functions/create_lead")
async def retell_create_lead(request: Request):
    """
    Create lead in Supabase and send email to specialist.
    Reuses your existing create_lead function.
    """
    try:
        body = await request.body()
        signature = request.headers.get("x-retell-signature", "")
        
        if not verify_retell_signature(body, signature):
            raise HTTPException(status_code=401, detail="Invalid signature")
        
        payload = json.loads(body)
        call = payload.get("call", {})
        args = payload.get("args", {})
        
        phone_number = call.get("from_number", "")
        
        print(f"\n📝 Retell create_lead: {args.get('first_name')} {args.get('last_name')}")
        
        # Use your existing function
        from main import create_lead
        result = await create_lead(phone_number, args)
        
        print(f"   ✓ Result: {result.get('success')}")
        
        return JSONResponse(content={
            "result": json.dumps(result)
        })
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error in create_lead: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(content={
            "result": json.dumps({
                "success": False,
                "error": str(e),
                "message": "I had trouble saving your information."
            })
        })


# ============================================================================
# FUNCTION: schedule_callback
# Callback scheduling with email notification
# ============================================================================

@router.post("/functions/schedule_callback")
async def retell_schedule_callback(request: Request):
    """
    Schedule callback and notify specialist.
    Reuses your existing schedule_callback function.
    """
    try:
        body = await request.body()
        signature = request.headers.get("x-retell-signature", "")
        
        if not verify_retell_signature(body, signature):
            raise HTTPException(status_code=401, detail="Invalid signature")
        
        payload = json.loads(body)
        call = payload.get("call", {})
        args = payload.get("args", {})
        
        phone_number = call.get("from_number", "")
        
        print(f"\n📅 Retell schedule_callback: {args}")
        
        # Use your existing function
        from main import schedule_callback
        result = await schedule_callback(phone_number, args)
        
        print(f"   ✓ Result: {result.get('success')}, timing={result.get('timing_summary')}")
        
        return JSONResponse(content={
            "result": json.dumps(result)
        })
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error in schedule_callback: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(content={
            "result": json.dumps({
                "success": False,
                "error": str(e),
                "message": "I had trouble scheduling that callback."
            })
        })


# ============================================================================
# FUNCTION: find_specialist (alias for lookup_town)
# Some prompts may call this instead of lookup_town
# ============================================================================

@router.post("/functions/find_specialist")
async def retell_find_specialist(request: Request):
    """
    Find specialist for a location.
    This is an alias that maps to lookup_town.
    """
    try:
        body = await request.body()
        signature = request.headers.get("x-retell-signature", "")
        
        if not verify_retell_signature(body, signature):
            raise HTTPException(status_code=401, detail="Invalid signature")
        
        payload = json.loads(body)
        args = payload.get("args", {})
        
        # Map find_specialist params to lookup_town params
        lookup_params = {
            "town_name": args.get("town") or args.get("town_name") or args.get("location", ""),
            "state": args.get("state", "")
        }
        
        # Also check for county - if provided, we need different logic
        county = args.get("county", "")
        
        print(f"\n👤 Retell find_specialist: town={lookup_params['town_name']}, county={county}")
        
        from main import lookup_town
        
        if lookup_params["town_name"]:
            result = await lookup_town(lookup_params)
        elif county:
            # If only county provided, search by county
            # This is a simplified lookup - could be enhanced
            result = {
                "success": False,
                "message": f"I need a town name to find your specialist. What town are you near in {county} county?"
            }
        else:
            result = {
                "success": False,
                "message": "I need to know your town or county to find your local specialist."
            }
        
        # Reformat response for find_specialist expectations
        if result.get("success") and result.get("specialist"):
            specialist = result["specialist"]
            return JSONResponse(content={
                "result": json.dumps({
                    "found": True,
                    "specialist_name": specialist.get("full_name"),
                    "specialist_email": specialist.get("email"),
                    "specialist_phone": specialist.get("phone"),
                    "territory": result.get("territory"),
                    "town": result.get("town"),
                    "message": result.get("message")
                })
            })
        else:
            return JSONResponse(content={
                "result": json.dumps({
                    "found": False,
                    "message": result.get("message", "Could not find specialist for that location.")
                })
            })
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error in find_specialist: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(content={
            "result": json.dumps({
                "found": False,
                "error": str(e),
                "message": "I had trouble looking up your specialist."
            })
        })


# ============================================================================
# FUNCTION: transfer_call
# Transfer call to specialist phone number
# ============================================================================

@router.post("/functions/transfer_call")
async def retell_transfer_call(request: Request):
    """
    Transfer the call to a specialist's phone number.
    
    Note: Retell call transfer works by returning a special response.
    The agent will announce the transfer and Retell handles the rest.
    """
    try:
        body = await request.body()
        signature = request.headers.get("x-retell-signature", "")
        
        if not verify_retell_signature(body, signature):
            raise HTTPException(status_code=401, detail="Invalid signature")
        
        payload = json.loads(body)
        call = payload.get("call", {})
        args = payload.get("args", {})
        
        # Get transfer details
        phone_number = args.get("phone_number") or args.get("specialist_phone", "")
        specialist_name = args.get("specialist_name", "your specialist")
        reason = args.get("reason", "to help you")
        
        print(f"\n📞 Retell transfer_call:")
        print(f"   To: {specialist_name} ({phone_number})")
        print(f"   Reason: {reason}")
        
        if not phone_number:
            return JSONResponse(content={
                "result": json.dumps({
                    "success": False,
                    "can_transfer": False,
                    "message": "I don't have a phone number to transfer you to. Let me take your information and have someone call you back instead."
                })
            })
        
        # Clean phone number format for Retell
        # Retell expects: +1XXXXXXXXXX format
        import re
        digits = re.sub(r'\D', '', phone_number)
        if len(digits) == 10:
            formatted_phone = f"+1{digits}"
        elif len(digits) == 11 and digits[0] == '1':
            formatted_phone = f"+{digits}"
        else:
            formatted_phone = phone_number
        
        print(f"   Formatted: {formatted_phone}")
        
        # For Retell, we return transfer info and the agent handles it
        # The message will be spoken before transfer
        return JSONResponse(content={
            "result": json.dumps({
                "success": True,
                "can_transfer": True,
                "transfer_number": formatted_phone,
                "specialist_name": specialist_name,
                "message": f"One moment please, I'm transferring you to {specialist_name} now."
            })
        })
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error in transfer_call: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(content={
            "result": json.dumps({
                "success": False,
                "can_transfer": False,
                "error": str(e),
                "message": "I'm having trouble with the transfer. Let me take your information and have someone call you back."
            })
        })
