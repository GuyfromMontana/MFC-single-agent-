from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
import time
import os
from supabase import create_client
import openai

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
RAILWAY_URL = os.getenv("RAILWAY_PUBLIC_URL", "https://your-app.up.railway.app")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
RAILWAY_URL = os.getenv("RAILWAY_PUBLIC_URL", "https://your-app.up.railway.app")

# DEBUG: Print what we're getting from environment
print("=" * 50)
print("ENVIRONMENT VARIABLE CHECK:")
print(f"SUPABASE_URL: {SUPABASE_URL}")
print(f"SUPABASE_KEY: {SUPABASE_KEY[:20] if SUPABASE_KEY else 'NONE/EMPTY'}")
print(f"OPENAI_API_KEY: {OPENAI_API_KEY[:20] if OPENAI_API_KEY else 'NONE/EMPTY'}")
print(f"RAILWAY_URL: {RAILWAY_URL}")
print("=" * 50)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
openai.api_key = OPENAI_API_KEY

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

@app.post("/retell/lookup_town")
async def lookup_town(data: dict):
    pass

@app.post("/retell/schedule_callback")
async def schedule_callback(data: dict):
    pass

@app.get("/retell/get_caller_history")
async def get_caller_history(phone: str):
    pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

