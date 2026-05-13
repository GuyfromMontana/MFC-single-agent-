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
import re
from typing import Optional, Dict

from config import supabase, logger

# Whitelist of characters allowed in a staff-name search. Everything else is
# stripped before the value is interpolated into a PostgREST `or_()` filter —
# `,`, `(`, `)`, `.`, `%`, and `*` are all meaningful to PostgREST filter
# syntax or ILIKE, so letting them through risks filter injection or wildcard
# abuse from ASR output / callers.
_NAME_ALLOWED = re.compile(r"[^A-Za-z\-' ]+")


def _sanitize_name(value: str) -> str:
    """Strip anything that isn't a letter, space, hyphen, or apostrophe."""
    if not value:
        return ""
    return _NAME_ALLOWED.sub("", value).strip()

# ============================================================================
# CORRECTED MONTANA TOWN → COUNTY RESOLUTION (7 LPS)
# ============================================================================

#
# IMPORTANT (2026-04-22 reassignments — keep in sync with Supabase
# `specialists.counties`, which is the actual routing source of truth):
#   - Isabell Gilleard moved Miles City → Columbus area (medium herds)
#   - Hannah Imer is the Columbus lead (large herds + feedlots)
#   - Kaylee Klaahsen now covers Miles City in addition to S-Central MT + WY
#   - NW MT (Missoula/Bitterroot/Flathead/Lincoln) is owned by Sheryl Shea
#     — she is NOT an LPS, so calls from those counties go to message-only,
#     not live transfer
# The section headers below describe geography, not current ownership.

