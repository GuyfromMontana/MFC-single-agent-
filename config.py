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
