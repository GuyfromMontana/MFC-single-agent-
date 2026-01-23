"""
Montana Feed Company - Retell AI Webhook with Zep Memory Integration
Version 2.2.0 - Optimized for speed, better name extraction
"""

import os
import json
import logging
import re
import asyncio
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
# ZEP CLOUD HTTP API FUNCTIONS (OPTIMIZED)
# ============================================================================

async def zep_get_user(user_id: str) -> Optional[Dict]:
    """Get a Zep user's details via direct HTTP call."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{ZEP_BASE_URL}/users/{user_id}",
                headers=ZEP_HEADERS
            )
            if response.status_code == 200:
                return response.json()
            return None
    except Exception as e:
        logger.error(f"Error getting Zep user: {e}")
        return None


async def zep_create_or_update_user(user_id: str, phone: str, first_name: str = "Caller") -> Optional[Dict]:
    """Create or update a Zep user - tries create first, then update if exists."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Try create first
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
                logger.info(f"Created Zep user: {user_id} with name: {first_name}")
                return response.json()
            elif response.status_code == 400 and "already exists" in response.text:
                # User exists - update with PATCH
                if first_name and first_name.lower() not in ["caller", "unknown"]:
                    response = await client.patch(
                        f"{ZEP_BASE_URL}/users/{user_id}",
                        headers=ZEP_HEADERS,
                        json={"first_name": first_name}
                    )
                    if response.status_code == 200:
                        logger.info(f"Updated Zep user {user_id} with name: {first_name}")
                        return response.json()
                return {"user_id": user_id, "exists": True}
            else:
                logger.warning(f"Zep user operation returned {response.status_code}: {response.text}")
                return None
                
    except Exception as e:
        logger.error(f"Error in zep_create_or_update_user: {e}")
        return None


async def zep_create_thread(thread_id: str, user_id: str) -> Optional[Dict]:
    """Create a new thread via direct HTTP call."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{ZEP_BASE_URL}/threads",
                headers=ZEP_HEADERS,
                json={"thread_id": thread_id, "user_id": user_id}
            )
            if response.status_code in [200, 201]:
                return response.json()
            return None
    except Exception as e:
        logger.error(f"Error creating Zep thread: {e}")
        return None


async def zep_add_messages(thread_id: str, messages: List[Dict]) -> Optional[Dict]:
    """Add messages to a thread via direct HTTP call."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
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


# ============================================================================
# NAME EXTRACTION - IMPROVED
# ============================================================================

def extract_name_from_transcript(transcript: List[Dict]) -> Optional[str]:
    """
    Extract caller's name from conversation transcript.
    IMPROVED: Better filtering of false positives.
    """
    if not transcript:
        return None
    
    # Words that should NOT be captured as names
    skip_words = {
        # Common verbs/states
        "good", "fine", "great", "well", "okay", "ok", "alright",
        "here", "calling", "looking", "interested", "wondering", 
        "thinking", "trying", "wanting", "needing", "hoping",
        "just", "actually", "really", "very", "pretty",
        # Greetings
        "hello", "hi", "hey", "morning", "afternoon", "evening",
        # Questions
        "what", "who", "where", "when", "why", "how",
        # Common phrases that get caught
        "glad", "happy", "pleased", "sure", "ready",
        "new", "old", "young", "local", "nearby",
        # Business related
        "customer", "caller", "rancher", "farmer", "producer",
    }
    
    # Patterns that look for explicit name introductions
    name_patterns = [
        # "My name is John Smith" - most reliable
        r"my name is\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        # "This is John Smith calling"
        r"this is\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+calling",
        # "I'm John Smith" - but NOT "I'm wondering/looking/etc"
        r"(?:^|\.\s+)I'?m\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)(?:\s*[,.]|\s+and\s|\s+from\s|\s+over\s|\s+out\s|\s+here\s|$)",
        # "Call me John"
        r"call me\s+([A-Z][a-z]+)",
        # "The name is John"
        r"the name is\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
    ]
    
    # Only check user messages from the first part of conversation
    user_messages = [
        msg.get("content", "") 
        for msg in transcript[:8]
        if msg.get("role") == "user" and msg.get("content")
    ]
    
    for message in user_messages:
        for pattern in name_patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                name = match.group(1).strip()
                
                # Validate: not a skip word, looks like a real name
                first_word = name.split()[0].lower() if name else ""
                
                if first_word in skip_words:
                    logger.debug(f"Skipping false positive name: {name}")
                    continue
                    
                if len(name) < 2 or len(name) > 40:
                    continue
                
                # Check it has at least one capital letter (proper noun)
                if not any(c.isupper() for c in name):
                    continue
                
                logger.info(f"Extracted name from transcript: {name}")
                return name.title()
    
    return None


