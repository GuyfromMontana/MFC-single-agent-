"""
Montana Feed Company - Skills Package
Modular functions for voice agent capabilities
"""

from .memory import (
    lookup_caller_fast,
    save_call_to_zep,
    extract_name_from_transcript,
    extract_location_from_transcript,
    zep_get_user,
    zep_create_or_update_user,
    zep_create_thread,
    zep_add_messages,
    zep_update_user_metadata,
)

from .specialists import (
    MONTANA_TOWN_TO_COUNTY,
    resolve_town_to_county,
    lookup_specialist_by_town,
)

from .knowledge import (
    search_knowledge_base,
)

from .leads import (
    get_caller_name_from_leads,
    update_lead_with_name,
    capture_lead,
)

__all__ = [
    # Memory
    "lookup_caller_fast",
    "save_call_to_zep",
    "extract_name_from_transcript",
    "extract_location_from_transcript",
    "zep_get_user",
    "zep_create_or_update_user",
    "zep_create_thread",
    "zep_add_messages",
    "zep_update_user_metadata",
    # Specialists
    "MONTANA_TOWN_TO_COUNTY",
    "resolve_town_to_county",
    "lookup_specialist_by_town",
    # Knowledge
    "search_knowledge_base",
    # Leads
    "get_caller_name_from_leads",
    "update_lead_with_name",
    "capture_lead",
]
