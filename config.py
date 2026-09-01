"""
Montana Feed Company - Configuration and Client Setup
Version 3.0.0 - Modular Refactor
"""

import os
import logging
from typing import Optional
from contextlib import asynccontextmanager

import httpx
from supabase import create_client, Client

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================================
# ENVIRONMENT VARIABLES
# ============================================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ZEP_API_KEY = os.getenv("ZEP_API_KEY", "").strip()

# ---------------------------------------------------------------------------
# ADVISORY MODE (2026-09-01)
# ---------------------------------------------------------------------------
# MFC staff were not comfortable with the agent giving nutrition / product
# advice on live calls, so the advisor is switched OFF for the initial
# deployment while every other feature (routing, messages, callbacks, leads,
# store info, transfers) ships.
#
# OFF (the default — a fresh deploy with no env var set fails CLOSED):
#   - search_knowledge_base is restricted to KB_NON_ADVISORY_CATEGORIES
#   - search_products / get_recommendations return a "defer to the LPS" string
#
# To restore the full advisor: set ADVISORY_MODE=on on the Railway service
# and republish the Retell agent version that carries the v13 system prompt.
ADVISORY_MODE = os.getenv("ADVISORY_MODE", "off").strip().lower()
ADVISORY_ENABLED = ADVISORY_MODE in {"on", "true", "1", "yes", "enabled"}

# Consulted only while the advisor is off. Case-SENSITIVE on purpose:
# lowercase `products` (3 rows — "what do you sell", custom mixes, commodity
# loads) is catalog fact and stays; capital-P `Products` (112 rows) is the
# Purina recommendation catalog, complete with pricing, and does not. This
# allowlist also happens to fence off the ~40 internal business-analytics
# rows filed under `Ranch Consultation` (margin analysis, lapsed-customer
# reports, SKU rationalization), which callers could otherwise reach.
KB_NON_ADVISORY_CATEGORIES = frozenset({
    "company_info",
    "stores",
    "locations",
    "operations",
    "specialists",
    "products",
})

logger.info(
    "ADVISORY_MODE=%s (nutrition/product advice %s)",
    ADVISORY_MODE, "ENABLED" if ADVISORY_ENABLED else "DISABLED",
)

# Validate critical env vars
if not SUPABASE_URL or not SUPABASE_KEY:
    logger.warning("Supabase not configured; lead features will be limited")
if not ZEP_API_KEY:
    logger.warning("Zep not configured; memory features disabled")

# ============================================================================
# CLIENT INITIALIZATION
# ============================================================================

# Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# ============================================================================
# ZEP CLOUD REST API CONFIGURATION
# ============================================================================

ZEP_BASE_URL = "https://api.getzep.com/api/v2"
ZEP_HEADERS = {
    "Authorization": f"Api-Key {ZEP_API_KEY}",
    "Content-Type": "application/json"
}

# Persistent HTTP client for Zep (reduces latency)
_zep_client: Optional[httpx.AsyncClient] = None

# Persistent HTTP client for other outbound APIs (Resend, etc.). Kept
# separate from the Zep client so a Zep outage can't starve the email
# connection pool (and vice versa).
_http_client: Optional[httpx.AsyncClient] = None


def get_zep_client() -> Optional[httpx.AsyncClient]:
    """Get the persistent Zep HTTP client."""
    return _zep_client


def get_http_client() -> Optional[httpx.AsyncClient]:
    """Get the shared outbound HTTP client (Resend, etc.)."""
    return _http_client


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def normalize_phone(phone: str) -> str:
    """Normalize phone number for consistent user IDs."""
    return phone.replace("+", "").replace(" ", "").replace("-", "")


def redact_phone(phone: str) -> str:
    """Mask a caller identifier for logging. Keeps the last 4 digits so on-call
    can still correlate a specific complaint against logs, without spraying
    full numbers into log aggregation / alerting systems.

    Examples:
        "+14065551234"     -> "***1234"
        "widget_abc123xyz" -> "widget_***xyz"
        ""                 -> "<unknown>"
    """
    if not phone:
        return "<unknown>"
    if phone.startswith("widget_"):
        tail = phone[-3:] if len(phone) > 10 else "xxx"
        return f"widget_***{tail}"
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) < 4:
        return "***"
    return f"***{digits[-4:]}"


# ============================================================================
# APPLICATION LIFESPAN MANAGER
# ============================================================================

@asynccontextmanager
async def lifespan(app):
    """Manage application lifespan - setup and teardown."""
    global _zep_client, _http_client

    # Startup: create persistent HTTP clients
    _zep_client = httpx.AsyncClient(
        timeout=httpx.Timeout(5.0, connect=2.0),
        limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
    )
    logger.info("✓ Started persistent Zep HTTP client")

    # Outbound client (Resend, etc.). 10s total is generous for transactional
    # email providers — still well under Retell's webhook patience.
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=2.0),
        limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
    )
    logger.info("✓ Started persistent outbound HTTP client")

    yield

    # Shutdown: close clients
    if _zep_client:
        await _zep_client.aclose()
        logger.info("✓ Closed Zep HTTP client")
    if _http_client:
        await _http_client.aclose()
        logger.info("✓ Closed outbound HTTP client")