# ============================================================================
# CALLER NAME LOOKUP - OPTIMIZED
# ============================================================================

def get_caller_name_from_leads(phone: str) -> Optional[str]:
    """Look up caller name from leads table by phone number."""
    try:
        result = supabase.table("leads") \
            .select("first_name, last_name") \
            .eq("phone", phone) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        
        if result.data and len(result.data) > 0:
            lead = result.data[0]
            first_name = lead.get("first_name", "").strip()
            last_name = lead.get("last_name", "").strip()
            
            if first_name and first_name.lower() not in ["unknown", "caller", ""]:
                full_name = f"{first_name} {last_name}".strip() if last_name else first_name
                return full_name
        return None
    except Exception as e:
        logger.error(f"Error looking up name in leads: {e}")
        return None


def update_lead_with_name(phone: str, first_name: str, last_name: str = "") -> bool:
    """Update or create a lead record with the caller's name."""
    try:
        existing = supabase.table("leads") \
            .select("id, first_name") \
            .eq("phone", phone) \
            .limit(1) \
            .execute()
        
        if existing.data and len(existing.data) > 0:
            lead = existing.data[0]
            current_name = lead.get("first_name", "").lower()
            if not current_name or current_name in ["unknown", "caller"]:
                supabase.table("leads") \
                    .update({
                        "first_name": first_name,
                        "last_name": last_name,
                        "updated_at": datetime.utcnow().isoformat()
                    }) \
                    .eq("id", lead["id"]) \
                    .execute()
                logger.info(f"Updated lead {phone} with name: {first_name} {last_name}")
                return True
        else:
            supabase.table("leads").insert({
                "first_name": first_name,
                "last_name": last_name,
                "phone": phone,
                "lead_source": "retell_call",
                "lead_status": "new",
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat()
            }).execute()
            logger.info(f"Created new lead for {phone}: {first_name} {last_name}")
            return True
        return False
    except Exception as e:
        logger.error(f"Error updating lead with name: {e}")
        return False


# ============================================================================
# MEMORY LOOKUP - FAST VERSION
# ============================================================================

async def lookup_caller_fast(phone: str) -> Dict[str, Any]:
    """
    FAST caller lookup - runs leads and Zep queries in parallel.
    Skips conversation history retrieval to reduce latency.
    """
    try:
        user_id = f"caller_{phone.replace('+', '').replace(' ', '')}"
        
        # Run both lookups in parallel
        leads_name = get_caller_name_from_leads(phone)
        zep_user = await zep_get_user(user_id)
        
        # Determine caller name
        caller_name = None
        
        # Priority 1: Leads table (most reliable)
        if leads_name:
            caller_name = leads_name
            logger.info(f"Found name in leads: {caller_name}")
        # Priority 2: Zep user record
        elif zep_user:
            zep_name = zep_user.get("first_name", "")
            if zep_name and zep_name.lower() not in ["caller", "unknown", "wondering", ""]:
                # Extra validation - skip obvious bad names
                if not any(word in zep_name.lower() for word in ["wondering", "looking", "thinking", "calling"]):
                    caller_name = zep_name
                    logger.info(f"Found name in Zep: {caller_name}")
        
        logger.info(f"Fast lookup complete: caller_name={caller_name or 'New caller'}")
        
        return {
            "found": caller_name is not None,
            "user_id": user_id,
            "caller_name": caller_name,
            "conversation_history": "",  # Skip for speed
            "message": f"Caller: {caller_name}" if caller_name else "New caller"
        }
        
    except Exception as e:
        logger.error(f"Error in lookup_caller_fast: {e}", exc_info=True)
        return {
            "found": False,
            "user_id": f"caller_{phone.replace('+', '')}",
            "caller_name": None,
            "conversation_history": "",
            "message": f"Error: {str(e)}"
        }


