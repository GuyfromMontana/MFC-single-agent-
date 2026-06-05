"""
Montana Feed Company - Product Catalog Skills

Backs two voice tools:
  * `search_products`     — caller names a product / category ("do you carry
                            range cubes?", "what minerals do you have?")
  * `get_recommendations` — caller describes a NEED and we suggest products
                            ("what should I feed for winter?", "something for
                            breeding cows", "fly control")

Both read the `products` table (19 active SKUs, all Beef Cattle today). The
`product_recommendations` table is a LOG of recommendations made to customers
(FK to products) — it is NOT a rules table, so recommendations are derived
here from the catalog itself.

DB-touching functions are async + offload the synchronous Supabase client to a
worker thread so they never block the FastAPI event loop.
"""

import asyncio
from typing import List, Dict

from config import supabase, logger

# Map common caller "needs" to the catalog. Each need has a set of trigger
# words (what a rancher might say) and the category/subcategory/keywords that
# should win. Scoring is additive: a product gets points for every trigger that
# appears in the caller's request AND matches the product's text.
#
# Kept intentionally readable — these are the real Purina/MFC use cases the
# Montana book sells against. Update alongside the `products` table.
_NEED_RULES = [
    {
        "triggers": ["winter", "drought", "cold", "snow", "dormant", "low quality hay",
                     "supplement protein", "protein supplement"],
        "match": ["protein supplement", "hi-pro", "xpc", "range cubes", "cake"],
    },
    {
        "triggers": ["breeding", "breed", "pregnant", "pregnancy", "gestation",
                     "lactation", "lactating", "repro", "reproduction", "calving",
                     "fertility", "conception", "preg"],
        "match": ["av4", "accuration", "breeding", "lactation", "mineral"],
    },
    {
        "triggers": ["bull", "heifer", "replacement"],
        "match": ["accuration", "bulls", "heifers"],
    },
    {
        "triggers": ["fly", "flies", "horn fly", "pest", "insect"],
        "match": ["fly control", "clarifly", "fly"],
    },
    {
        "triggers": ["mineral", "minerals", "trace mineral", "loose mineral"],
        "match": ["mineral", "wind & rain", "rangeland"],
    },
    {
        "triggers": ["range", "pasture", "grass", "summer", "grazing", "turnout"],
        "match": ["rangeland", "minimizer", "range cubes", "summer", "grass"],
    },
    {
        "triggers": ["wean", "weaning", "stress", "shipping", "ship", "receiving",
                     "sick", "health", "newly received", "backgrounding calves"],
        "match": ["stress tub", "stress", "calf starter"],
    },
    {
        "triggers": ["calf", "calves", "starter", "baby", "young", "creep"],
        "match": ["calf starter", "starter"],
    },
    {
        "triggers": ["show", "performance", "fair", "4-h", "4h", "ffa", "club calf",
                     "gain", "finish weight", "bloom"],
        "match": ["top gun", "show", "performance"],
    },
    {
        "triggers": ["energy", "finishing", "finish", "backgrounding", "fatten",
                     "feedlot", "drylot", "grain"],
        "match": ["corn", "barley", "grain", "energy", "cattle chow"],
    },
]


def _product_haystack(p: dict) -> str:
    return " ".join(str(p.get(k) or "") for k in (
        "product_name", "product_code", "brand", "category",
        "subcategory", "description",
    )).lower()


def _format_product(p: dict) -> str:
    """One-line spoken summary of a product for the agent."""
    parts = [p.get("product_name") or "Unknown product"]
    specs = []
    if p.get("protein_percentage"):
        specs.append(f"{int(float(p['protein_percentage']))}% protein")
    if p.get("unit_type"):
        specs.append(str(p["unit_type"]))
    if specs:
        parts.append(f"({', '.join(specs)})")
    if p.get("in_stock") is False:
        parts.append("- currently out of stock")
    return " ".join(parts)


async def _all_active_products() -> List[Dict]:
    if not supabase:
        logger.warning("[PRODUCTS] Supabase not configured")
        return []
    try:
        result = await asyncio.to_thread(
            lambda: supabase.table("products")
                .select("product_name, product_code, brand, category, subcategory, "
                        "livestock_type, protein_percentage, fat_percentage, "
                        "unit_type, in_stock, description, is_active")
                .eq("is_active", True)
                .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"[PRODUCTS] fetch error: {e}", exc_info=True)
        return []


async def search_products(query: str = "", category: str = "",
                          livestock_type: str = "", limit: int = 5) -> List[Dict]:
    """Text-search the catalog. Scores each active product against the query
    (name/code/brand/category/subcategory/description) plus optional category
    and livestock_type filters. Returns up to `limit` best matches.
    """
    products = await _all_active_products()
    if not products:
        return []

    q = (query or "").lower().strip()
    cat = (category or "").lower().strip()
    lt = (livestock_type or "").lower().strip()
    q_tokens = [t for t in q.split() if len(t) >= 3]

    scored = []
    for p in products:
        if lt and lt not in (p.get("livestock_type") or "").lower():
            continue
        if cat and cat not in (p.get("category") or "").lower() \
                and cat not in (p.get("subcategory") or "").lower():
            continue

        hay = _product_haystack(p)
        score = 0
        if q and q in hay:
            score += 10  # whole-phrase hit
        for tok in q_tokens:
            if tok in hay:
                score += 3
        # If only a category/livestock filter was given (no free-text query),
        # every product that survived the filter is a valid result.
        if not q:
            score += 1

        if score > 0:
            scored.append((p, score))

    scored.sort(key=lambda ps: ps[1], reverse=True)
    return [p for p, _ in scored[:limit]]


async def recommend_products(livestock_type: str = "", need: str = "",
                             limit: int = 3) -> List[Dict]:
    """Recommend products for a described NEED (e.g. "winter feeding",
    "breeding minerals", "fly control"). Maps the need to catalog categories
    via `_NEED_RULES`, then ranks active products by how strongly they match.
    Falls back to plain keyword scoring when no rule fires.
    """
    products = await _all_active_products()
    if not products:
        return []

    lt = (livestock_type or "").lower().strip()
    need_l = (need or "").lower().strip()

    # Collect the catalog match-words for every rule whose trigger appears in
    # the caller's need text.
    active_match_words: List[str] = []
    for rule in _NEED_RULES:
        if any(trig in need_l for trig in rule["triggers"]):
            active_match_words.extend(rule["match"])

    need_tokens = [t for t in need_l.split() if len(t) >= 4]

    scored = []
    for p in products:
        if lt and lt not in (p.get("livestock_type") or "").lower():
            continue
        hay = _product_haystack(p)
        score = 0
        # Strong signal: a fired need-rule points at this product.
        for mw in active_match_words:
            if mw in hay:
                score += 5
        # Weak signal: raw words from the caller's request match product text.
        for tok in need_tokens:
            if tok in hay:
                score += 2
        if score > 0:
            scored.append((p, score))

    scored.sort(key=lambda ps: ps[1], reverse=True)
    return [p for p, _ in scored[:limit]]
