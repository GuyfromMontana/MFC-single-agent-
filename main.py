"""
Montana Feed Company - Retell AI Webhook with Zep Memory Integration
Version 2.4.0 - Optimized for latency + fixed call_ended logic
"""

import os
import json
import logging
import re
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

# Validate critical env vars
if not SUPABASE_URL or not SUPABASE_KEY:
    logger.warning("Supabase not configured; lead features will be limited")
if not ZEP_API_KEY:
    logger.warning("Zep not configured; memory features disabled")

# Initialize clients
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None
openai_client = OpenAI(api_key=OPENAI_API_KEY, timeout=5.0, max_retries=1)

# Zep Cloud REST API configuration
ZEP_BASE_URL = "https://api.getzep.com/api/v2"
ZEP_HEADERS = {
    "Authorization": f"Api-Key {ZEP_API_KEY}",
    "Content-Type": "application/json"
}

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def normalize_phone(phone: str) -> str:
    """Normalize phone number for consistent user IDs."""
    return phone.replace("+", "").replace(" ", "").replace("-", "")

# ============================================================================
# ZEP CLOUD HTTP API FUNCTIONS
# ============================================================================

async def zep_get_user(user_id: str) -> Optional[Dict]:
    """Get a Zep user's details."""
    if not ZEP_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:  # Reduced from 5.0
            response = await client.get(f"{ZEP_BASE_URL}/users/{user_id}", headers=ZEP_HEADERS)
            if response.status_code == 200:
                return response.json()
            return None
    except Exception as e:
        logger.error(f"Error getting Zep user: {e}")
        return None


async def zep_create_or_update_user(user_id: str, phone: str, first_name: str = "Caller") -> Optional[Dict]:
    """Create or update a Zep user."""
    if not ZEP_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{ZEP_BASE_URL}/users",
                headers=ZEP_HEADERS,
                json={"user_id": user_id, "metadata": {"phone": phone}, "first_name": first_name}
            )
            
            if response.status_code in [200, 201]:
                logger.info(f"Created Zep user: {user_id} with name: {first_name}")
                return response.json()
            elif response.status_code == 400 and "already exists" in response.text:
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
            return None
    except Exception as e:
        logger.error(f"Error in zep_create_or_update_user: {e}")
        return None


async def zep_create_thread(thread_id: str, user_id: str) -> Optional[Dict]:
    """Create a new thread."""
    if not ZEP_API_KEY:
        return None
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
    """Add messages to a thread."""
    if not ZEP_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{ZEP_BASE_URL}/threads/{thread_id}/messages",
                headers=ZEP_HEADERS,
                json={"messages": messages}
            )
            if response.status_code in [200, 201]:
                return response.json()
            logger.warning(f"Zep add messages returned {response.status_code}: {response.text}")
            return None
    except Exception as e:
        logger.error(f"Error adding Zep messages: {e}")
        return None


# ============================================================================
# NAME EXTRACTION
# ============================================================================

def extract_name_from_transcript(transcript: List[Dict]) -> Optional[str]:
    """Extract caller's name from conversation transcript."""
    if not transcript:
        return None
    
    skip_words = {
        "good", "fine", "great", "well", "okay", "ok", "alright",
        "here", "calling", "looking", "interested", "wondering", 
        "thinking", "trying", "wanting", "needing", "hoping",
        "just", "actually", "really", "very", "pretty",
        "hello", "hi", "hey", "morning", "afternoon", "evening",
        "what", "who", "where", "when", "why", "how",
        "glad", "happy", "pleased", "sure", "ready",
        "new", "old", "young", "local", "nearby",
        "customer", "caller", "rancher", "farmer", "producer",
    }
    
    name_patterns = [
        r"my name is\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        r"this is\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+calling",
        r"(?:^|\.\s+)I'?m\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)(?:\s*[,.]|\s+and\s|\s+from\s|\s+over\s|\s+out\s|\s+here\s|$)",
        r"call me\s+([A-Z][a-z]+)",
        r"the name is\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
    ]
    
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
                first_word = name.split()[0].lower() if name else ""
                
                if first_word in skip_words:
                    continue
                if len(name) < 2 or len(name) > 40:
                    continue
                # Allow all-lowercase names from ASR transcripts
                if not any(c.isalpha() for c in name):
                    continue
                
                logger.info(f"Extracted name from transcript: {name}")
                return name.title()
    
    return None