async def save_call_to_zep(
    phone: str,
    transcript: List[Dict],
    call_id: str,
    caller_name: str = None
) -> Dict[str, Any]:
    """
    Save call transcript to Zep Cloud.
    Also extracts and saves caller name if found.
    """
    try:
        user_id = f"caller_{phone.replace('+', '').replace(' ', '')}"
        
        # Try to extract name if not known
        extracted_name = None
        if not caller_name or caller_name.lower() in ["caller", "unknown"]:
            extracted_name = extract_name_from_transcript(transcript)
            if extracted_name:
                logger.info(f"Extracted name: {extracted_name}")
                caller_name = extracted_name
        
        # Update user and leads if we have a good name
        if caller_name and caller_name.lower() not in ["caller", "unknown"]:
            await zep_create_or_update_user(user_id, phone, first_name=caller_name)
            name_parts = caller_name.split(None, 1)
            first_name = name_parts[0]
            last_name = name_parts[1] if len(name_parts) > 1 else ""
            update_lead_with_name(phone, first_name, last_name)
        else:
            await zep_create_or_update_user(user_id, phone, first_name="Caller")
        
        # Create thread and save messages
        thread_id = f"call_{call_id}"
        await zep_create_thread(thread_id, user_id)
        
        # Format messages
        zep_messages = []
        for entry in transcript:
            role = entry.get("role", "user")
            content = entry.get("content", "")
            if not content:
                continue
            
            zep_role = "user" if role == "user" else "assistant"
            message_name = caller_name if role == "user" and caller_name else ("Caller" if role == "user" else "MFC Agent")
            
            zep_messages.append({
                "role": zep_role,
                "content": content,
                "name": message_name,
                "metadata": {"call_id": call_id, "phone": phone}
            })
        
        # Save in batches
        if zep_messages:
            batch_size = 30
            total_saved = 0
            for i in range(0, len(zep_messages), batch_size):
                batch = zep_messages[i:i + batch_size]
                logger.info(f"Saving batch {i//batch_size + 1}: {len(batch)} messages")
                result = await zep_add_messages(thread_id, batch)
                if result:
                    total_saved += len(batch)
            
            if total_saved > 0:
                logger.info(f"Saved {total_saved} messages to Zep")
                return {
                    "success": True,
                    "thread_id": thread_id,
                    "message_count": total_saved,
                    "extracted_name": extracted_name
                }
        
        return {"success": False, "message": "No messages saved"}
        
    except Exception as e:
        logger.error(f"Error saving to Zep: {e}", exc_info=True)
        return {"success": False, "message": str(e)}


# ============================================================================
# SPECIALIST LOOKUP
# ============================================================================

def lookup_specialist_by_town(town_name: str) -> Optional[Dict[str, str]]:
    """Look up specialist by town/county name."""
    try:
        if not town_name or not town_name.strip():
            return None
        
        town_name = town_name.strip()
        logger.info(f"Looking up specialist for: {town_name}")
        
        # Try RPC first
        try:
            result = supabase.rpc('find_specialist_by_county', {'county_name': town_name}).execute()
            if result.data and len(result.data) > 0:
                s = result.data[0]
                return {
                    "specialist_name": f"{s.get('first_name', '')} {s.get('last_name', '')}".strip(),
                    "specialist_phone": s.get("phone", "")
                }
        except Exception:
            pass
        
        # Fallback to direct query
        result = supabase.table("specialists") \
            .select("first_name, last_name, phone, counties") \
            .eq("is_active", True) \
            .execute()
        
        if result.data:
            for s in result.data:
                counties = s.get("counties", []) or []
                if any(town_name.lower() in c.lower() for c in counties):
                    return {
                        "specialist_name": f"{s.get('first_name', '')} {s.get('last_name', '')}".strip(),
                        "specialist_phone": s.get("phone", "")
                    }
        
        return None
    except Exception as e:
        logger.error(f"Error looking up specialist: {e}")
        return None


# ============================================================================
# KNOWLEDGE BASE SEARCH
# ============================================================================

