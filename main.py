"""
Montana Feed Company - Retell AI Webhook with Zep Memory Integration
Version 2.6.0 - Optimized with all Priority 1-5 improvements
Changes:
- Fixed dynamic variables to always be strings
- Removed function handling from agent webhook (simplified)
- Added town→county resolution for Montana
- Auto-save specialist to Zep metadata
- Persistent HTTP client for better latency
"""

import os
import json
import logging
import re
from datetime import datetime
from typing import Optional, Dict, List, Any
from contextlib import asynccontextmanager

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

# IMPROVEMENT #5: Global persistent HTTP client for Zep (reduces latency)
_zep_client: Optional[httpx.AsyncClient] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan - setup and teardown."""
    global _zep_client
    
    # Startup: create persistent HTTP client
    _zep_client = httpx.AsyncClient(
        timeout=httpx.Timeout(5.0, connect=2.0),
        limits=httpx.Limits(max_keepalive_connections=5, max_connections=10)
    )
    logger.info("✓ Started persistent Zep HTTP client")
    
    yield
    
    # Shutdown: close client
    if _zep_client:
        await _zep_client.aclose()
        logger.info("✓ Closed Zep HTTP client")

# Initialize FastAPI with lifespan
app = FastAPI(
    title="Montana Feed Retell Webhook",
    lifespan=lifespan
)

# ============================================================================
# IMPROVEMENT #3: MONTANA TOWN → COUNTY RESOLUTION
# ============================================================================

MONTANA_TOWN_TO_COUNTY = {
    # Lake County
    "polson": "Lake County",
    "ronan": "Lake County",
    "st ignatius": "Lake County",
    "saint ignatius": "Lake County",
    "charlo": "Lake County",
    "pablo": "Lake County",
    
    # Missoula County
    "missoula": "Missoula County",
    "lolo": "Missoula County",
    "frenchtown": "Missoula County",
    "bonner": "Missoula County",
    "clinton": "Missoula County",
    
    # Flathead County
    "kalispell": "Flathead County",
    "whitefish": "Flathead County",
    "columbia falls": "Flathead County",
    "bigfork": "Flathead County",
    
    # Ravalli County
    "hamilton": "Ravalli County",
    "stevensville": "Ravalli County",
    "darby": "Ravalli County",
    
    # Sanders County
    "thompson falls": "Sanders County",
    "plains": "Sanders County",
    "hot springs": "Sanders County",
    
    # Lincoln County
    "libby": "Lincoln County",
    "troy": "Lincoln County",
    "eureka": "Lincoln County",
    
    # Gallatin County
    "bozeman": "Gallatin County",
    "belgrade": "Gallatin County",
    "manhattan": "Gallatin County",
    
    # Yellowstone County
    "billings": "Yellowstone County",
    "laurel": "Yellowstone County",
    
    # Cascade County
    "great falls": "Cascade County",
    
    # Lewis and Clark County
    "helena": "Lewis and Clark County",
    
    # Silver Bow County
    "butte": "Silver Bow County",
    
    # Add more as needed...
}

def resolve_town_to_county(location: str) -> str:
    """Convert town name to county, or return original if already a county."""
    if not location:
        return location
    
    location_lower = location.lower().strip()
    
    # Check if it's a known town
    if location_lower in MONTANA_TOWN_TO_COUNTY:
        county = MONTANA_TOWN_TO_COUNTY[location_lower]
        logger.info(f"[RESOLVE] '{location}' → '{county}'")
        return county
    
    # If it already says "County", assume it's a county
    if "county" in location_lower:
        return location
    
    # Otherwise try appending "County"
    return f"{location} County"

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def normalize_phone(phone: str) -> str:
    """Normalize phone number for consistent user IDs."""
    return phone.replace("+", "").replace(" ", "").replace("-", "")

# ============================================================================
# ZEP CLOUD HTTP API FUNCTIONS (USING PERSISTENT CLIENT)
# ============================================================================

async def zep_get_user(user_id: str) -> Optional[Dict]:
    """Get a Zep user's details."""
    if not ZEP_API_KEY or not _zep_client:
        return None
    try:
        response = await _zep_client.get(
            f"{ZEP_BASE_URL}/users/{user_id}",
            headers=ZEP_HEADERS
        )
        if response.status_code == 200:
            return response.json()
        return None
    except Exception as e:
        logger.error(f"Error getting Zep user: {e}")
        return None