MONTANA_TOWN_TO_COUNTY = {
    # SOUTHWEST MONTANA — Taylor Staudenmeyer
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
    
    # WESTERN MONTANA — Sheryl Shea (operations manager, message-only — NOT live-transfer)
    # Isabell Gilleard was reassigned away from this region on 2026-04-22.
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
    
    # NORTH-CENTRAL MONTANA — Brady Johnson
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
    
    # NORTHEAST MONTANA — Austin Buzanowski
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
    
    # CENTRAL MONTANA — Brady Johnson services Lewistown area as of 2026-04-22
    # (Hannah Imer reassigned to Columbus lead). Routing follows Supabase
    # `specialists.counties` regardless of this comment.
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
    
    # SOUTHERN MONTANA + WYOMING — Kaylee Klaahsen
    # Stillwater County (Columbus, Absarokee, Nye) is co-served by Hannah Imer
    # + Isabell Gilleard per 2026-04-22 reassignment.
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
    # Wyoming towns served from Riverton store
    "riverton": "Wyoming",
    "lander": "Wyoming",
    "dubois": "Wyoming",
    "thermopolis": "Wyoming",
    "worland": "Wyoming",
    "shoshoni": "Wyoming",
    "hudson": "Wyoming",
    "pavillion": "Wyoming",
    "buffalo": "Wyoming",

    # EASTERN MONTANA — Caitlin Lapicki
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

    Accepts single names ("Sheryl"), full names ("Sheryl Shea"), or partials
    ("shea"). Inactive staff are excluded. Returns 0, 1, or many matches.

    Each result is a dict with: id, first_name, last_name, full_name, email,
    phone, role, specialties, counties, is_lps (live-transfer eligible).

    Implementation: pulls ALL active specialists in one query (~13 rows) and
    does case-insensitive substring matching in Python. This is intentional:
    earlier PostgREST `or_()` filters with `%name%` patterns and nested
    `and(...)` clauses were unreliable for multi-word queries (the embedded
    space in "Sheryl Shea" + nested commas confused PostgREST URL parsing
    and caused real production misses where ASR-correct names returned 0
    matches even though the row clearly existed). With ~13 rows the
    table-scan + python-filter is fast, easy to reason about, and never
    silently fails on edge cases.
    """
    if not supabase:
        logger.warning("[STAFF] Supabase not configured")
        return []

    if not name or not name.strip():
        return []

    # Sanitize: strip anything that isn't a plausible name character so
    # ASR garbage or punctuation can't sneak past.
    query = _sanitize_name(name)
    if not query:
        logger.warning(f"[STAFF] Name sanitized to empty — original: {name!r}")
        return []

    logger.info(f"[STAFF] Looking up by name: '{query}'")

    try:
        # Pull everything active in one shot. ~13 rows; trivial.
        def _run_query():
            return (
                supabase.table("specialists")
                .select("id, first_name, last_name, email, phone, role, specialties, counties, is_active")
                .eq("is_active", True)
                .execute()
            )

        result = await asyncio.to_thread(_run_query)
        rows = result.data or []

        # Tokens for matching. Most callers say "first last" but we also
        # handle single names ("Sheryl") and partials ("shea").
        tokens = [_sanitize_name(t).lower() for t in query.split() if _sanitize_name(t)]
        if not tokens:
            return []

        query_lower = query.lower()
        matches = []
        for s in rows:
            first = (s.get("first_name") or "").strip()
            last = (s.get("last_name") or "").strip()
            first_lower = first.lower()
            last_lower = last.lower()
            full_lower = f"{first_lower} {last_lower}".strip()

            # Match conditions (any one wins):
            # 1. Full query substring of full name (the natural case for
            #    "Sheryl Shea" being asked about "Sheryl Shea").
            # 2. Each token matches first OR last (handles ASR mishears
            #    where one of two tokens is wrong — e.g. "Cheryl Shea":
            #    "Cheryl" matches nothing, "Shea" matches Sheryl Shea's
            #    last name, so we count it).
            # 3. Single-token query that matches first or last name.
            matched = False
            if query_lower and query_lower in full_lower:
                matched = True
            else:
                token_hits = sum(
                    1 for t in tokens
                    if (t in first_lower) or (t in last_lower)
                )
                if len(tokens) == 1 and token_hits >= 1:
                    matched = True
                elif len(tokens) >= 2 and token_hits >= 1:
                    # At least one token of a multi-word query matched —
                    # forgiving of ASR errors. Caller can disambiguate
                    # downstream if multiple specialists match.
                    matched = True

            if matched:
                matches.append({
                    "id": s.get("id"),
                    "first_name": s.get("first_name"),
                    "last_name": s.get("last_name"),
                    "full_name": f"{first} {last}".strip(),
                    "email": s.get("email"),
                    "phone": s.get("phone"),
                    "role": s.get("role"),
                    "specialties": s.get("specialties") or [],
                    "counties": s.get("counties") or [],
                    "is_lps": is_lps(s),
                })

        logger.info(f"[STAFF] Found {len(matches)} match(es) for '{query}': "
                    f"{[m['full_name'] for m in matches]}")
        return matches

    except Exception as e:
        logger.error(f"[STAFF] lookup_staff_by_name error: {e}", exc_info=True)
        return []


async def lookup_specialist_by_town(town_name: str) -> Optional[Dict[str, str]]:
    """Look up specialist by town/county name with automatic town→county resolution.

    Returns a dict including `is_lps`, which callers should check before
    attempting a live transfer — non-LPS staff (e.g. Sheryl Shea covering
    NW MT) are message-only.
    """
    if not supabase:
        logger.warning("[SPECIALIST] Supabase not configured")
        return None

    try:
        if not town_name or not town_name.strip():
            return None

        county_name = resolve_town_to_county(town_name.strip())
        logger.info(f"[SPECIALIST] Looking up: '{town_name}' → '{county_name}'")

        # Table scan — needed so we can read `role` and `is_active` to compute
        # is_lps. The RPC `find_specialist_by_county` doesn't return those
        # fields, so a non-LPS like Sheryl Shea would silently look like an LPS
        # via the RPC path and the agent would try to live-transfer her. With
        # ~13 specialists this scan is cheap.
        result = await asyncio.to_thread(
            lambda: supabase.table("specialists")
                .select("id, first_name, last_name, phone, email, role, specialties, counties, is_active")
                .eq("is_active", True)
                .execute()
        )

        if result.data:
            for s in result.data:
                counties = s.get("counties", []) or []
                if any(town_name.lower() in c.lower() or county_name.lower() in c.lower() for c in counties):
                    full_name = f"{s.get('first_name', '')} {s.get('last_name', '')}".strip()
                    specialist_info = {
                        "id": s.get("id"),
                        "specialist_name": full_name,
                        "specialist_phone": s.get("phone", ""),
                        "specialist_email": s.get("email", ""),
                        "role": s.get("role", ""),
                        "specialties": s.get("specialties") or [],
                        "territory": county_name,
                        "is_lps": is_lps(s),
                    }
                    logger.info(
                        f"[SPECIALIST] Found: {full_name} "
                        f"(role={s.get('role')}, is_lps={specialist_info['is_lps']})"
                    )
                    return specialist_info

        logger.info(f"[SPECIALIST] No match for: '{town_name}' or '{county_name}'")
        return None

    except Exception as e:
        logger.error(f"[SPECIALIST] Error: {e}")
        return None