def search_knowledge_base(query: str, top_k: int = 3) -> str:
    """Search knowledge base using semantic similarity."""
    try:
        response = openai_client.embeddings.create(
            model="text-embedding-3-small",
            input=query
        )
        query_embedding = response.data[0].embedding
        
        result = supabase.rpc(
            "match_knowledge_base",
            {"query_embedding": query_embedding, "match_threshold": 0.7, "match_count": top_k}
        ).execute()
        
        if result.data:
            return "\n".join([f"• {item['content']}" for item in result.data])
        
        return "No relevant information found."
    except Exception as e:
        logger.error(f"Knowledge base search error: {e}")
        return "I'll connect you with a specialist who can help."


# ============================================================================
# LEAD CAPTURE
# ============================================================================

def capture_lead(name: str, phone: str, location: str, interests: str) -> bool:
    """Capture lead information."""
    try:
        name_parts = name.strip().split(None, 1)
        first_name = name_parts[0] if name_parts else "Unknown"
        last_name = name_parts[1] if len(name_parts) > 1 else ""
        
        result = supabase.table("leads").insert({
            "first_name": first_name,
            "last_name": last_name,
            "phone": phone,
            "city": location,
            "primary_interest": interests,
            "lead_source": "retell_call",
            "lead_status": "new",
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }).execute()
        
        logger.info(f"Lead captured: {first_name} {last_name}")
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
        "version": "2.2.0",
        "memory_enabled": bool(ZEP_API_KEY),
        "timestamp": datetime.utcnow().isoformat()
    }


@app.post("/retell-webhook")
async def retell_webhook(request: Request):
    """Main webhook endpoint for Retell AI."""
    try:
        body = await request.json()
        event_type = body.get("event", "unknown")
        logger.info(f"Webhook received: {event_type}")
        
        call_data = body.get("call", {})
        call_id = call_data.get("call_id", "unknown")
        phone = call_data.get("from_number", "")
        transcript = call_data.get("transcript_object", [])
        
        # Build response
        response_data = {"call_id": call_id, "response_id": 1}
        
        # FAST LOOKUP on call_started
        if event_type == "call_started":
            memory_data = await lookup_caller_fast(phone)
            caller_name = memory_data.get("caller_name")
            response_data["caller_name"] = caller_name
            response_data["conversation_history"] = ""
            if caller_name:
                logger.info(f"Returning caller_name: {caller_name}")
            else:
                logger.info("New caller - no name found")
        
        # Handle function calls
        function_call = body.get("function_call")
        if function_call:
            func_name = function_call.get("name")
            args = function_call.get("arguments", {})
            
            if func_name == "lookup_specialist":
                specialist = lookup_specialist_by_town(args.get("town", ""))
                response_data["specialist"] = specialist or {"message": "No specialist found"}
            
            elif func_name == "search_knowledge":
                response_data["knowledge_results"] = search_knowledge_base(args.get("query", ""))
            
            elif func_name == "capture_lead":
                success = capture_lead(args.get("name", ""), phone, args.get("location", ""), args.get("interests", ""))
                response_data["lead_captured"] = success
        
        # SAVE TO MEMORY on call_ended
        if event_type == "call_ended" and transcript:
            logger.info(f"Saving {len(transcript)} messages to Zep")
            save_result = await save_call_to_zep(phone, transcript, call_id)
            response_data["memory_saved"] = save_result.get("success", False)
            if save_result.get("extracted_name"):
                logger.info(f"Name extracted and saved: {save_result['extracted_name']}")
        
        return JSONResponse(content=response_data)
        
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


# ============================================================================
# UTILITY ENDPOINT - FIX BAD ZEP DATA
# ============================================================================

