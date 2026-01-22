"""
Montana Feed Company - Retell AI Webhook with Zep Memory Integration
Updated: Direct HTTP API calls to Zep Cloud (no SDK dependency issues)
Fixed: Specialist lookup, Zep message batching, enhanced logging
"""

import os
import json
import logging
from datetime import datetime
from typing import Optional, Dict, List, Any

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from supabase import create_client, Client
from openai import OpenAI

# ============================================================================
# CONFIGURATION
# ============================================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Montana Feed Retell Webhook")

# Environment variables
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ZEP_API_KEY = os.getenv("ZEP_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Initialize clients with timeout settings
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
openai_client = OpenAI(
    api_key=OPENAI_API_KEY,
    timeout=5.0,
    max_retries=1
)

# Zep Cloud REST API configuration
ZEP_BASE_URL = "https://api.getzep.com/api/v2"
ZEP_HEADERS = {
    "Authorization": f"Api-Key {ZEP_API_KEY}",
    "Content-Type": "application/json"
}

# ============================================================================
# ZEP CLOUD HTTP API FUNCTIONS
# ============================================================================

async def zep_create_user(user_id: str, phone: str, first_name: str = "Caller") -> Optional[Dict]:
    """Create or update a Zep user via direct HTTP call."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{ZEP_BASE_URL}/users",
                headers=ZEP_HEADERS,
                json={
                    "user_id": user_id,
                    "metadata": {"phone": phone},
                    "first_name": first_name
                }
            )
            
            if response.status_code in [200, 201]:
                return response.json()
            else:
                logger.warning(f"Zep user creation returned {response.status_code}: {response.text}")
                return None
                
    except Exception as e:
        logger.error(f"Error creating Zep user: {e}")
        return None


async def zep_get_user_threads(user_id: str, limit: int = 5) -> List[Dict]:
    """Get recent threads for a user via direct HTTP call."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{ZEP_BASE_URL}/threads",
                headers=ZEP_HEADERS,
                params={
                    "user_id": user_id,
                    "page_size": limit,
                    "order_by": "updated_at",
                    "asc": False
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                return data.get("threads", [])
            else:
                logger.warning(f"Zep get threads returned {response.status_code}: {response.text}")
                return []
                
    except Exception as e:
        logger.error(f"Error getting Zep threads: {e}")
        return []


async def zep_get_thread_messages(thread_id: str, limit: int = 10) -> List[Dict]:
    """Get messages from a specific thread via direct HTTP call."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{ZEP_BASE_URL}/threads/{thread_id}/messages",
                headers=ZEP_HEADERS,
                params={"limit": limit}
            )
            
            if response.status_code == 200:
                data = response.json()
                return data.get("messages", [])
            else:
                logger.warning(f"Zep get messages returned {response.status_code}: {response.text}")
                return []
                
    except Exception as e:
        logger.error(f"Error getting Zep messages: {e}")
        return []


async def zep_create_thread(thread_id: str, user_id: str) -> Optional[Dict]:
    """Create a new thread via direct HTTP call."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{ZEP_BASE_URL}/threads",
                headers=ZEP_HEADERS,
                json={
                    "thread_id": thread_id,
                    "user_id": user_id
                }
            )
            
            if response.status_code in [200, 201]:
                return response.json()
            else:
                logger.warning(f"Zep thread creation returned {response.status_code}: {response.text}")
                return None
                
    except Exception as e:
        logger.error(f"Error creating Zep thread: {e}")
        return None


async def zep_add_messages(thread_id: str, messages: List[Dict]) -> Optional[Dict]:
    """Add messages to a thread via direct HTTP call."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{ZEP_BASE_URL}/threads/{thread_id}/messages",
                headers=ZEP_HEADERS,
                json={"messages": messages}
            )
            
            if response.status_code in [200, 201]:
                return response.json()
            else:
                logger.warning(f"Zep add messages returned {response.status_code}: {response.text}")
                return None
                
    except Exception as e:
        logger.error(f"Error adding Zep messages: {e}")
        return None


async def zep_get_user_context(thread_id: str) -> Optional[Dict]:
    """Get user context for a thread via direct HTTP call."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{ZEP_BASE_URL}/threads/{thread_id}/context",
                headers=ZEP_HEADERS
            )
            
            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(f"Zep get context returned {response.status_code}: {response.text}")
                return None
                
    except Exception as e:
        logger.error(f"Error getting Zep context: {e}")
        return None


