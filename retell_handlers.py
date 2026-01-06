# retell_handlers.py
# Retell AI webhook and function handlers for Montana Feed Company
# 
# This file adds Retell support alongside your existing Vapi setup.
# All endpoints are under /retell/* prefix so they don't conflict.
#
# To enable: Add this line near the top of main.py after the imports:
#   from retell_handlers import router as retell_router
# 
# Then add this line after creating the FastAPI app:
#   app.include_router(retell_router)

import os
import json
import hmac
import hashlib
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

# We'll import these from main.py when this module is loaded
# This allows us to reuse all your existing functions
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
    print("✅ Retell handlers initialized with Zep and Supabase clients")

# ============================================================================
# RETELL SIGNATURE VERIFICATION
# ============================================================================

def verify_retell_signature(body: bytes, signature: str) -> bool:
    """
    Verify the request is from Retell using HMAC signature.
    Retell signs the request body with your API key.
    """
    api_key = os.getenv("RETELL_API_KEY", "")
    if not api_key:
        print("⚠️ RETELL_API_KEY not set - skipping signature verification")
        return True  # Allow requests if no key set (for testing)
    
    if not signature:
        print("⚠️ No x-retell-signature header")
        return False
    
    try:
        # Retell uses the API key as the HMAC secret
        expected = hmac.new(
            api_key.encode('utf-8'),
            body,
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception as e:
        print(f"⚠️ Signature verification error: {e}")
        return False

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
        "retell_api_key_set": bool(os.getenv("RETELL_API_KEY"))
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
        # CALL ENDED - Save to Zep
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
    Process end of Retell call - save conversation to Zep.
    Reuses your existing save_conversation logic.
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
        
        print(f"   📝 Formatted {len(messages)} messages for Zep")
        
        if messages or transcript_text:
            # Import and call your existing save function
            from main import save_conversation, SEND_CALL_SUMMARIES
            from main import get_specialist_for_caller, extract_actions_from_messages
            from main import send_call_summary_email
            
            # Save to Zep
            await save_conversation(from_number, call_id, transcript_text, messages)
            
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
# Retrieves caller context from Zep + Supabase
# ============================================================================

@router.post("/functions/get_caller_history")
async def retell_get_caller_history(request: Request):
    """
    Called at start of conversation to get caller context.
    Reuses your existing get_caller_context function.
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
        
        print(f"\n🧠 Retell get_caller_history: {phone_number}")
        
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
        
        # Use your existing function
        from main import get_caller_context
        context = await get_caller_context(phone_number)
        
        # Add phone to context for reference
        context["caller_phone"] = phone_number
        
        print(f"   ✓ Context: returning={context.get('is_returning_caller')}, known={context.get('is_known_contact')}, name={context.get('caller_name')}")
        
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
# FUNCTION: query_knowledge (Semantic Search)
# Note: You may need to add this function to main.py if not already there
# ============================================================================

@router.post("/functions/query_knowledge")
async def retell_query_knowledge(request: Request):
    """
    Semantic search for cattle nutrition questions.
    Note: Requires search_knowledge_base function in main.py
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
                    "confidence": 0
                })
            })
        
        # Try to use semantic search if available
        try:
            # Check if search function exists in main
            from main import supabase as sb
            
            # Call the semantic search RPC function
            result = sb.rpc(
                'search_knowledge_base_semantic',
                {
                    'query_embedding': None,  # Would need to generate embedding
                    'match_count': 3,
                    'match_threshold': 0.5
                }
            ).execute()
            
            # For now, do a simple text search as fallback
            text_result = sb.table("knowledge_base")\
                .select("question, answer")\
                .or_(f"question.ilike.%{question}%,answer.ilike.%{question}%")\
                .limit(3)\
                .execute()
            
            if text_result.data and len(text_result.data) > 0:
                best_match = text_result.data[0]
                return JSONResponse(content={
                    "result": json.dumps({
                        "success": True,
                        "answer": best_match.get("answer", ""),
                        "related_question": best_match.get("question", ""),
                        "confidence": 0.7,
                        "source": "Montana Feed Knowledge Base"
                    })
                })
            else:
                return JSONResponse(content={
                    "result": json.dumps({
                        "success": False,
                        "answer": "I don't have specific information on that topic. Let me connect you with a specialist who can help.",
                        "confidence": 0
                    })
                })
                
        except Exception as search_err:
            print(f"   ⚠️ Search error: {search_err}")
            return JSONResponse(content={
                "result": json.dumps({
                    "success": False,
                    "answer": "I'm having trouble searching our knowledge base right now. Let me connect you with a specialist.",
                    "error": str(search_err)
                })
            })
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error in query_knowledge: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(content={
            "result": json.dumps({
                "success": False,
                "answer": "I had trouble looking that up.",
                "error": str(e)
            })
        })


# ============================================================================
# FUNCTION: get_warehouse
# Warehouse location and hours
# ============================================================================

