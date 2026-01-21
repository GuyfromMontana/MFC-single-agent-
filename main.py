from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import httpx
import asyncio
import time
import os
from supabase import create_client
import openai
from datetime import datetime
from zep_cloud import Zep
import logging
from typing import Optional, List, Dict
import json

app = FastAPI()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Environment variables
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
RAILWAY_URL = os.getenv("RAILWAY_PUBLIC_URL", "https://your-app.up.railway.app")
ZEP_API_KEY = os.getenv("ZEP_API_KEY")
ZEP_API_URL = "https://api.getzep.com"

# DEBUG: Print what we're getting from environment
print("=" * 50)
print("ENVIRONMENT VARIABLE CHECK:")
print(f"SUPABASE_URL: {SUPABASE_URL}")
print(f"SUPABASE_KEY: {SUPABASE_KEY[:20] if SUPABASE_KEY else 'NONE/EMPTY'}")
print(f"OPENAI_API_KEY: {OPENAI_API_KEY[:20] if OPENAI_API_KEY else 'NONE/EMPTY'}")
print(f"ZEP_API_KEY: {ZEP_API_KEY[:20] if ZEP_API_KEY else 'NONE/EMPTY'}")
print(f"RAILWAY_URL: {RAILWAY_URL}")
print("=" * 50)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
openai.api_key = OPENAI_API_KEY
zep_client = Zep(api_key=ZEP_API_KEY)

CACHED_ANSWERS = {}

async def keep_alive_ping():
    while True:
        try:
            await asyncio.sleep(300)
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.get(f"{RAILWAY_URL}/health")
            print("Keep-alive ping successful")
        except Exception as e:
            print(f"Keep-alive ping failed: {e}")

async def load_cached_questions():
    try:
        result = supabase.table('knowledge_base').select('question, answer, keywords').eq('is_active', True).order('priority', desc=True).limit(100).execute()
        
        global CACHED_ANSWERS
        
        for row in result.data:
            if row.get('keywords'):
                for keyword in row['keywords']:
                    key = keyword.lower().strip()
                    CACHED_ANSWERS[key] = row['answer']
            
            question = row['question'].lower()
            CACHED_ANSWERS[question] = row['answer']
            
            words = question.split()[:5]
            phrase = ' '.join(words)
            CACHED_ANSWERS[phrase] = row['answer']
        
        print(f"Cached {len(CACHED_ANSWERS)} common answer lookups")
        
    except Exception as e:
        print(f"Failed to load cache: {e}")

@app.on_event("startup")
async def startup():
    await load_cached_questions()
    asyncio.create_task(keep_alive_ping())
    print("MFC Agent started with caching, keep-alive, and memory integration")

@app.middleware("http")
async def log_request_time(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = (time.time() - start) * 1000
    print(f"{request.method} {request.url.path}: {duration:.0f}ms")
    if duration > 1000:
        print(f"SLOW: {request.url.path} took {duration:.0f}ms")
    return response

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "mfc-agent",
        "cached_items": len(CACHED_ANSWERS),
        "memory_enabled": True
    }

# ============================================================================
# MEMORY-ENHANCED CALLER RECOGNITION
# ============================================================================

def format_phone_for_zep(phone: str) -> str:
    """Convert phone to Zep user_id format: +1XXXXXXXXXX"""
    clean = ''.join(filter(str.isdigit, phone))
    if len(clean) == 10:
        return f"+1{clean}"
    elif len(clean) == 11 and clean.startswith('1'):
        return f"+{clean}"
    return phone

