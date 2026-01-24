"""
Montana Feed Company - Lead Management Skills
Lead capture, lookup, and updates
"""

from datetime import datetime
from typing import Optional

from config import supabase, logger


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