async def zep_create_or_update_user(user_id: str, phone: str, first_name: str = "Caller", metadata: Dict = None) -> Optional[Dict]:
    """Create or update a Zep user with metadata."""
    if not ZEP_API_KEY or not _zep_client:
        return None
    try:
        user_data = {
            "user_id": user_id,
            "first_name": first_name,
            "metadata": metadata or {"phone": phone}
        }

        response = await _zep_client.post(
            f"{ZEP_BASE_URL}/users",
            headers=ZEP_HEADERS,
            json=user_data
        )

        if response.status_code in [200, 201]:
            logger.info(f"Created Zep user: {user_id} with name: {first_name}")
            return response.json()
        elif response.status_code == 400 and "already exists" in response.text:
            update_data = {"first_name": first_name}
            if metadata:
                update_data["metadata"] = metadata

            response = await _zep_client.patch(
                f"{ZEP_BASE_URL}/users/{user_id}",
                headers=ZEP_HEADERS,
                json=update_data
            )
            if response.status_code == 200:
                logger.info(f"Updated Zep user {user_id}")
                return response.json()
            return {"user_id": user_id, "exists": True}
        return None
    except Exception as e:
        logger.error(f"Error in zep_create_or_update_user: {e}")
        return None


async def zep_create_thread(thread_id: str, user_id: str) -> Optional[Dict]:
    """Create a new thread."""
    if not ZEP_API_KEY or not _zep_client:
        return None
    try:
        response = await _zep_client.post(
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
    if not ZEP_API_KEY or not _zep_client:
        return None
    try:
        response = await _zep_client.post(
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


async def zep_update_user_metadata(user_id: str, new_metadata: Dict) -> bool:
    """Update user metadata safely by merging with existing."""
    if not ZEP_API_KEY or not _zep_client:
        return False
    try:
        # Get current user data
        get_resp = await _zep_client.get(
            f"{ZEP_BASE_URL}/users/{user_id}",
            headers=ZEP_HEADERS
        )
        
        if get_resp.status_code == 200:
            user_data = get_resp.json()
            metadata = user_data.get("metadata", {}) or {}
            
            # Merge in new metadata
            metadata.update(new_metadata)
            
            # Update user
            patch_resp = await _zep_client.patch(
                f"{ZEP_BASE_URL}/users/{user_id}",
                headers=ZEP_HEADERS,
                json={"metadata": metadata}
            )
            
            if patch_resp.status_code == 200:
                logger.info(f"Updated Zep metadata for {user_id}: {new_metadata}")
                return True
        
        return False
    except Exception as e:
        logger.error(f"Error updating Zep metadata: {e}")
        return False


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
                if not any(c.isalpha() for c in name):
                    continue

                logger.info(f"Extracted name from transcript: {name}")
                return name.title()

    return None


def extract_location_from_transcript(transcript: List[Dict]) -> Optional[str]:
    """Extract location from conversation transcript."""
    if not transcript:
        return None

    montana_locations = [
        "polson", "missoula", "billings", "bozeman", "kalispell", "helena",
        "great falls", "butte", "havre", "miles city", "livingston", "whitefish",
        "columbia falls", "bigfork", "ronan", "st ignatius", "charlo"
    ]

    location_patterns = [
        r"(?:from|in|near|around|out of)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        r"(?:live in|located in|based in)\s+([A-Z][a-z]+)",
        r"(?:I'm|we're)\s+(?:in|at|from)\s+([A-Z][a-z]+)",
    ]

    user_messages = [
        msg.get("content", "")
        for msg in transcript[:15]
        if msg.get("role") == "user" and msg.get("content")
    ]

    for message in user_messages:
        message_lower = message.lower()
        for location in montana_locations:
            if location in message_lower:
                logger.info(f"Found Montana location in transcript: {location.title()}")
                return location.title()

        for pattern in location_patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                potential_location = match.group(1).strip()
                if len(potential_location) >= 3:
                    logger.info(f"Extracted location from transcript: {potential_location}")
                    return potential_location.title()

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
# MEMORY LOOKUP - WITH FULL CONTEXT RETRIEVAL
# ============================================================================

async def lookup_caller_fast(phone: str) -> Dict[str, Any]:
    """Fast caller lookup with memory context retrieval."""
    try:
        user_id = f"caller_{normalize_phone(phone)}"

        zep_user = await zep_get_user(user_id)

        caller_name = None
        caller_location = None
        caller_specialist = None
        conversation_context = ""

        if zep_user:
            zep_name = zep_user.get("first_name", "")
            if zep_name and zep_name.lower() not in ["caller", "unknown", "wondering", ""]:
                if not any(word in zep_name.lower() for word in ["wondering", "looking", "thinking", "calling"]):
                    caller_name = zep_name
                    logger.info(f"[MEMORY] Name: {caller_name}")

            metadata = zep_user.get("metadata", {})
            if metadata and isinstance(metadata, dict):
                caller_location = metadata.get("location") or metadata.get("city") or metadata.get("town")
                caller_specialist = metadata.get("specialist")

                if caller_location:
                    logger.info(f"[MEMORY] Location: {caller_location}")
                if caller_specialist:
                    logger.info(f"[MEMORY] Specialist: {caller_specialist}")

                context_parts = []
                if caller_location:
                    context_parts.append(f"Location: {caller_location}")
                if caller_specialist:
                    context_parts.append(f"Specialist: {caller_specialist}")
                if metadata.get("preferences"):
                    context_parts.append(f"Preferences: {metadata['preferences']}")
                if metadata.get("last_topic"):
                    context_parts.append(f"Last discussed: {metadata['last_topic']}")

                if context_parts:
                    conversation_context = " | ".join(context_parts)
                    logger.info(f"[MEMORY] Context: {conversation_context}")

        if not caller_name:
            logger.info("[MEMORY] New caller - no previous data")

        return {
            "found": caller_name is not None,
            "user_id": user_id,
            "caller_name": caller_name,
            "caller_location": caller_location,
            "caller_specialist": caller_specialist,
            "conversation_history": conversation_context,
            "message": f"Caller: {caller_name}" if caller_name else "New caller"
        }

    except Exception as e:
        logger.error(f"Error in lookup_caller_fast: {e}", exc_info=True)
        return {
            "found": False,
            "user_id": f"caller_{normalize_phone(phone)}",
            "caller_name": None,
            "caller_location": None,
            "caller_specialist": None,
            "conversation_history": "",
            "message": f"Error: {str(e)}"
        }


async def save_call_to_zep(phone: str, transcript: List[Dict], call_id: str, caller_name: str = None) -> Dict[str, Any]:
    """Save call transcript to Zep with metadata extraction."""
    if not ZEP_API_KEY:
        return {"success": False, "message": "Zep not configured"}

    try:
        user_id = f"caller_{normalize_phone(phone)}"

        extracted_name = None
        if not caller_name or caller_name.lower() in ["caller", "unknown", "new caller"]:
            extracted_name = extract_name_from_transcript(transcript)
            if extracted_name:
                logger.info(f"Extracted name: {extracted_name}")
                caller_name = extracted_name

        extracted_location = extract_location_from_transcript(transcript)

        metadata = {"phone": phone}
        if extracted_location:
            metadata["location"] = extracted_location
            logger.info(f"Extracted location: {extracted_location}")

        if caller_name and caller_name.lower() not in ["caller", "unknown", "new caller"]:
            await zep_create_or_update_user(user_id, phone, first_name=caller_name, metadata=metadata)

            name_parts = caller_name.split(None, 1)
            first_name = name_parts[0]
            last_name = name_parts[1] if len(name_parts) > 1 else ""
            update_lead_with_name(phone, first_name, last_name)
        else:
            await zep_create_or_update_user(user_id, phone, first_name="Caller", metadata=metadata)

        thread_id = f"call_{call_id}"
        await zep_create_thread(thread_id, user_id)

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
                    "extracted_name": extracted_name,
                    "extracted_location": extracted_location
                }

        return {"success": False, "message": "No messages saved"}

    except Exception as e:
        logger.error(f"Error saving to Zep: {e}", exc_info=True)
        return {"success": False, "message": str(e)}


# ============================================================================
# SPECIALIST LOOKUP (WITH TOWN→COUNTY RESOLUTION)
# ============================================================================

def lookup_specialist_by_town(town_name: str) -> Optional[Dict[str, str]]:
    """Look up specialist by town/county name with automatic town→county resolution."""
    if not supabase:
        logger.warning("[SPECIALIST] Supabase not configured")
        return None
    
    try:
        if not town_name or not town_name.strip():
            return None

        # IMPROVEMENT #3: Resolve town to county
        county_name = resolve_town_to_county(town_name.strip())
        logger.info(f"[SPECIALIST] Looking up: '{town_name}' → '{county_name}'")

        # Try RPC with resolved county name
        try:
            result = supabase.rpc('find_specialist_by_county', {'county_name': county_name}).execute()
            if result.data and len(result.data) > 0:
                s = result.data[0]
                specialist_info = {
                    "specialist_name": f"{s.get('first_name', '')} {s.get('last_name', '')}".strip(),
                    "specialist_phone": s.get("phone", ""),
                    "territory": county_name  # Include for context
                }
                logger.info(f"[SPECIALIST] Found via RPC: {specialist_info['specialist_name']}")
                return specialist_info
        except Exception as e:
            logger.warning(f"[SPECIALIST] RPC failed: {e}")

        # Fallback: table scan
        result = supabase.table("specialists") \
            .select("first_name, last_name, phone, counties") \
            .eq("is_active", True) \
            .execute()

        if result.data:
            for s in result.data:
                counties = s.get("counties", []) or []
                # Try both original and resolved names
                if any(town_name.lower() in c.lower() or county_name.lower() in c.lower() for c in counties):
                    specialist_info = {
                        "specialist_name": f"{s.get('first_name', '')} {s.get('last_name', '')}".strip(),
                        "specialist_phone": s.get("phone", ""),
                        "territory": county_name
                    }
                    logger.info(f"[SPECIALIST] Found via table: {specialist_info['specialist_name']}")
                    return specialist_info

        logger.info(f"[SPECIALIST] No match for: '{town_name}' or '{county_name}'")
        return None
        
    except Exception as e:
        logger.error(f"[SPECIALIST] Error: {e}")
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
        "version": "2.6.0",
        "memory_enabled": bool(ZEP_API_KEY),
        "supabase_enabled": supabase is not None,
        "persistent_client": _zep_client is not None,
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

            # IMPROVEMENT #1: Always include all variables as strings
            dynamic_vars = {
                "caller_name": caller_name if caller_name else "New caller",
                "is_returning": "true" if caller_name else "false",
                "conversation_history": conversation_history or "",
                "caller_location": caller_location or "",
                "caller_specialist": caller_specialist or "",
            }

            # Simplified logging - one line with all key data
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
    IMPROVEMENT #2: Simplified agent webhook - ONLY handles call_ended for analytics.
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
    """
    IMPROVEMENT #4: Now saves specialist to Zep metadata for future calls.
    """
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
            # IMPROVEMENT #4: Save specialist to Zep for future calls
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
    """Also saves specialist to Zep when found."""
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