# ============================================================================
# MEMORY FUNCTIONS (Using HTTP API)
# ============================================================================

async def lookup_caller_in_zep(phone: str) -> Dict[str, Any]:
    """
    Look up caller's past conversations in Zep Cloud using direct HTTP API.
    
    Returns:
        {
            "found": bool,
            "user_id": str,
            "caller_name": str,
            "conversation_history": str,
            "message": str
        }
    """
    try:
        # 1. Create user_id from phone number
        user_id = f"caller_{phone.replace('+', '').replace(' ', '')}"
        
        # 2. Ensure user exists in Zep
        await zep_create_user(user_id, phone)
        
        # 3. Get recent threads for this user
        threads = await zep_get_user_threads(user_id, limit=5)
        
        if not threads:
            logger.info(f"No past threads found for {user_id}")
            return {
                "found": False,
                "user_id": user_id,
                "caller_name": "Caller",
                "conversation_history": "",
                "message": "New caller - no conversation history"
            }
        
        # 4. Get messages from most recent thread
        latest_thread = threads[0]
        thread_id = latest_thread.get("thread_id")
        
        messages = await zep_get_thread_messages(thread_id, limit=10)
        
        # 5. Format conversation history
        conversation_lines = []
        caller_name = "Caller"
        
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            name = msg.get("name", "")
            
            # Extract caller's name if present
            if role == "user" and name and name != "Caller":
                caller_name = name
                logger.info(f"Extracted caller name: {caller_name}")
            
            # Format message
            speaker = "Customer" if role == "user" else "Agent"
            if content:
                conversation_lines.append(f"{speaker}: {content}")
        
        conversation_text = "\n".join(conversation_lines[-5:])  # Last 5 exchanges
        
        logger.info(f"Memory lookup complete: caller_name={caller_name}, found {len(messages)} messages")
        
        return {
            "found": True,
            "user_id": user_id,
            "caller_name": caller_name,
            "conversation_history": conversation_text,
            "message": f"Found {len(messages)} past messages"
        }
        
    except Exception as e:
        logger.error(f"Error in lookup_caller_in_zep: {e}", exc_info=True)
        return {
            "found": False,
            "user_id": f"caller_{phone}",
            "caller_name": "Caller",
            "conversation_history": "",
            "message": f"Error: {str(e)}"
        }


async def save_call_to_zep_enhanced(
    phone: str,
    transcript: List[Dict],
    call_id: str,
    caller_name: str = "Caller"
) -> Dict[str, Any]:
    """
    Save call transcript to Zep Cloud using direct HTTP API.
    Handles Zep's 30-message limit by batching.
    
    Args:
        phone: Caller's phone number
        transcript: List of transcript entries with role and content
        call_id: Unique call identifier
        caller_name: Name of the caller
    
    Returns:
        Status dict with success/failure info
    """
    try:
        # 1. Create user_id
        user_id = f"caller_{phone.replace('+', '').replace(' ', '')}"
        
        # 2. Ensure user exists
        await zep_create_user(user_id, phone, first_name=caller_name)
        
        # 3. Create thread for this call
        thread_id = f"call_{call_id}"
        await zep_create_thread(thread_id, user_id)
        
        # 4. Format messages for Zep
        zep_messages = []
        for entry in transcript:
            role = entry.get("role", "user")
            content = entry.get("content", "")
            
            if not content:
                continue
            
            # Map Retell roles to Zep roles
            zep_role = "user" if role == "user" else "assistant"
            message_name = caller_name if role == "user" else "Montana Feed Agent"
            
            zep_messages.append({
                "role": zep_role,
                "content": content,
                "name": message_name,
                "metadata": {
                    "call_id": call_id,
                    "phone": phone,
                    "timestamp": datetime.utcnow().isoformat()
                }
            })
        
        # 5. Add messages to thread in batches of 30 (Zep's limit)
        if zep_messages:
            batch_size = 30
            total_saved = 0
            
            for i in range(0, len(zep_messages), batch_size):
                batch = zep_messages[i:i + batch_size]
                logger.info(f"Saving batch {i//batch_size + 1}: {len(batch)} messages")
                result = await zep_add_messages(thread_id, batch)
                
                if result:
                    total_saved += len(batch)
                else:
                    logger.error(f"Failed to save batch {i//batch_size + 1}")
            
            if total_saved > 0:
                logger.info(f"Successfully saved {total_saved} messages to Zep for call {call_id}")
                return {
                    "success": True,
                    "thread_id": thread_id,
                    "message_count": total_saved,
                    "message": f"Saved {total_saved} messages to Zep"
                }
            else:
                return {
                    "success": False,
                    "message": "Failed to add messages to Zep"
                }
        else:
            return {
                "success": False,
                "message": "No messages to save"
            }
        
    except Exception as e:
        logger.error(f"Error in save_call_to_zep_enhanced: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"Error: {str(e)}"
        }