# ============================================================================
# CALLER NAME LOOKUP
# ============================================================================

def get_caller_name_from_leads(phone: str) -> Optional[str]:
    """Look up caller name from leads table."""
    if not supabase:
        return None
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
                return f"{first_name} {last_name}".strip() if last_name else first_name
        return None
    except Exception as e:
        logger.error(f"Error looking up name in leads: {e}")
        return None


def update_lead_with_name(phone: str, first_name: str, last_name: str = "") -> bool:
    """Update or create a lead record with the caller's name."""
    if not supabase:
        return False
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
# MEMORY LOOKUP - ULTRA-FAST VERSION (ZEP ONLY)
# ============================================================================

async def lookup_caller_fast(phone: str) -> Dict[str, Any]:
    """ULTRA-FAST caller lookup - Zep only for minimum latency."""
    try:
        user_id = f"caller_{normalize_phone(phone)}"
        
        # Check Zep user only (skip Supabase to reduce latency)
        zep_user = await zep_get_user(user_id)
        
        caller_name = None
        if zep_user:
            zep_name = zep_user.get("first_name", "")
            if zep_name and zep_name.lower() not in ["caller", "unknown", "wondering", ""]:
                if not any(word in zep_name.lower() for word in ["wondering", "looking", "thinking", "calling"]):
                    caller_name = zep_name
                    logger.info(f"Found name in Zep: {caller_name}")
        
        if not caller_name:
            logger.info("New caller - no name found")
        
        return {
            "found": caller_name is not None,
            "user_id": user_id,
            "caller_name": caller_name,
            "conversation_history": "",
            "message": f"Caller: {caller_name}" if caller_name else "New caller"
        }
        
    except Exception as e:
        logger.error(f"Error in lookup_caller_fast: {e}", exc_info=True)
        return {
            "found": False,
            "user_id": f"caller_{normalize_phone(phone)}",
            "caller_name": None,
            "conversation_history": "",
            "message": f"Error: {str(e)}"
        }


async def save_call_to_zep(phone: str, transcript: List[Dict], call_id: str, caller_name: str = None) -> Dict[str, Any]:
    """Save call transcript to Zep Cloud."""
    if not ZEP_API_KEY:
        return {"success": False, "message": "Zep not configured"}
    
    try:
        user_id = f"caller_{normalize_phone(phone)}"
        
        # Try to extract name if not known
        extracted_name = None
        if not caller_name or caller_name.lower() in ["caller", "unknown", "new caller"]:
            extracted_name = extract_name_from_transcript(transcript)
            if extracted_name:
                logger.info(f"Extracted name: {extracted_name}")
                caller_name = extracted_name
        
        # Update user and leads if we have a good name
        if caller_name and caller_name.lower() not in ["caller", "unknown", "new caller"]:
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
                return {"success": True, "thread_id": thread_id, "message_count": total_saved, "extracted_name": extracted_name}
        
        return {"success": False, "message": "No messages saved"}
        
    except Exception as e:
        logger.error(f"Error saving to Zep: {e}", exc_info=True)
        return {"success": False, "message": str(e)}


# ============================================================================
# SPECIALIST LOOKUP
# ============================================================================

