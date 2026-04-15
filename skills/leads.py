"""
Montana Feed Company - Lead Management Skills
Lead capture, lookup, and updates.

All DB-touching functions are `async` and offload the synchronous Supabase
client to a worker thread via `asyncio.to_thread`, so they don't block the
FastAPI event loop.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from config import supabase, logger


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_caller_name_from_leads(phone: str) -> Optional[str]:
    """Look up caller name from leads table."""
    if not supabase:
        return None
    try:
        result = await asyncio.to_thread(
            lambda: supabase.table("leads")
                .select("first_name, last_name")
                .eq("phone", phone)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
        )

        if result.data and len(result.data) > 0:
            lead = result.data[0]
            first_name = (lead.get("first_name") or "").strip()
            last_name = (lead.get("last_name") or "").strip()

            if first_name and first_name.lower() not in ["unknown", "caller", ""]:
                return f"{first_name} {last_name}".strip() if last_name else first_name
        return None
    except Exception as e:
        logger.error(f"Error looking up name in leads: {e}")
        return None


async def update_lead_with_name(phone: str, first_name: str, last_name: str = "") -> bool:
    """Update or create a lead record with the caller's name."""
    if not supabase:
        return False
    try:
        existing = await asyncio.to_thread(
            lambda: supabase.table("leads")
                .select("id, first_name")
                .eq("phone", phone)
                .limit(1)
                .execute()
        )

        if existing.data and len(existing.data) > 0:
            lead = existing.data[0]
            current_name = (lead.get("first_name") or "").lower()
            if not current_name or current_name in ["unknown", "caller"]:
                await asyncio.to_thread(
                    lambda: supabase.table("leads")
                        .update({
                            "first_name": first_name,
                            "last_name": last_name,
                            "updated_at": _now_iso(),
                        })
                        .eq("id", lead["id"])
                        .execute()
                )
                logger.info(f"Updated lead {phone} with name: {first_name} {last_name}")
                return True
        else:
            await asyncio.to_thread(
                lambda: supabase.table("leads").insert({
                    "first_name": first_name,
                    "last_name": last_name,
                    "phone": phone,
                    "lead_source": "retell_call",
                    "lead_status": "new",
                    "created_at": _now_iso(),
                    "updated_at": _now_iso(),
                }).execute()
            )
            logger.info(f"Created new lead for {phone}: {first_name} {last_name}")
            return True
        return False
    except Exception as e:
        logger.error(f"Error updating lead with name: {e}")
        return False


async def capture_lead(name: str, phone: str, location: str, interests: str) -> bool:
    """Capture lead information."""
    if not supabase:
        logger.warning("Cannot capture lead - Supabase not configured")
        return False
    try:
        name_parts = name.strip().split(None, 1)
        first_name = name_parts[0] if name_parts else "Unknown"
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        result = await asyncio.to_thread(
            lambda: supabase.table("leads").insert({
                "first_name": first_name,
                "last_name": last_name,
                "phone": phone,
                "city": location,
                "primary_interest": interests,
                "lead_source": "retell_call",
                "lead_status": "new",
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            }).execute()
        )

        logger.info(f"Lead captured: {first_name} {last_name}")
        return bool(result.data)
    except Exception as e:
        logger.error(f"Error capturing lead: {e}")
        return False


async def create_message_for_specialist(
    specialist_id: Optional[str],
    specialist_name: Optional[str],
    specialist_email: Optional[str],
    caller_name: Optional[str],
    caller_phone: Optional[str],
    message: str,
    reason: str = "message",
) -> Optional[str]:
    """
    Create a callback row in the `callbacks` table representing a message
    left by a caller for a specific staff member.

    The existing `callbacks` table has exactly the right columns for this
    (specialist_id, specialist_email, specialist_assigned, caller_phone,
    caller_name, reason, notes, status). The `schedule_callback` tool
    historically dumped everything into `leads` instead — this function is
    the correct path for any "please tell X that..." flow.

    Returns the new callback row id on success, None on failure.
    """
    if not supabase:
        logger.warning("[MESSAGE] Cannot create message - Supabase not configured")
        return None
    try:
        payload = {
            "caller_phone": caller_phone or "unknown",
            "caller_name": caller_name,
            "specialist_id": specialist_id,
            "specialist_email": specialist_email,
            "specialist_assigned": specialist_name,
            "reason": reason,
            "notes": message,
            "status": "pending",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        result = await asyncio.to_thread(
            lambda: supabase.table("callbacks").insert(payload).execute()
        )
        if result.data and len(result.data) > 0:
            row_id = result.data[0].get("id")
            logger.info(
                f"[MESSAGE] Created callback {row_id} for "
                f"{specialist_name or 'unknown'} from {caller_name or caller_phone}"
            )
            return row_id
        return None
    except Exception as e:
        logger.error(f"[MESSAGE] Error creating callback: {e}")
        return None