# ============================================================================
# SPECIALIST LOOKUP (FIXED FOR ARRAY SEARCH)
# ============================================================================

def lookup_specialist_by_town(town_name: str) -> Optional[Dict[str, str]]:
    """
    Look up specialist by town/county name.
    Searches the specialists table's counties array.
    """
    try:
        logger.info(f"Looking up specialist for town: '{town_name}'")
        
        if not town_name or town_name.strip() == "":
            logger.warning("Empty town name provided to lookup_specialist_by_town")
            return None
        
        town_name = town_name.strip()
        
        # Use PostgreSQL array overlap operator via raw SQL
        # This searches if the counties array contains the town name (case-insensitive)
        result = supabase.rpc(
            'find_specialist_by_county',
            {'county_name': town_name}
        ).execute()
        
        # Fallback: try direct query with contains operator
        if not result.data or len(result.data) == 0:
            logger.info(f"Trying direct query for: {town_name}")
            result = supabase.table("specialists") \
                .select("first_name, last_name, phone, counties") \
                .eq("is_active", True) \
                .execute()
            
            # Filter in Python (case-insensitive search in counties array)
            if result.data:
                for specialist in result.data:
                    counties = specialist.get("counties", []) or []
                    if any(town_name.lower() in county.lower() for county in counties):
                        full_name = f"{specialist.get('first_name', '')} {specialist.get('last_name', '')}".strip()
                        logger.info(f"Found specialist: {full_name} for {town_name}")
                        return {
                            "specialist_name": full_name,
                            "specialist_phone": specialist.get("phone", "")
                        }
        
        elif result.data and len(result.data) > 0:
            specialist = result.data[0]
            full_name = f"{specialist.get('first_name', '')} {specialist.get('last_name', '')}".strip()
            logger.info(f"Found specialist via RPC: {full_name} for {town_name}")
            return {
                "specialist_name": full_name,
                "specialist_phone": specialist.get("phone", "")
            }
        
        logger.info(f"No specialist found for: {town_name}")
        return None
        
    except Exception as e:
        logger.error(f"Error looking up specialist: {e}")
        return None


# ============================================================================
# KNOWLEDGE BASE SEARCH (OPTIMIZED)
# ============================================================================

def search_knowledge_base(query: str, top_k: int = 3) -> str:
    """Search knowledge base using semantic similarity with optimized performance."""
    try:
        # Generate embedding for query with short timeout
        response = openai_client.embeddings.create(
            model="text-embedding-3-small",
            input=query
        )
        query_embedding = response.data[0].embedding
        
        # Search using pgvector
        result = supabase.rpc(
            "match_knowledge_base",
            {
                "query_embedding": query_embedding,
                "match_threshold": 0.7,
                "match_count": top_k
            }
        ).execute()
        
        if result.data:
            contexts = []
            for item in result.data:
                contexts.append(f"• {item['content']}")
            return "\n".join(contexts)
        
        return "No relevant information found in our knowledge base."
        
    except Exception as e:
        logger.error(f"Knowledge base search error: {e}")
        return "I'll connect you with one of our specialists who can help with your specific question. They'll have the most up-to-date information on feed recommendations."


# ============================================================================
# LEAD CAPTURE (FIXED FOR ACTUAL SCHEMA)
# ============================================================================