@router.post("/functions/get_warehouse")
async def retell_get_warehouse(request: Request):
    """
    Get warehouse location, hours, and contact info.
    """
    try:
        body = await request.body()
        signature = request.headers.get("x-retell-signature", "")
        
        if not verify_retell_signature(body, signature):
            raise HTTPException(status_code=401, detail="Invalid signature")
        
        payload = json.loads(body)
        args = payload.get("args", {})
        
        location = args.get("location", "") or args.get("warehouse", "") or args.get("city", "")
        
        print(f"\n🏢 Retell get_warehouse: {location}")
        
        if not location:
            return JSONResponse(content={
                "result": json.dumps({
                    "found": False,
                    "message": "Which warehouse are you looking for? We have locations in Missoula, Great Falls, Billings, Miles City, Glasgow, and Lewistown."
                })
            })
        
        # Search warehouses/territories for location info
        from main import supabase as sb
        
        # Try territories table (which has warehouse info)
        result = sb.table("territories")\
            .select("territory_name, territory_code")\
            .or_(f"territory_name.ilike.%{location}%,territory_code.ilike.%{location}%")\
            .eq("is_active", True)\
            .limit(1)\
            .execute()
        
        if result.data and len(result.data) > 0:
            territory = result.data[0]
            
            # Standard hours for all locations
            hours = "Monday through Friday, 8am to 5pm"
            
            return JSONResponse(content={
                "result": json.dumps({
                    "found": True,
                    "name": f"{territory['territory_name']} Warehouse",
                    "territory": territory['territory_name'],
                    "hours": hours,
                    "message": f"Our {territory['territory_name']} location is open {hours}."
                })
            })
        else:
            return JSONResponse(content={
                "result": json.dumps({
                    "found": False,
                    "message": f"I couldn't find a warehouse matching '{location}'. Our main locations are Missoula, Great Falls, Billings, Miles City, Glasgow, and Lewistown."
                })
            })
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error in get_warehouse: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(content={
            "result": json.dumps({
                "found": False,
                "error": str(e),
                "message": "I had trouble looking up that warehouse."
            })
        })


# ============================================================================
# FUNCTION: get_recommendations
# Product recommendations
# ============================================================================

@router.post("/functions/get_recommendations")
async def retell_get_recommendations(request: Request):
    """
    Get product recommendations based on needs.
    """
    try:
        body = await request.body()
        signature = request.headers.get("x-retell-signature", "")
        
        if not verify_retell_signature(body, signature):
            raise HTTPException(status_code=401, detail="Invalid signature")
        
        payload = json.loads(body)
        args = payload.get("args", {})
        
        livestock_type = args.get("livestock_type", "cattle")
        need = args.get("need", "")
        season = args.get("season", "")
        
        print(f"\n💊 Retell get_recommendations: {livestock_type}, need={need}")
        
        from main import supabase as sb
        
        # Build query
        query = sb.table("products").select("name, category, description")
        
        if need:
            query = query.or_(f"category.ilike.%{need}%,description.ilike.%{need}%,name.ilike.%{need}%")
        
        result = query.limit(5).execute()
        
        if result.data and len(result.data) > 0:
            products = result.data
            product_names = [p.get("name") for p in products]
            
            return JSONResponse(content={
                "result": json.dumps({
                    "found": True,
                    "count": len(products),
                    "products": products,
                    "message": f"I'd recommend looking at: {', '.join(product_names)}. Would you like details on any of these?"
                })
            })
        else:
            return JSONResponse(content={
                "result": json.dumps({
                    "found": False,
                    "message": "I don't have specific product recommendations for that. Let me connect you with your local specialist who can give personalized advice."
                })
            })
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error in get_recommendations: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(content={
            "result": json.dumps({
                "found": False,
                "error": str(e),
                "message": "I had trouble looking up recommendations."
            })
        })


# ============================================================================
# FUNCTION: search_products
# Product search by name
# ============================================================================

@router.post("/functions/search_products")
async def retell_search_products(request: Request):
    """
    Search products by name or category.
    """
    try:
        body = await request.body()
        signature = request.headers.get("x-retell-signature", "")
        
        if not verify_retell_signature(body, signature):
            raise HTTPException(status_code=401, detail="Invalid signature")
        
        payload = json.loads(body)
        args = payload.get("args", {})
        
        query_text = args.get("query", "") or args.get("product_name", "") or args.get("name", "")
        
        print(f"\n🔎 Retell search_products: {query_text}")
        
        if not query_text:
            return JSONResponse(content={
                "result": json.dumps({
                    "found": False,
                    "message": "What product are you looking for?"
                })
            })
        
        from main import supabase as sb
        
        result = sb.table("products")\
            .select("name, category, description")\
            .or_(f"name.ilike.%{query_text}%,category.ilike.%{query_text}%,description.ilike.%{query_text}%")\
            .limit(5)\
            .execute()
        
        if result.data and len(result.data) > 0:
            products = result.data
            
            return JSONResponse(content={
                "result": json.dumps({
                    "found": True,
                    "count": len(products),
                    "products": products,
                    "message": f"I found {len(products)} product(s) matching '{query_text}'."
                })
            })
        else:
            return JSONResponse(content={
                "result": json.dumps({
                    "found": False,
                    "message": f"I couldn't find a product matching '{query_text}'. Can you check the spelling or describe what you need?"
                })
            })
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error in search_products: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(content={
            "result": json.dumps({
                "found": False,
                "error": str(e),
                "message": "I had trouble searching for that product."
            })
        })