async def lookup_caller_in_zep(from_number: str) -> dict:
    """Look up caller in Zep and return their info WITH conversation history"""
    try:
        user_id = format_phone_for_zep(from_number)
        logger.info(f"Looking up caller with memory: {user_id}")
        
        # Get user profile
        try:
            user = await asyncio.to_thread(zep_client.user.get, user_id=user_id)
            user_exists = True
            user_name = user.first_name if user and user.first_name else "New caller"
            logger.info(f"Found user: {user_name}")
        except Exception as e:
            logger.info(f"User not found in Zep: {e}")
            user_exists = False
            user_name = "New caller"
        
        # If returning caller, get conversation history
        conversation_context = ""
        if user_exists and user_name != "New caller":
            try:
                logger.info(f"Loading conversation history for {user_id}...")
                
                # Get recent sessions (last 5 conversations)
                sessions_result = await asyncio.to_thread(
                    zep_client.memory.search_sessions,
                    user_id=user_id,
                    limit=5
                )
                
                if sessions_result and hasattr(sessions_result, 'sessions') and sessions_result.sessions:
                    logger.info(f"Found {len(sessions_result.sessions)} past sessions")
                    
                    # Build conversation summary
                    past_topics = []
                    for idx, session in enumerate(sessions_result.sessions):
                        if hasattr(session, 'summary') and session.summary:
                            past_topics.append(f"{idx+1}. {session.summary}")
                            logger.info(f"Session summary: {session.summary[:100]}...")
                    
                    if past_topics:
                        # Combine into natural context
                        conversation_context = "Past conversations with this caller:\n" + "\n".join(past_topics)
                        logger.info(f"Built conversation context with {len(past_topics)} summaries")
                else:
                    logger.info("No past sessions found")
                
            except Exception as mem_error:
                logger.warning(f"Could not load conversation history: {mem_error}")
                import traceback
                logger.warning(f"Memory error traceback: {traceback.format_exc()}")
        
        return {
            "caller_name": user_name,
            "is_returning": user_exists and user_name != "New caller",
            "user_id": user_id,
            "conversation_context": conversation_context
        }
            
    except Exception as e:
        logger.error(f"Zep lookup error: {str(e)}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return {
            "caller_name": "New caller",
            "is_returning": False,
            "user_id": from_number,
            "conversation_context": ""
        }