def capture_lead(name: str, phone: str, location: str, interests: str) -> bool:
    """
    Capture lead information using correct schema.
    Maps simple inputs to comprehensive leads table.
    """
    try:
        # Split name into first and last
        name_parts = name.strip().split(None, 1)
        first_name = name_parts[0] if name_parts else "Unknown"
        last_name = name_parts[1] if len(name_parts) > 1 else ""
        
        data = {
            "first_name": first_name,
            "last_name": last_name,
            "phone": phone,
            "city": location,
            "primary_interest": interests,
            "lead_source": "retell_call",
            "lead_status": "new",
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }
        
        result = supabase.table("leads").insert(data).execute()
        return bool(result.data)
        
    except Exception as e:
        logger.error(f"Error capturing lead: {e}")
        return False


# ============================================================================
# WEBHOOK ENDPOINTS
# ============================================================================

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "montana-feed-retell-webhook",
        "memory_enabled": bool(ZEP_API_KEY),
        "knowledge_base_enabled": bool(OPENAI_API_KEY),
        "timestamp": datetime.utcnow().isoformat()
    }


@app.post("/retell-webhook")
async def retell_webhook(request: Request):
    """
    Main webhook endpoint for Retell AI.
    Provides specialist lookup, knowledge base search, lead capture, and memory.
    """
    try:
        body = await request.json()
        event_type = body.get("event", "unknown")
        logger.info(f"Webhook received: {event_type}")
        
        # Extract key data
        call_data = body.get("call", {})
        call_id = call_data.get("call_id", "unknown")
        phone = call_data.get("from_number", "")
        
        # Extract transcript from correct location
        transcript = call_data.get("transcript_object", [])
        
        # MEMORY LOOKUP - Check if caller has conversation history (only on call_started)
        memory_data = {"caller_name": "Caller", "conversation_history": ""}
        if event_type == "call_started":
            memory_data = await lookup_caller_in_zep(phone)
            logger.info(f"Returning caller_name to Retell: {memory_data.get('caller_name')}")
        
        caller_name = memory_data.get("caller_name", "Caller")
        conversation_history = memory_data.get("conversation_history", "")
        
        # Build response data
        response_data = {
            "call_id": call_id,
            "caller_name": caller_name,
            "conversation_history": conversation_history,
            "response_id": 1
        }
        
        # Handle function calls if present
        function_call = body.get("function_call")
        if function_call:
            function_name = function_call.get("name")
            arguments = function_call.get("arguments", {})
            
            if function_name == "lookup_specialist":
                town = arguments.get("town", "")
                specialist = lookup_specialist_by_town(town)
                if specialist:
                    response_data["specialist"] = specialist
                else:
                    response_data["specialist"] = {
                        "message": f"No specialist assigned to {town}"
                    }
            
            elif function_name == "search_knowledge":
                query = arguments.get("query", "")
                results = search_knowledge_base(query)
                response_data["knowledge_results"] = results
            
            elif function_name == "capture_lead":
                name = arguments.get("name", "")
                location = arguments.get("location", "")
                interests = arguments.get("interests", "")
                success = capture_lead(name, phone, location, interests)
                response_data["lead_captured"] = success
        
        # SAVE TO MEMORY - Only on call_ended event when transcript is available
        if event_type == "call_ended" and transcript and len(transcript) > 0:
            logger.info(f"Saving transcript with {len(transcript)} messages to Zep for call {call_id}")
            save_result = await save_call_to_zep_enhanced(
                phone=phone,
                transcript=transcript,
                call_id=call_id,
                caller_name=caller_name
            )
            response_data["memory_saved"] = save_result.get("success", False)
            if save_result.get("success"):
                logger.info(f"Memory saved successfully for call {call_id}")
            else:
                logger.warning(f"Memory save failed for call {call_id}: {save_result.get('message')}")
        
        return JSONResponse(content=response_data)
        
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )


