"""
Montana Feed Company - Memory Skills (Zep Cloud Integration)
Caller recognition, transcript analysis, and conversation memory
Version 3.0.1 - Added automatic specialist lookup
"""

import asyncio
import re
import logging
from typing import Optional, Dict, List, Any

from config import (
    ZEP_API_KEY,
    ZEP_BASE_URL,
    ZEP_HEADERS,
    get_zep_client,
    normalize_phone,
    logger,
)
from .leads import update_lead_with_name

# Hold references to fire-and-forget tasks so asyncio doesn't GC them
# before they finish. Tasks remove themselves via the done_callback.
_background_tasks: set = set()


def _fire_and_forget(coro, label: str = "task") -> None:
    """Schedule a coroutine to run without blocking the caller. Exceptions
    are logged rather than silently swallowed."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        exc = t.exception()
        if exc is not None:
            logger.error(f"[BG] {label} failed: {exc}", exc_info=exc)

    task.add_done_callback(_on_done)

# ============================================================================
# ZEP CLOUD HTTP API FUNCTIONS (USING PERSISTENT CLIENT)
# ============================================================================

async def zep_get_user(user_id: str) -> Optional[Dict]:
    """Get a Zep user's details."""
    _zep_client = get_zep_client()
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
    _zep_client = get_zep_client()
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
            # Update name only — metadata is MERGED separately below to avoid
            # wiping existing fields like `specialist`, `last_topic`, etc.
            # Zep PATCH replaces metadata wholesale; we must merge by hand.
            response = await _zep_client.patch(
                f"{ZEP_BASE_URL}/users/{user_id}",
                headers=ZEP_HEADERS,
                json={"first_name": first_name},
            )
            if metadata:
                await zep_update_user_metadata(user_id, metadata)
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
    _zep_client = get_zep_client()
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
    _zep_client = get_zep_client()
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
    _zep_client = get_zep_client()
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

    # Connectors that the case-insensitive regex can capture as a bogus second
    # word (e.g. "my name is MacGregor and"). Trim these from the tail before
    # returning.
    trailing_connectors = {
        "and", "from", "over", "out", "here", "calling", "up", "in", "at",
        "with", "of", "on", "for", "to", "the", "a", "an",
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

                # Trim any trailing connector ("MacGregor and" -> "MacGregor")
                # that the case-insensitive regex may have pulled in.
                parts = name.split()
                while len(parts) > 1 and parts[-1].lower() in trailing_connectors:
                    parts.pop()
                name = " ".join(parts)

                # Don't `.title()` — the regex already captures `[A-Z][a-z]+`,
                # so internal capitalization ("McDonald", "O'Brien",
                # "MacCready") is preserved. Title-casing would mangle them.
                logger.info(f"Extracted name from transcript: {name}")
                return name

    return None


def extract_location_from_transcript(transcript: List[Dict]) -> Optional[str]:
    """Extract location from conversation transcript."""
    if not transcript:
        return None

    # Towns we recognize by exact substring match in lowercased transcripts.
    # Keep this aligned with MONTANA_TOWN_TO_COUNTY in skills/specialists.py —
    # any town here should also have a county mapping there so the specialist
    # lookup resolves.
    known_locations = [
        # Montana
        "polson", "missoula", "billings", "bozeman", "kalispell", "helena",
        "great falls", "butte", "havre", "miles city", "livingston", "whitefish",
        "columbia falls", "bigfork", "ronan", "st ignatius", "charlo",
        "dillon", "lewistown", "columbus", "glasgow", "glendive",
        # Wyoming — Riverton store service area
        "riverton", "lander", "dubois", "thermopolis", "worland",
        "shoshoni", "hudson", "pavillion",
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
        for location in known_locations:
            if location in message_lower:
                logger.info(f"Found known location in transcript: {location.title()}")
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
# MEMORY LOOKUP - WITH FULL CONTEXT RETRIEVAL AND AUTO-SPECIALIST LOOKUP
# ============================================================================

async def lookup_caller_fast(phone: str) -> Dict[str, Any]:
    """Fast caller lookup with memory context retrieval and automatic specialist assignment."""
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

                # AUTO-LOOKUP: If we have location but no specialist, look it up now.
                # The Supabase lookup stays on the hot path because we need the
                # specialist name for THIS call's dynamic vars. The Zep PATCH
                # (which just saves the result for next time) is fire-and-forget
                # so Retell gets its `call_inbound` response ~80ms sooner.
                if caller_location and not caller_specialist:
                    from .specialists import lookup_specialist_by_town
                    specialist_info = await lookup_specialist_by_town(caller_location)
                    if specialist_info:
                        caller_specialist = specialist_info["specialist_name"]
                        _fire_and_forget(
                            zep_update_user_metadata(user_id, {"specialist": caller_specialist}),
                            label=f"save_specialist({user_id})",
                        )
                        logger.info(f"[MEMORY] Auto-assigned specialist: {caller_specialist}")

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
            await update_lead_with_name(phone, first_name, last_name)
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