@app.post("/retell/webhook/inbound")
async def retell_inbound_webhook(request: Request):
    """
    Retell inbound call webhook - fires at call start
    Returns dynamic variables including conversation history
    """
    try:
        body = await request.json()
        logger.info(f"Inbound webhook received: {body}")
        
        # Extract call details
        call = body.get("call", {})
        from_number = call.get("from_number", "")
        call_id = call.get("call_id", "")
        
        if not from_number:
            logger.warning("No from_number in webhook")
            from_number = "unknown"
        
        # Look up caller in Zep WITH memory
        caller_info = await lookup_caller_in_zep(from_number)
        
        # Build response with conversation context
        response = {
            "response_id": 1,
            "dynamic_variables": {
                "caller_name": caller_info["caller_name"],
                "is_returning": str(caller_info["is_returning"]).lower(),
                "conversation_history": caller_info["conversation_context"]
            },
            "metadata": {
                "caller_name": caller_info["caller_name"],
                "is_returning": caller_info["is_returning"],
                "from_number": from_number,
                "user_id": caller_info["user_id"],
                "call_id": call_id,
                "has_history": len(caller_info["conversation_context"]) > 0
            }
        }
        
        logger.info(f"Returning webhook response with memory (history length: {len(caller_info['conversation_context'])} chars)")
        return JSONResponse(content=response)
        
    except Exception as e:
        logger.error(f"Webhook error: {str(e)}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        
        # Return safe defaults on error
        return JSONResponse(
            content={
                "response_id": 1,
                "dynamic_variables": {
                    "caller_name": "New caller",
                    "is_returning": "false",
                    "conversation_history": ""
                },
                "metadata": {
                    "error": str(e)
                }
            }
        )

# ============================================================================
# EXISTING CUSTOM FUNCTIONS (Keep these as-is)
# ============================================================================

async def generate_embedding(text: str):
    start = time.time()
    response = await asyncio.to_thread(
        openai.Embedding.create,
        model="text-embedding-ada-002",
        input=text
    )
    duration = (time.time() - start) * 1000
    print(f"  OpenAI embedding: {duration:.0f}ms")
    return response['data'][0]['embedding']

async def search_database(embedding):
    start = time.time()
    result = await asyncio.to_thread(
        lambda: supabase.rpc('match_knowledge', {
            'query_embedding': embedding,
            'match_threshold': 0.7,
            'match_count': 3
        }).execute()
    )
    duration = (time.time() - start) * 1000
    print(f"  Database search: {duration:.0f}ms")
    return result

@app.post("/retell/functions/search_knowledge_base")
async def search_knowledge(data: dict):
    start_total = time.time()
    
    try:
        query = data.get("args", {}).get("question", data.get("parameters", {}).get("question", ""))
        query_lower = query.lower()
        
        print(f"[SEARCH_KB] Query: {query}")
        
        # Fast cache lookup
        for cached_key, answer in CACHED_ANSWERS.items():
            if cached_key in query_lower:
                duration = (time.time() - start_total) * 1000
                print(f"[SEARCH_KB] CACHE HIT: {cached_key} ({duration:.0f}ms)")
                return {"result": answer}
        
        print(f"[SEARCH_KB] Cache miss - doing semantic search")
        
        try:
            embedding = await asyncio.wait_for(
                generate_embedding(query),
                timeout=3.0
            )
            
            result = await asyncio.wait_for(
                search_database(embedding),
                timeout=2.0
            )
            
            duration = (time.time() - start_total) * 1000
            print(f"[SEARCH_KB] Total search time: {duration:.0f}ms")
            
            if result.data and len(result.data) > 0:
                return {"result": result.data[0]['answer']}
            else:
                return {"result": "Let me have your specialist follow up with specific details for your situation."}
                
        except asyncio.TimeoutError:
            print(f"[SEARCH_KB] Search timed out")
            return {
                "result": "Great question! Let me have your specialist call you back to discuss that - they'll have the most current information for your operation."
            }
    
    except Exception as e:
        print(f"[SEARCH_KB] Error in search: {e}")
        return {
            "result": "Let me have your specialist give you a call back to help with that."
        }

# DEPRECATED: This function is no longer needed - webhook handles caller recognition
@app.post("/retell/functions/get_caller_history")
async def get_caller_history(data: dict):
    """DEPRECATED: Use webhook instead. Keeping for backwards compatibility."""
    logger.warning("[CALLER_HISTORY] DEPRECATED FUNCTION CALLED - Use webhook instead")
    return {"result": "New caller"}

@app.post("/retell/functions/lookup_town")
async def lookup_town(data: dict):
    """Look up county and specialist based on town"""
    try:
        args = data.get("args", {})
        town = args.get("town_name", args.get("town", "")).strip()
        
        print(f"[LOOKUP_TOWN] Args received: {args}")
        print(f"[LOOKUP_TOWN] Looking up town: {town}")
        
        if not town:
            print("[LOOKUP_TOWN] No town provided")
            return {"result": "No town provided"}
        
        county_search = f'{town} County' if not town.lower().endswith('county') else town
        
        result = supabase.table('county_coverage') \
            .select('county_name, primary_lps, state') \
            .ilike('county_name', f'%{county_search}%') \
            .limit(1) \
            .execute()
        
        print(f"[LOOKUP_TOWN] Database result: {len(result.data) if result.data else 0} matches")
        
        if result.data and len(result.data) > 0:
            county_data = result.data[0]
            county_name = county_data['county_name']
            specialist_name = county_data.get('primary_lps', 'Unknown')
            
            result_text = f"County: {county_name}, Specialist: {specialist_name}"
            print(f"[LOOKUP_TOWN] Returning: {result_text}")
            
            return {"result": result_text}
        
        print(f"[LOOKUP_TOWN] No match found for: {town}")
        return {"result": f"TOWN_NOT_FOUND: {town}"}
        
    except Exception as e:
        print(f"[LOOKUP_TOWN] ERROR: {type(e).__name__}: {str(e)}")
        import traceback
        print(f"[LOOKUP_TOWN] Traceback: {traceback.format_exc()}")
        return {"result": "Unable to determine county"}

@app.post("/retell/functions/schedule_callback")
async def schedule_callback(data: dict):
    """Schedule a callback and save to database"""
    try:
        args = data.get("args", {})
        phone = data.get("call", {}).get("from_number", "")
        name = args.get("name", args.get("caller_name", ""))
        reason = args.get("reason", "")
        specialist = args.get("specialist", args.get("specialist_name", ""))
        
        print(f"[SCHEDULE_CALLBACK] Scheduling callback for {name} ({phone})")
        
        callback_data = {
            "phone_number": phone,
            "caller_name": name,
            "reason": reason,
            "specialist_assigned": specialist,
            "status": "pending",
            "created_at": datetime.utcnow().isoformat()
        }
        
        result = supabase.table('callbacks').insert(callback_data).execute()
        
        if result.data:
            return {
                "result": f"Callback scheduled for {specialist} to call {name} at {phone}"
            }
        
        return {"result": "Callback request saved"}
        
    except Exception as e:
        print(f"[SCHEDULE_CALLBACK] Error scheduling callback: {e}")
        import traceback
        print(f"[SCHEDULE_CALLBACK] Traceback: {traceback.format_exc()}")
        return {"result": "Callback request noted"}

@app.post("/retell/functions/lookup_staff")
async def lookup_staff(data: dict):
    """Look up staff member by name"""
    try:
        args = data.get("args", {})
        name = args.get("name", "").strip()
        
        print(f"[LOOKUP_STAFF] Looking up staff: {name}")
        
        if not name:
            print("[LOOKUP_STAFF] No name provided")
            return {"result": "No staff name provided"}
        
        result = supabase.table('specialists') \
            .select('id, name, email, phone') \
            .ilike('name', f'%{name}%') \
            .limit(1) \
            .execute()
        
        print(f"[LOOKUP_STAFF] Database result: {len(result.data) if result.data else 0} matches")
        
        if result.data and len(result.data) > 0:
            staff = result.data[0]
            staff_name = staff['name']
            staff_id = staff['id']
            
            counties_result = supabase.table('county_coverage') \
                .select('county_name') \
                .eq('primary_lps', staff_name) \
                .execute()
            
            county_names = ', '.join([c['county_name'] for c in counties_result.data]) if counties_result.data else 'Unknown'
            
            result_text = f"Found {staff_name}. They cover: {county_names}"
            print(f"[LOOKUP_STAFF] Returning: {result_text}")
            
            return {"result": result_text}
        
        print(f"[LOOKUP_STAFF] No match found for: {name}")
        return {"result": f"Staff member {name} not found"}
        
    except Exception as e:
        print(f"[LOOKUP_STAFF] ERROR: {type(e).__name__}: {str(e)}")
        import traceback
        print(f"[LOOKUP_STAFF] Traceback: {traceback.format_exc()}")
        return {"result": "Unable to look up staff"}

# ============================================================================
# MEMORY-ENHANCED WEBHOOKS
# ============================================================================

@app.post("/retell/webhook")
async def retell_webhook(data: dict):
    """Handle Retell webhooks for call events - enhanced for memory"""
    try:
        event = data.get("event", "")
        call = data.get("call", {})
        call_id = call.get("call_id", "")
        
        logger.info(f"[WEBHOOK] Received event: {event} for call: {call_id}")
        
        if event == "call_ended":
            phone = call.get("from_number", "")
            transcript = call.get("transcript", "")
            
            if phone and call_id:
                # Extract metadata for better memory
                call_analysis = call.get("call_analysis", {})
                metadata = {
                    "duration_ms": call.get("duration_ms", 0),
                    "call_successful": call_analysis.get("call_successful", True),
                    "user_sentiment": call_analysis.get("user_sentiment", "Neutral"),
                    "call_summary": call_analysis.get("call_summary", ""),
                }
                
                await save_call_to_zep_enhanced(call_id, phone, transcript, metadata)
        
        return {"status": "ok"}
        
    except Exception as e:
        logger.error(f"[WEBHOOK] ERROR: {type(e).__name__}: {str(e)}")
        return {"status": "error", "message": str(e)}

async def save_call_to_zep_enhanced(call_id: str, phone: str, transcript: str, metadata: dict):
    """Save call session to Zep with rich context"""
    try:
        user_id = format_phone_for_zep(phone)
        
        logger.info(f"[ZEP_SAVE] Saving session with memory for {user_id}, call_id: {call_id}")
        
        # Ensure user exists in Zep
        try:
            user = await asyncio.to_thread(zep_client.user.get, user_id=user_id)
            logger.info(f"[ZEP_SAVE] User exists: {user.first_name}")
        except Exception:
            logger.info(f"[ZEP_SAVE] User doesn't exist yet - will create on next interaction")
            return False
        
        # Create session with messages
        if transcript and len(transcript) > 10:
            try:
                session_id = call_id
                
                # Parse transcript into messages (Retell format: "Agent: ... User: ...")
                messages = []
                
                # Split on newlines and parse
                parts = transcript.split("\n")
                for part in parts:
                    part = part.strip()
                    if not part:
                        continue
                        
                    if part.startswith("Agent:"):
                        content = part.replace("Agent:", "").strip()
                        if content:
                            messages.append({
                                "role": "assistant",
                                "content": content,
                                "role_type": "assistant"
                            })
                    elif part.startswith("User:") or part.startswith("Caller:"):
                        content = part.replace("User:", "").replace("Caller:", "").strip()
                        if content:
                            messages.append({
                                "role": "user", 
                                "content": content,
                                "role_type": "user"
                            })
                
                # Add to Zep memory
                if messages:
                    logger.info(f"[ZEP_SAVE] Adding {len(messages)} messages to Zep")
                    
                    await asyncio.to_thread(
                        zep_client.memory.add,
                        session_id=session_id,
                        messages=messages,
                        metadata={
                            "call_duration_ms": metadata.get("duration_ms", 0),
                            "call_successful": metadata.get("call_successful", True),
                            "user_sentiment": metadata.get("user_sentiment", "Neutral"),
                            "call_summary": metadata.get("call_summary", ""),
                            "timestamp": datetime.utcnow().isoformat()
                        }
                    )
                    
                    logger.info(f"[ZEP_SAVE] Successfully saved {len(messages)} messages to Zep for session {session_id}")
                    return True
                else:
                    logger.warning(f"[ZEP_SAVE] No messages parsed from transcript")
                
            except Exception as save_error:
                logger.error(f"[ZEP_SAVE] Error saving to Zep: {save_error}")
                import traceback
                logger.error(f"[ZEP_SAVE] Traceback: {traceback.format_exc()}")
                return False
        else:
            logger.warning(f"[ZEP_SAVE] Transcript too short or empty: {len(transcript) if transcript else 0} chars")
        
        return True
        
    except Exception as e:
        logger.error(f"[ZEP_SAVE] ERROR: {e}")
        import traceback
        logger.error(f"[ZEP_SAVE] Traceback: {traceback.format_exc()}")
        return False

@app.post("/save-session")
async def save_session(data: dict):
    """Save conversation session to Zep (legacy endpoint)"""
    try:
        call_id = data.get("call_id")
        transcript = data.get("transcript", "")
        phone = data.get("from_number", "")
        metadata = data.get("metadata", {})
        
        if not phone or not call_id:
            return {"status": "error", "message": "Missing required fields"}
        
        phone_clean = phone.replace("+1", "").replace("-", "").replace(" ", "")
        user_id = f"+1{phone_clean}"
        
        print(f"Saving session for {user_id}, call_id: {call_id}")
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            headers = {
                "Authorization": f"Bearer {ZEP_API_KEY}",
                "Content-Type": "application/json"
            }
            
            user_data = {
                "user_id": user_id,
                "metadata": {
                    "name": metadata.get("caller_name", ""),
                    "phone": phone,
                    "last_call": datetime.utcnow().isoformat()
                }
            }
            
            await client.put(
                f"{ZEP_API_URL}/v2/users/{user_id}",
                headers=headers,
                json=user_data
            )
            
            session_data = {
                "session_id": call_id,
                "user_id": user_id,
                "metadata": {
                    "call_duration": metadata.get("duration", 0),
                    "specialist": metadata.get("specialist", "")
                }
            }
            
            await client.post(
                f"{ZEP_API_URL}/v2/sessions",
                headers=headers,
                json=session_data
            )
            
            if transcript:
                memory_data = {
                    "messages": [
                        {
                            "role": "user",
                            "content": transcript,
                            "role_type": "user"
                        }
                    ]
                }
                
                await client.post(
                    f"{ZEP_API_URL}/v2/sessions/{call_id}/memory",
                    headers=headers,
                    json=memory_data
                )
            
            print(f"Session saved successfully: {call_id}")
            return {"status": "success", "session_id": call_id}
            
    except Exception as e:
        print(f"Error saving session: {e}")
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