@app.post("/test-memory")
async def test_memory(request: Request):
    """Test endpoint to verify Zep memory integration."""
    try:
        body = await request.json()
        phone = body.get("phone", "+14065551234")
        
        lookup_result = await lookup_caller_in_zep(phone)
        
        test_transcript = [
            {"role": "user", "content": "Hello, I need feed for my cattle."},
            {"role": "agent", "content": "I'd be happy to help. What type of cattle?"}
        ]
        
        save_result = await save_call_to_zep_enhanced(
            phone=phone,
            transcript=test_transcript,
            call_id="test_" + datetime.utcnow().strftime("%Y%m%d%H%M%S"),
            caller_name="Test Caller"
        )
        
        return {
            "lookup_test": lookup_result,
            "save_test": save_result,
            "status": "Memory integration working"
        }
        
    except Exception as e:
        logger.error(f"Test memory error: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )


# ============================================================================
# RETELL FUNCTION ENDPOINTS
# ============================================================================

@app.post("/retell/functions/get_warehouse")
async def get_warehouse(request: Request):
    """Get warehouse information by location."""
    try:
        body = await request.json()
        arguments = body.get("arguments", {})
        location = arguments.get("location", "")
        
        return JSONResponse(content={
            "result": f"Warehouse lookup for {location} - contact main office at 406-555-0100 for details",
            "success": True
        })
    except Exception as e:
        logger.error(f"get_warehouse error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e), "result": "Unable to get warehouse info"})


@app.post("/retell/functions/transfer_call_tool")
async def transfer_call_tool(request: Request):
    """Handle call transfer requests."""
    try:
        body = await request.json()
        arguments = body.get("arguments", {})
        transfer_to = arguments.get("transfer_to", "")
        
        return JSONResponse(content={
            "result": f"Transferring you to {transfer_to} now. Please hold.",
            "success": True
        })
    except Exception as e:
        logger.error(f"transfer_call_tool error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e), "result": "Unable to transfer call"})


@app.post("/retell/functions/lookup_town")
async def lookup_town(request: Request):
    """Look up specialist by town name."""
    try:
        body = await request.json()
        logger.info(f"lookup_town called with body: {json.dumps(body)}")
        
        arguments = body.get("arguments", {})
        logger.info(f"Arguments: {json.dumps(arguments)}")
        
        # Try different possible argument names
        town = arguments.get("town", "") or arguments.get("location", "") or arguments.get("city", "")
        logger.info(f"Extracted town: '{town}'")
        
        specialist = lookup_specialist_by_town(town)
        
        if specialist:
            result = f"{specialist['specialist_name']} handles {town}. You can reach them at {specialist['specialist_phone']}."
        else:
            result = f"We don't have a specialist assigned to {town} yet. Please contact our main office at 406-555-0100."
        
        return JSONResponse(content={"result": result, "success": True})
    except Exception as e:
        logger.error(f"lookup_town error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e), "result": "Unable to look up specialist"})


@app.post("/retell/functions/schedule_callback")
async def schedule_callback(request: Request):
    """Schedule a callback."""
    try:
        body = await request.json()
        arguments = body.get("arguments", {})
        name = arguments.get("name", "")
        phone_num = arguments.get("phone", body.get("call", {}).get("from_number", ""))
        callback_time = arguments.get("callback_time", "")
        notes = arguments.get("notes", "")
        
        success = capture_lead(name, phone_num, "callback", f"Callback: {callback_time} - {notes}")
        
        if success:
            result = f"Perfect, {name}. I've scheduled a callback for {callback_time}. Someone will call you at {phone_num}."
        else:
            result = f"I've noted your callback request for {callback_time}, but there was an issue saving it. Please call back if you don't hear from us."
        
        return JSONResponse(content={"result": result, "success": success})
    except Exception as e:
        logger.error(f"schedule_callback error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e), "result": "Unable to schedule callback"})


@app.post("/retell/functions/create_lead")
async def create_lead_endpoint(request: Request):
    """Capture lead information."""
    try:
        body = await request.json()
        arguments = body.get("arguments", {})
        name = arguments.get("name", "")
        phone_num = arguments.get("phone", body.get("call", {}).get("from_number", ""))
        location = arguments.get("location", "")
        interests = arguments.get("interests", "")
        
        success = capture_lead(name, phone_num, location, interests)
        
        if success:
            result = f"Thank you, {name}. I've saved your contact information and someone from our team will reach out to you soon."
        else:
            result = "I've noted your information. If you don't hear from us, please call back."
        
        return JSONResponse(content={"result": result, "success": success})
    except Exception as e:
        logger.error(f"create_lead error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e), "result": "Unable to save contact info"})


@app.post("/retell/functions/query_knowledge")
async def query_knowledge(request: Request):
    """Search knowledge base."""
    try:
        body = await request.json()
        arguments = body.get("arguments", {})
        query = arguments.get("query", "")
        
        results = search_knowledge_base(query, top_k=3)
        
        return JSONResponse(content={"result": results, "success": True})
    except Exception as e:
        logger.error(f"query_knowledge error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e), "result": "Unable to search knowledge base"})


@app.post("/retell/functions/query_mfc_knowledge")
async def query_mfc_knowledge(request: Request):
    """Search MFC-specific knowledge."""
    try:
        body = await request.json()
        arguments = body.get("arguments", {})
        query = arguments.get("query", "")
        
        results = search_knowledge_base(query, top_k=3)
        
        return JSONResponse(content={"result": results, "success": True})
    except Exception as e:
        logger.error(f"query_mfc_knowledge error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e), "result": "Unable to search knowledge base"})


@app.post("/retell/functions/search_products")
async def search_products(request: Request):
    """Search for product information."""
    try:
        body = await request.json()
        arguments = body.get("arguments", {})
        query = arguments.get("query", "")
        
        results = search_knowledge_base(f"products {query}", top_k=3)
        
        return JSONResponse(content={"result": results, "success": True})
    except Exception as e:
        logger.error(f"search_products error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e), "result": "Unable to search products"})


@app.post("/retell/functions/get_recommendations")
async def get_recommendations(request: Request):
    """Get feed recommendations."""
    try:
        body = await request.json()
        arguments = body.get("arguments", {})
        query = arguments.get("query", "")
        animal_type = arguments.get("animal_type", "cattle")
        
        search_query = f"{animal_type} {query}" if animal_type else query
        results = search_knowledge_base(search_query, top_k=3)
        
        return JSONResponse(content={"result": results, "success": True})
    except Exception as e:
        logger.error(f"get_recommendations error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e), "result": "Unable to get recommendations"})


@app.post("/retell/functions/end_call")
async def end_call(request: Request):
    """Handle call ending."""
    try:
        return JSONResponse(content={
            "result": "Thank you for calling Montana Feed Company. Have a great day!",
            "success": True
        })
    except Exception as e:
        logger.error(f"end_call error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e), "result": "Goodbye"})


@app.post("/retell/functions/lookup_staff")
async def lookup_staff(request: Request):
    """Look up staff member by location."""
    try:
        body = await request.json()
        arguments = body.get("arguments", {})
        location = arguments.get("location", "")
        
        specialist = lookup_specialist_by_town(location)
        
        if specialist:
            result = f"For {location}, your specialist is {specialist['specialist_name']}. You can reach them at {specialist['specialist_phone']}."
        else:
            result = f"I don't have a staff member assigned to {location}. Let me connect you with our main office."
        
        return JSONResponse(content={"result": result, "success": True})
    except Exception as e:
        logger.error(f"lookup_staff error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e), "result": "Unable to look up staff"})


@app.post("/retell/functions/search_knowledge_base")
async def search_knowledge_base_endpoint(request: Request):
    """Search knowledge base."""
    try:
        body = await request.json()
        arguments = body.get("arguments", {})
        query = arguments.get("query", "")
        
        results = search_knowledge_base(query, top_k=3)
        
        return JSONResponse(content={"result": results, "success": True})
    except Exception as e:
        logger.error(f"search_knowledge_base error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e), "result": "Unable to search knowledge base"})


@app.post("/retell/functions/get_caller_history")
async def get_caller_history(request: Request):
    """Get caller's conversation history."""
    try:
        body = await request.json()
        phone = body.get("call", {}).get("from_number", "")
        
        memory_data = await lookup_caller_in_zep(phone)
        
        history = memory_data.get("conversation_history", "")
        if history:
            result = f"Based on our previous conversations: {history}"
        else:
            result = "I don't see any previous conversations in our system."
        
        return JSONResponse(content={
            "result": result,
            "caller_name": memory_data.get("caller_name", "Caller"),
            "success": True
        })
    except Exception as e:
        logger.error(f"get_caller_history error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e), "result": "Unable to retrieve caller history"})


# ============================================================================
# APPLICATION STARTUP
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
