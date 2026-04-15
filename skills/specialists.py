"""
Montana Feed Company - Specialist Lookup Skills
7 Livestock Performance Specialists covering Montana + Wyoming

DB-touching functions (lookup_staff_by_name, lookup_specialist_by_town) are
`async` and offload the synchronous Supabase client to a worker thread via
`asyncio.to_thread`, so they don't block the FastAPI event loop. Pure helpers
(`is_lps`, `resolve_town_to_county`) stay synchronous.
"""

import asyncio
import logging
from typing import Optional, Dict

from config import supabase, logger

# ============================================================================
# CORRECTED MONTANA TOWN → COUNTY RESOLUTION (7 LPS)
# ============================================================================

MONTANA_TOWN_TO_COUNTY = {
    # SOUTHWEST MONTANA - Taylor Staudenmeyer (YELLOW + BROWN - took over Danielle's territory)
    "dillon": "Beaverhead County",
    "lima": "Beaverhead County",
    "dell": "Beaverhead County",
    "wisdom": "Beaverhead County",
    "jackson": "Beaverhead County",
    "ennis": "Madison County",
    "virginia city": "Madison County",
    "sheridan": "Madison County",
    "twin bridges": "Madison County",
    "alder": "Madison County",
    "boulder": "Jefferson County",
    "whitehall": "Jefferson County",
    "butte": "Silver Bow County",
    "anaconda": "Deer Lodge County",
    "deer lodge": "Deer Lodge County",
    "philipsburg": "Granite County",
    "hamilton": "Ravalli County",
    "stevensville": "Ravalli County",
    "darby": "Ravalli County",
    "victor": "Ravalli County",
    "corvallis": "Ravalli County",
    "superior": "Mineral County",
    "alberton": "Mineral County",
    
    # WESTERN MONTANA - Isabell Gilleard (DULL ORANGE)
    # Missoula, Ravalli, Lake, Flathead
    "missoula": "Missoula County",
    "lolo": "Missoula County",
    "frenchtown": "Missoula County",
    "bonner": "Missoula County",
    "clinton": "Missoula County",
    "seeley lake": "Missoula County",
    "thompson falls": "Sanders County",
    "plains": "Sanders County",
    "hot springs": "Sanders County",
    "trout creek": "Sanders County",
    "noxon": "Sanders County",
    "polson": "Lake County",
    "ronan": "Lake County",
    "st ignatius": "Lake County",
    "saint ignatius": "Lake County",
    "charlo": "Lake County",
    "pablo": "Lake County",
    "bigfork": "Lake County",
    "kalispell": "Flathead County",
    "whitefish": "Flathead County",
    "columbia falls": "Flathead County",
    "lakeside": "Flathead County",
    "somers": "Flathead County",
    "libby": "Lincoln County",
    "troy": "Lincoln County",
    "eureka": "Lincoln County",
    "fortine": "Lincoln County",
    
    # NORTH-CENTRAL MONTANA - Brady Johnson (DARK GREEN)
    "great falls": "Cascade County",
    "belt": "Cascade County",
    "neihart": "Cascade County",
    "cascade": "Cascade County",
    "simms": "Cascade County",
    "sun river": "Cascade County",
    "helena": "Lewis and Clark County",
    "east helena": "Lewis and Clark County",
    "augusta": "Lewis and Clark County",
    "lincoln": "Lewis and Clark County",
    "fort benton": "Chouteau County",
    "geraldine": "Chouteau County",
    "choteau": "Teton County",
    "fairfield": "Teton County",
    "dutton": "Teton County",
    "conrad": "Pondera County",
    "valier": "Pondera County",
    "cut bank": "Glacier County",
    "browning": "Glacier County",
    "shelby": "Toole County",
    "chester": "Liberty County",
    
    # NORTHEAST MONTANA - Austin Buzanowski (RED)
    "glasgow": "Valley County",
    "nashua": "Valley County",
    "malta": "Phillips County",
    "saco": "Phillips County",
    "scobey": "Daniels County",
    "plentywood": "Sheridan County",
    "wolf point": "Roosevelt County",
    "poplar": "Roosevelt County",
    "culbertson": "Roosevelt County",
    "havre": "Hill County",
    "chinook": "Blaine County",
    "harlem": "Blaine County",
    
    # CENTRAL MONTANA - Hannah Imer (BLUE)
    "lewistown": "Fergus County",
    "roy": "Fergus County",
    "grass range": "Fergus County",
    "winifred": "Fergus County",
    "stanford": "Judith Basin County",
    "hobson": "Judith Basin County",
    "geyser": "Judith Basin County",
    "harlowton": "Wheatland County",
    "two dot": "Wheatland County",
    "white sulphur springs": "Meagher County",
    "martinsdale": "Meagher County",
    "ryegate": "Golden Valley County",
    "lavina": "Golden Valley County",
    "roundup": "Musselshell County",
    "melstone": "Musselshell County",
    
    # SOUTHERN MONTANA/WYOMING - Kaylee Klaahsen (LIGHT GREEN/LIME)
    "billings": "Yellowstone County",
    "laurel": "Yellowstone County",
    "shepherd": "Yellowstone County",
    "huntley": "Yellowstone County",
    "columbus": "Stillwater County",
    "absarokee": "Stillwater County",
    "nye": "Stillwater County",
    "big timber": "Sweet Grass County",
    "greycliff": "Sweet Grass County",
    "livingston": "Park County",
    "gardiner": "Park County",
    "pray": "Park County",
    "clyde park": "Park County",
    "red lodge": "Carbon County",
    "bearcreek": "Carbon County",
    "bridger": "Carbon County",
    "bozeman": "Gallatin County",
    "belgrade": "Gallatin County",
    "manhattan": "Gallatin County",
    "three forks": "Gallatin County",
    "west yellowstone": "Gallatin County",
    "broadus": "Powder River County",
    "hardin": "Big Horn County",
    "crow agency": "Big Horn County",
    "lodge grass": "Big Horn County",
    "forsyth": "Rosebud County",
    "colstrip": "Rosebud County",
    "hysham": "Treasure County",
    "miles city": "Custer County",
    "ismay": "Custer County",
    "riverton": "Wyoming",
    
    # EASTERN MONTANA - Caitlin Lapicki (PURPLE)
    "jordan": "Garfield County",
    "circle": "McCone County",
    "glendive": "Dawson County",
    "sidney": "Richland County",
    "fairview": "Richland County",
    "savage": "Richland County",
    "terry": "Prairie County",
    "wibaux": "Wibaux County",
    "baker": "Fallon County",
    "plevna": "Fallon County",
    "ekalaka": "Carter County",
    
    # Common alternate names
    "msla": "Missoula County",
    "gt falls": "Cascade County",
    "gf": "Cascade County",
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


def is_lps(specialist: Dict) -> bool:
    """
    Is this specialist a Livestock Performance Specialist (live-transfer eligible)?

    Per 2026-04-09 design decision: only active LPSs get live call transfers.
    Non-LPS staff (managers, warehouse, corporate) are message-only.
    """
    if not specialist or not specialist.get("is_active"):
        return False
    role = (specialist.get("role") or "").lower()
    return "livestock performance" in role or role == "lps"


async def lookup_staff_by_name(name: str) -> list:
    """
    Fuzzy-match active staff in the `specialists` table by name.

    Matches against first_name, last_name, and the concatenated full name
    using case-insensitive ILIKE. Returns a list of matches (0, 1, or many).

    Accepts single names ("Sheryl"), full names ("Sheryl Shea"), or partials
    ("shea"). Inactive staff are excluded.

    Each result is a dict with: id, first_name, last_name, full_name, email,
    phone, role, specialties, counties, is_lps (live-transfer eligible).
    """
    if not supabase:
        logger.warning("[STAFF] Supabase not configured")
        return []

    if not name or not name.strip():
        return []

    query = name.strip()
    # Uppercase & lowercase variants both work — PostgREST ilike is case-insensitive.
    # Supabase Python client uses `%` wildcards and `or_` for disjunction.
    logger.info(f"[STAFF] Looking up by name: '{query}'")

    try:
        # Split into tokens so "Sheryl Shea" matches even if columns differ
        tokens = [t.strip() for t in query.split() if t.strip()]

        def _run_query():
            q = supabase.table("specialists") \
                .select("id, first_name, last_name, email, phone, role, specialties, counties, is_active") \
                .eq("is_active", True)

            if len(tokens) >= 2:
                first_tok, last_tok = tokens[0], tokens[-1]
                or_filter = (
                    f"and(first_name.ilike.%{first_tok}%,last_name.ilike.%{last_tok}%),"
                    f"and(first_name.ilike.%{last_tok}%,last_name.ilike.%{first_tok}%),"
                    f"first_name.ilike.%{query}%,"
                    f"last_name.ilike.%{query}%"
                )
                q = q.or_(or_filter)
            else:
                tok = tokens[0]
                or_filter = (
                    f"first_name.ilike.%{tok}%,"
                    f"last_name.ilike.%{tok}%"
                )
                q = q.or_(or_filter)

            return q.execute()

        result = await asyncio.to_thread(_run_query)
        rows = result.data or []

        # Normalize output for the voice agent
        matches = []
        for s in rows:
            full_name = f"{s.get('first_name','')} {s.get('last_name','')}".strip()
            matches.append({
                "id": s.get("id"),
                "first_name": s.get("first_name"),
                "last_name": s.get("last_name"),
                "full_name": full_name,
                "email": s.get("email"),
                "phone": s.get("phone"),
                "role": s.get("role"),
                "specialties": s.get("specialties") or [],
                "counties": s.get("counties") or [],
                "is_lps": is_lps(s),
            })

        logger.info(f"[STAFF] Found {len(matches)} match(es) for '{query}'")
        return matches

    except Exception as e:
        logger.error(f"[STAFF] lookup_staff_by_name error: {e}")
        return []


async def lookup_specialist_by_town(town_name: str) -> Optional[Dict[str, str]]:
    """Look up specialist by town/county name with automatic town→county resolution."""
    if not supabase:
        logger.warning("[SPECIALIST] Supabase not configured")
        return None

    try:
        if not town_name or not town_name.strip():
            return None

        # Resolve town to county
        county_name = resolve_town_to_county(town_name.strip())
        logger.info(f"[SPECIALIST] Looking up: '{town_name}' → '{county_name}'")

        # Try RPC with resolved county name
        try:
            result = await asyncio.to_thread(
                lambda: supabase.rpc('find_specialist_by_county', {'county_name': county_name}).execute()
            )
            if result.data and len(result.data) > 0:
                s = result.data[0]
                specialist_info = {
                    "specialist_name": f"{s.get('first_name', '')} {s.get('last_name', '')}".strip(),
                    "specialist_phone": s.get("phone", ""),
                    "specialist_email": s.get("email", ""),
                    "territory": county_name
                }
                logger.info(f"[SPECIALIST] Found via RPC: {specialist_info['specialist_name']}")
                return specialist_info
        except Exception as e:
            logger.warning(f"[SPECIALIST] RPC failed: {e}")

        # Fallback: table scan - NOW INCLUDING EMAIL
        result = await asyncio.to_thread(
            lambda: supabase.table("specialists")
                .select("first_name, last_name, phone, email, counties")
                .eq("is_active", True)
                .execute()
        )

        if result.data:
            for s in result.data:
                counties = s.get("counties", []) or []
                # Try both original and resolved names
                if any(town_name.lower() in c.lower() or county_name.lower() in c.lower() for c in counties):
                    specialist_info = {
                        "specialist_name": f"{s.get('first_name', '')} {s.get('last_name', '')}".strip(),
                        "specialist_phone": s.get("phone", ""),
                        "specialist_email": s.get("email", ""),
                        "territory": county_name
                    }
                    logger.info(f"[SPECIALIST] Found via table: {specialist_info['specialist_name']}")
                    return specialist_info

        logger.info(f"[SPECIALIST] No match for: '{town_name}' or '{county_name}'")
        return None
        
    except Exception as e:
        logger.error(f"[SPECIALIST] Error: {e}")
        return None
