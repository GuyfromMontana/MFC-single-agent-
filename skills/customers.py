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
import re
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

        # Guard: an Eagle house/cash account is not a person. Blank the name
        # (so the prompt falls back to "New caller" and simply ASKS) and drop
        # the fake purchase history that came with the shared account — a
        # walk-in account's last_purchase belongs to whoever last used it,
        # not to this caller, and "you got the AI again" to a first-time
        # caller is worse than no recognition at all.
        _raw_name = row.get("customer_name")
        if _is_placeholder_name(_raw_name, row.get("customer_id")):
            logger.warning(
                "[CUSTOMER] Placeholder account matched for %s -> %r "
                "(customer_id=%r); suppressing name/history. The real buyer "
                "may be in first_name=%r. Fix the phone on this Eagle account.",
                phone, _raw_name, row.get("customer_id"), row.get("first_name"),
            )
            row = dict(row)
            row["customer_name"] = None
            row["first_name"] = None
            row["last_name"] = None
            row["last_purchase"] = None
            row["is_existing_customer"] = False
            row["transaction_count"] = 0
            row["total_sales"] = 0
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


# Eagle system/house accounts that are NOT people. A walk-in rung up under
# one of these lands in caller_contacts with the account label sitting in
# customer_name, so without this guard the agent greets the caller by the
# account name.
#
# Real production failure (2026-09-04): a brand-new prospect was greeted
# "Well hey there, Cash Customer — you got the AI again" and had to correct
# the agent in his first breath. The row was Eagle's generic CASH CUSTOMER
# account carrying MFC's OWN main office number (+1 406-728-7020), with the
# actual buyer ("RAMBLIN M RANCH LLC") stuffed into first_name. Every call
# forwarded through the main line matched it.
#
# Mirrors the junk-name blocklist the Zep path already has in skills/memory.py.
_PLACEHOLDER_NAMES = frozenset({
    "cash customer", "cash sale", "cash", "walk in", "walkin", "walk-in",
    "counter sale", "counter", "house account", "house acct", "misc",
    "miscellaneous", "customer", "unknown", "no name", "n/a", "na", "none",
    "test", "test customer",
})


def _is_placeholder_name(value: Optional[str], customer_id: Optional[str] = None) -> bool:
    """True when `value` is an Eagle account label rather than a person's name.

    Three independent tells, any one of which is enough:
      1. the normalized name is in the blocklist above;
      2. the name is `*`-prefixed — Eagle's convention for cash accounts;
      3. the name is identical to customer_id, which only happens for
         system accounts (real customers have a numeric/coded id).
    """
    if not value:
        return True
    v = value.strip()
    if v.startswith("*"):
        return True
    norm = re.sub(r"[^a-z ]", " ", v.lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    if norm in _PLACEHOLDER_NAMES:
        return True
    if customer_id and v.upper() == str(customer_id).strip().upper():
        return True
    return False


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