def lookup_specialist_by_town(town_name: str) -> Optional[Dict[str, str]]:
    """Look up specialist by town/county name."""
    if not supabase:
        return None
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
        
        # Fallback
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
    if not supabase:
        return "Knowledge base unavailable."
    try:
        response = openai_client.embeddings.create(model="text-embedding-3-small", input=query)
        query_embedding = response.data[0].embedding
        
        result = supabase.rpc(
            "match_knowledge_base",
            {"query_embedding": query_embedding, "match_threshold": 0.7, "match_count": top_k}
        ).execute()
        
        if result.data:
            # Truncate to prevent token bloat
            return "\n".join([f"• {item['content'][:500]}" for item in result.data])
        
        return "No relevant information found."
    except Exception as e:
        logger.error(f"Knowledge base search error: {e}")
        return "I'll connect you with a specialist who can help."


# ============================================================================
# LEAD CAPTURE
# ============================================================================

def capture_lead(name: str, phone: str, location: str, interests: str) -> bool:
    """Capture lead information."""
    if not supabase:
        logger.warning("Cannot capture lead - Supabase not configured")
        return False
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
        "version": "2.4.0",
        "memory_enabled": bool(ZEP_API_KEY),
        "supabase_enabled": supabase is not None,
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
        
        # Early exit if no phone on call_started
        if event_type == "call_started" and not phone:
            logger.warning("call_started without from_number")
            return JSONResponse(content={"call_id": call_id, "response_id": 1})
        
        # Build response
        response_data = {"call_id": call_id, "response_id": 1}
        
        # ULTRA-FAST LOOKUP on call_started - Zep only
        if event_type == "call_started" and phone:
            memory_data = await lookup_caller_fast(phone)
            caller_name = memory_data.get("caller_name")
            
            # Retell requires retell_llm_dynamic_variables with all string values
            response_data["retell_llm_dynamic_variables"] = {
                "caller_name": caller_name if caller_name else "New caller",
                "conversation_history": memory_data.get("conversation_history", "") or "",
                "is_returning": "true" if caller_name else "false"
            }
            
            logger.info(f"[FAST] Returning caller_name: {caller_name or 'New caller'} in {response_data.get('lookup_ms', '?')}ms")
        
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
        
        # SAVE TO MEMORY on call_ended - FIX: Re-lookup caller_name properly
        if event_type == "call_ended" and transcript and phone:
            logger.info(f"Saving {len(transcript)} messages to Zep")
            
            # Get caller_name from Retell's payload or re-lookup
            caller_name = body.get("retell_llm_dynamic_variables", {}).get("caller_name")
            if not caller_name or caller_name == "New caller":
                # Fallback: quick re-lookup
                memory_data = await lookup_caller_fast(phone)
                caller_name = memory_data.get("caller_name")
            
            save_result = await save_call_to_zep(phone, transcript, call_id, caller_name)
            response_data["memory_saved"] = save_result.get("success", False)
            if save_result.get("extracted_name"):
                logger.info(f"Name extracted and saved: {save_result['extracted_name']}")
        
        return JSONResponse(content=response_data)
        
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": "Internal server error"})


# ============================================================================
# UTILITY ENDPOINT - FIX BAD ZEP DATA
# ============================================================================

@app.post("/fix-zep-user")
async def fix_zep_user(request: Request):
    """Fix bad Zep user data. POST with {"phone": "+14062402889", "name": "Guy Hanson"}"""
    try:
        body = await request.json()
        phone = body.get("phone", "")
        name = body.get("name", "")
        
        if not phone or not name:
            return {"error": "Provide phone and name"}
        
        user_id = f"caller_{normalize_phone(phone)}"
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.patch(
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


@app.post("/retell/functions/search_knowledge_base")
async def search_knowledge_base_endpoint(request: Request):
    """Main knowledge search - consolidates multiple similar functions."""
    try:
        body = await request.json()
        query = body.get("arguments", {}).get("query", "")
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


# REMOVED: get_caller_history function - redundant with dynamic variables
# If agent calls this, it means dynamic variables aren't working


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
