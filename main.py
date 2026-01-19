from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
import time
import os
from supabase import create_client
import openai
from datetime import datetime
from zep_cloud import Zep

app = FastAPI()

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
    print("MFC Agent started with caching and keep-alive")

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
        "cached_items": len(CACHED_ANSWERS)
    }

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

@app.post("/retell/search_knowledge_base")
async def search_knowledge(data: dict):
    start_total = time.time()
    
    try:
        query = data.get("parameters", {}).get("question", "")
        query_lower = query.lower()
        
        print(f"Query: {query}")
        
        for cached_key, answer in CACHED_ANSWERS.items():
            if cached_key in query_lower:
                duration = (time.time() - start_total) * 1000
                print(f"CACHE HIT: {cached_key} ({duration:.0f}ms)")
                return {"result": answer}
        
        print(f"Cache miss - doing semantic search")
        
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
            print(f"  Total search time: {duration:.0f}ms")
            
            if result.data and len(result.data) > 0:
                return {"result": result.data[0]['answer']}
            else:
                return {"result": "Let me have your specialist follow up with specific details for your situation."}
                
        except asyncio.TimeoutError:
            print(f"Search timed out")
            return {
                "result": "Great question! Let me have your specialist call you back to discuss that - they'll have the most current information for your operation."
            }
    
    except Exception as e:
        print(f"Error in search: {e}")
        return {
            "result": "Let me have your specialist give you a call back to help with that."
        }

@app.post("/retell/functions/get_caller_history")
async def get_caller_history(data: dict):
    """Retrieve caller history from Zep"""
    try:
        print(f"[CALLER_HISTORY] Received data keys: {data.keys()}")
        
        # Get phone from call metadata
        phone = data.get("call", {}).get("from_number", "")
        
        print(f"[CALLER_HISTORY] Extracted phone: {phone}")
        
        if not phone:
            print("[CALLER_HISTORY] No phone number in request")
            return {"result": "No phone number provided"}
        
        # Format phone number consistently
        phone_clean = phone.replace("+1", "").replace("-", "").replace(" ", "")
        user_id = f"+1{phone_clean}"
        
        print(f"[CALLER_HISTORY] Looking up caller history for user_id: {user_id}")
        
        # Try to get user from Zep using SDK
        try:
            user = await asyncio.to_thread(zep_client.user.get, user_id=user_id)
            
            print(f"[CALLER_HISTORY] Found user in Zep!")
            
            user_name = user.metadata.get("name", "Unknown") if user.metadata else "Unknown"
            
            # Try to get user's sessions
            try:
                sessions_result = await asyncio.to_thread(
                    zep_client.user.get_sessions,
                    user_id=user_id
                )
                
                # Handle the response - might be a list or an object with sessions
                sessions = sessions_result if isinstance(sessions_result, list) else getattr(sessions_result, 'sessions', [])
                
                print(f"[CALLER_HISTORY] Found {len(sessions)} sessions")
                
                if sessions and len(sessions) > 0:
                    result_text = f"Returning caller: {user_name}. They have called {len(sessions)} times before."
                    print(f"[CALLER_HISTORY] Returning: {result_text}")
                    return {"result": result_text}
            except Exception as session_error:
                print(f"[CALLER_HISTORY] Error getting sessions: {session_error}")
            
            result_text = f"Returning caller: {user_name}"
            print(f"[CALLER_HISTORY] Returning: {result_text}")
            return {"result": result_text}
            
        except Exception as user_error:
            print(f"[CALLER_HISTORY] User not found: {user_error}")
            return {"result": "New caller"}
            
    except Exception as e:
        print(f"[CALLER_HISTORY] ERROR: {type(e).__name__}: {str(e)}")
        import traceback
        print(f"[CALLER_HISTORY] Traceback: {traceback.format_exc()}")
        return {"result": "New caller"}

