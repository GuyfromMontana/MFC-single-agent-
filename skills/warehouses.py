"""
Montana Feed Company - Warehouse / Store Lookup Skill

Backs the `get_warehouse` voice tool. Callers ask things like "what are the
hours in Dillon?", "where's your Columbus store?", or "do you have a location
near Red Lodge?". They give us a city, a warehouse code (DL/MC/LT/CB/RV), a
region, or a county/town that's only mentioned inside a store's service area.

There are only 5 active warehouses, so we pull them all in one query and do
flexible Python-side scoring rather than trying to express fuzzy matching in
PostgREST. This mirrors the deliberate table-scan approach used in
`specialists.py` (small table, robust matching > clever SQL).

DB-touching function is async + offloads the synchronous Supabase client to a
worker thread so it never blocks the FastAPI event loop.
"""

import asyncio
from typing import Optional, Dict, List

from config import supabase, logger
from .specialists import resolve_town_to_county


def _score_warehouse(w: dict, terms: List[str]) -> int:
    """Score how well a warehouse matches the caller's search terms.

    Higher is better; 0 means no match. We check the most specific signals
    first (warehouse code, exact city) and fall back to looser ones (region,
    service-area text, town→county resolution).
    """
    city = (w.get("city") or "").lower().strip()
    name = (w.get("warehouse_name") or "").lower()
    code = (w.get("warehouse_code") or "").lower().strip()
    region = (w.get("region") or "").lower()
    service = (w.get("service_area_description") or "").lower()

    best = 0
    for raw in terms:
        t = (raw or "").lower().strip()
        if not t:
            continue

        score = 0
        # Exact 2-letter warehouse code ("DL", "RV").
        if code and t == code:
            score = max(score, 100)
        # Exact city ("Dillon" -> Dillon store).
        if city and t == city:
            score = max(score, 95)
        # Store name contains the term ("Riverton" lives in warehouse_name
        # even though the city is technically Shoshoni).
        if t in name:
            score = max(score, 80)
        # Loose city overlap either direction.
        if city and (t in city or city in t):
            score = max(score, 70)
        # Region ("Southwest Montana").
        if region and t in region:
            score = max(score, 45)
        # County / town mentioned in the service-area blurb.
        if service and t in service:
            score = max(score, 40)
        # Resolve a town to its county and see if that county is served.
        county = resolve_town_to_county(raw).lower() if raw else ""
        if county and county != t:
            county_word = county.replace(" county", "").strip()
            if county_word and county_word in service:
                score = max(score, 50)

        best = max(best, score)

    return best


async def lookup_warehouse(terms: List[str]) -> Optional[Dict]:
    """Find the single best-matching active warehouse for the caller's
    search terms (city, code, region, county, or town). Returns the raw
    warehouse row dict, or None if nothing scored above zero.
    """
    if not supabase:
        logger.warning("[WAREHOUSE] Supabase not configured")
        return None

    cleaned = [t for t in (terms or []) if t and t.strip()]
    if not cleaned:
        return None

    logger.info(f"[WAREHOUSE] Looking up warehouse for terms: {cleaned}")

    try:
        result = await asyncio.to_thread(
            lambda: supabase.table("warehouses")
                .select("warehouse_name, warehouse_code, city, region, address, "
                        "phone, manager_name, operating_hours, "
                        "service_area_description, is_active")
                .eq("is_active", True)
                .execute()
        )
        rows = result.data or []

        scored = [(w, _score_warehouse(w, cleaned)) for w in rows]
        scored = [(w, s) for w, s in scored if s > 0]
        if not scored:
            logger.info(f"[WAREHOUSE] No warehouse matched {cleaned}")
            return None

        scored.sort(key=lambda ws: ws[1], reverse=True)
        best, score = scored[0]
        logger.info(
            f"[WAREHOUSE] Best match: {best.get('warehouse_name')} "
            f"(score={score})"
        )
        return best

    except Exception as e:
        logger.error(f"[WAREHOUSE] lookup_warehouse error: {e}", exc_info=True)
        return None