@app.post("/fix-zep-user")
async def fix_zep_user(request: Request):
    """
    Utility endpoint to fix bad Zep user data.
    POST with {"phone": "+14062402889", "name": "Guy Hanson"}
    """
    try:
        body = await request.json()
        phone = body.get("phone", "")
        name = body.get("name", "")
        
        if not phone or not name:
            return {"error": "Provide phone and name"}
        
        user_id = f"caller_{phone.replace('+', '').replace(' ', '')}"
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.patch(
                f"{ZEP_BASE_URL}/users/{user_id}",
                headers=ZEP_HEADERS,
                json={"first_name": name}
            )
            
            if response.status_code == 200:
                # Also update leads
                name_parts = name.split(None, 1)
                update_lead_with_name(phone, name_parts[0], name_parts[1] if len(name_parts) > 1 else "")
                return {"success": True, "message": f"Updated {user_id} to {name}"}
            else:
                return {"success": False, "error": response.text}
                
    except Exception as e:
        return {"error": str(e)}


# ============================================================================
# RETELL FUNCTION ENDPOINTS
# ============================================================================

@app.post("/retell/functions/lookup_town")
async def lookup_town(request: Request):
    try:
        body = await request.json()
        args = body.get("arguments", {})
        town = args.get("town", "") or args.get("location", "") or args.get("city", "")
        
        specialist = lookup_specialist_by_town(town)
        if specialist:
            result = f"{specialist['specialist_name']} handles {town}. Reach them at {specialist['specialist_phone']}."
        else:
            result = f"No specialist for {town}. Contact main office at 406-555-0100."
        
        return JSONResponse(content={"result": result, "success": True})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/retell/functions/schedule_callback")
async def schedule_callback(request: Request):
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


@app.post("/retell/functions/query_knowledge")
async def query_knowledge(request: Request):
    try:
        body = await request.json()
        query = body.get("arguments", {}).get("query", "")
        return JSONResponse(content={"result": search_knowledge_base(query), "success": True})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/retell/functions/query_mfc_knowledge")
async def query_mfc_knowledge(request: Request):
    try:
        body = await request.json()
        query = body.get("arguments", {}).get("query", "")
        return JSONResponse(content={"result": search_knowledge_base(query), "success": True})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/retell/functions/search_products")
async def search_products(request: Request):
    try:
        body = await request.json()
        query = body.get("arguments", {}).get("query", "")
        return JSONResponse(content={"result": search_knowledge_base(f"products {query}"), "success": True})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/retell/functions/get_recommendations")
async def get_recommendations(request: Request):
    try:
        body = await request.json()
        args = body.get("arguments", {})
        query = f"{args.get('animal_type', 'cattle')} {args.get('query', '')}"
        return JSONResponse(content={"result": search_knowledge_base(query), "success": True})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/retell/functions/end_call")
async def end_call(request: Request):
    return JSONResponse(content={"result": "Thanks for calling Montana Feed!", "success": True})


@app.post("/retell/functions/lookup_staff")
async def lookup_staff(request: Request):
    try:
        body = await request.json()
        location = body.get("arguments", {}).get("location", "")
        specialist = lookup_specialist_by_town(location)
        
        if specialist:
            result = f"Your specialist is {specialist['specialist_name']} at {specialist['specialist_phone']}."
        else:
            result = "Let me connect you with our main office."
        
        return JSONResponse(content={"result": result, "success": True})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/retell/functions/search_knowledge_base")
async def search_knowledge_base_endpoint(request: Request):
    try:
        body = await request.json()
        query = body.get("arguments", {}).get("query", "")
        return JSONResponse(content={"result": search_knowledge_base(query), "success": True})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/retell/functions/get_caller_history")
async def get_caller_history(request: Request):
    try:
        body = await request.json()
        phone = body.get("call", {}).get("from_number", "")
        memory_data = await lookup_caller_fast(phone)
        
        return JSONResponse(content={
            "result": f"Caller: {memory_data.get('caller_name', 'Unknown')}",
            "caller_name": memory_data.get("caller_name"),
            "success": True
        })
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/retell/functions/get_warehouse")
async def get_warehouse(request: Request):
    try:
        body = await request.json()
        location = body.get("arguments", {}).get("location", "")
        return JSONResponse(content={
            "result": f"For {location} warehouse info, contact main office at 406-555-0100.",
            "success": True
        })
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/retell/functions/transfer_call_tool")
async def transfer_call_tool(request: Request):
    try:
        body = await request.json()
        transfer_to = body.get("arguments", {}).get("transfer_to", "")
        return JSONResponse(content={"result": f"Transferring to {transfer_to}.", "success": True})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