@app.post("/retell/functions/lookup_town")
async def lookup_town(data: dict):
    """Look up county and specialist based on town"""
    try:
        # Get town from tool arguments - Retell sends "town_name"
        args = data.get("args", {})
        town = args.get("town_name", args.get("town", "")).strip()
        
        print(f"[LOOKUP_TOWN] Args received: {args}")
        print(f"[LOOKUP_TOWN] Looking up town: {town}")
        
        if not town:
            print("[LOOKUP_TOWN] No town provided")
            return {"result": "No town provided"}
        
        # Search counties table for matching town
        result = supabase.table('counties') \
            .select('name, specialist_id, specialists(name)') \
            .ilike('name', f'%{town}%') \
            .limit(1) \
            .execute()
        
        print(f"[LOOKUP_TOWN] Database result: {len(result.data) if result.data else 0} matches")
        
        if result.data and len(result.data) > 0:
            county_data = result.data[0]
            county_name = county_data['name']
            specialist_name = county_data.get('specialists', {}).get('name', 'Unknown')
            
            result_text = f"County: {county_name}, Specialist: {specialist_name}"
            print(f"[LOOKUP_TOWN] Returning: {result_text}")
            
            return {"result": result_text}
        
        print(f"[LOOKUP_TOWN] No match found for: {town}")
        return {"result": f"County not found for {town}"}
        
    except Exception as e:
        print(f"[LOOKUP_TOWN] ERROR: {type(e).__name__}: {str(e)}")
        import traceback
        print(f"[LOOKUP_TOWN] Traceback: {traceback.format_exc()}")
        return {"result": "Unable to determine county"}

@app.post("/retell/functions/schedule_callback")
async def schedule_callback(data: dict):
    """Schedule a callback and save to database"""
    try:
        # Get parameters from tool arguments
        args = data.get("args", {})
        phone = data.get("call", {}).get("from_number", "")
        name = args.get("name", "")
        reason = args.get("reason", "")
        specialist = args.get("specialist", "")
        
        print(f"[SCHEDULE_CALLBACK] Scheduling callback for {name} ({phone})")
        
        # Insert into callbacks table
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
        print(f"Error scheduling callback: {e}")
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
        
        # Search specialists table for matching name
        result = supabase.table('specialists') \
            .select('name, email, phone, counties(name)') \
            .ilike('name', f'%{name}%') \
            .limit(1) \
            .execute()
        
        print(f"[LOOKUP_STAFF] Database result: {len(result.data) if result.data else 0} matches")
        
        if result.data and len(result.data) > 0:
            staff = result.data[0]
            staff_name = staff['name']
            counties = staff.get('counties', [])
            county_names = ', '.join([c['name'] for c in counties]) if counties else 'Unknown'
            
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

@app.post("/retell/webhook")
async def retell_webhook(data: dict):
    """Handle Retell webhooks for call events"""
    try:
        event = data.get("event", "")
        call = data.get("call", {})
        call_id = call.get("call_id", "")
        
        print(f"[WEBHOOK] Received event: {event} for call: {call_id}")
        
        if event == "call_ended":
            # Save the call session to Zep
            phone = call.get("from_number", "")
            transcript = call.get("transcript", "")
            
            if phone and call_id:
                await save_call_to_zep(call_id, phone, transcript, call)
        
        return {"status": "ok"}
        
    except Exception as e:
        print(f"[WEBHOOK] ERROR: {type(e).__name__}: {str(e)}")
        return {"status": "error", "message": str(e)}

async def save_call_to_zep(call_id: str, phone: str, transcript: str, call_data: dict):
    """Save call session to Zep"""
    try:
        phone_clean = phone.replace("+1", "").replace("-", "").replace(" ", "")
        user_id = f"+1{phone_clean}"
        
        print(f"[ZEP_SAVE] Saving session for {user_id}, call_id: {call_id}")
        
        # Get or create user metadata
        caller_name = call_data.get("metadata", {}).get("caller_name", "Unknown")
        
        # Note: Using Zep SDK for saving will be added in next iteration
        # For now, just log it
        print(f"[ZEP_SAVE] Would save: user={user_id}, session={call_id}, transcript_length={len(transcript)}")
        
        return True
        
    except Exception as e:
        print(f"[ZEP_SAVE] ERROR: {e}")
        return False

@app.post("/save-session")
async def save_session(data: dict):
    """Save conversation session to Zep"""
    try:
        call_id = data.get("call_id")
        transcript = data.get("transcript", "")
        phone = data.get("from_number", "")
        metadata = data.get("metadata", {})
        
        if not phone or not call_id:
            return {"status": "error", "message": "Missing required fields"}
        
        # Format phone number consistently
        phone_clean = phone.replace("+1", "").replace("-", "").replace(" ", "")
        user_id = f"+1{phone_clean}"
        
        print(f"Saving session for {user_id}, call_id: {call_id}")
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            headers = {
                "Authorization": f"Bearer {ZEP_API_KEY}",
                "Content-Type": "application/json"
            }
            
            # Create or update user
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
            
            # Add session with messages
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
            
            # Add the conversation transcript as memory
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
