"""
Montana Feed Company - Customer Lookup Skills

Phone-indexed customer lookup against the Supabase `caller_contacts`
table — which is populated by the Eagle Bridge `customer_sync` job and
holds phone -> customer_name + primary_warehouse + territory mappings
for ~1,280 known callers.

This is Phase 1 of the customer-aware routing work:

    caller phone in --> caller_contacts (Supabase, local)
                        |
                        +--> customer_name (overrides "New caller")
                        +--> primary_warehouse (drives store-default
                              routing + per-store greeting context)
                        +--> territory (region label, advisory)

Phase 2 (later session) will extend the Eagle Bridge customer_sync to
also pull each customer's assigned salesrep code from Eagle, write it
to `caller_contacts.salesrep_code`, and let us go phone -> specialist
directly without a county-based fallback.
"""

import asyncio
from typing import Optional, Dict

from config import supabase, logger


async def lookup_customer_by_phone(phone: str) -> Optional[Dict]:
    """Look up a caller in `caller_contacts` by E.164 phone number.

    `caller_contacts.phone_normalized` is stored as `+14062402889`
    (E.164), which matches exactly what Retell sends in `from_number`.
    No normalization needed — query with the raw value.

    Widget calls (no phone, key like `widget_<call_id>`) return None.

    Returns
    -------
    None
        Caller not found, no phone, or Supabase unavailable.
    dict
        Subset of the caller_contacts row with the fields the voice
        agent actually uses. All non-string fields are stringified for
        the JSON-only Retell dynamic-variable channel.
    """
    if not supabase:
        logger.warning("[CUSTOMER] Supabase not configured")
        return None

    if not phone or not phone.startswith("+"):
        # Widget keys ("widget_abc") and unsigned phones won't match
        # phone_normalized. Bail early instead of round-tripping to PG.
        return None

    try:
        result = await asyncio.to_thread(
            lambda: supabase.table("caller_contacts")
                .select(
                    "customer_id, customer_name, first_name, last_name, "
                    "customer_type, city, state, primary_warehouse, "
                    "territory, total_sales, transaction_count, "
                    "last_purchase, is_existing_customer, is_prospect"
                )
                .eq("phone_normalized", phone)
                .limit(1)
                .execute()
        )

        rows = result.data or []
        if not rows:
            return None

        row = rows[0]
        # Stringify everything Retell will consume as a dynamic variable —
        # Retell dynamic vars are string-only and None values cause the
        # agent to render literal "None" if not handled.
        out = {
            "found": True,
            "customer_id": (row.get("customer_id") or "") or "",
            "customer_name": _title_or_empty(row.get("customer_name")),
            "first_name": _title_or_empty(row.get("first_name")),
            "last_name": _title_or_empty(row.get("last_name")),
            "city": _title_or_empty(row.get("city")),
            "state": (row.get("state") or "").upper(),
            "primary_warehouse": row.get("primary_warehouse") or "",
            "territory": row.get("territory") or "",
            "total_sales": float(row.get("total_sales") or 0),
            "transaction_count": int(row.get("transaction_count") or 0),
            "last_purchase": str(row.get("last_purchase") or ""),
            "is_existing_customer": bool(row.get("is_existing_customer")),
            "is_prospect": bool(row.get("is_prospect")),
        }
        logger.info(
            f"[CUSTOMER] Matched phone -> {out['customer_name'] or '?'} "
            f"({out['primary_warehouse'] or 'no-warehouse'}, "
            f"customer_id={out['customer_id'] or '-'}, "
            f"txns={out['transaction_count']})"
        )
        return out

    except Exception as e:
        logger.error(f"[CUSTOMER] lookup_customer_by_phone error: {e}")
        return None


def _title_or_empty(value: Optional[str]) -> str:
    """Convert UPPERCASE Eagle-style names to Title Case for spoken use.

    `caller_contacts` mirrors Eagle's customer master, which stores
    names in all caps ("GUY HANSON"). All caps on the voice agent's
    side produces emphasized TTS — `.title()` gives Brian a more
    natural read while still preserving punctuation like apostrophes.
    """
    if not value:
        return ""
    return value.strip().title()
