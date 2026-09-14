"""
Montana Feed Company - Configuration and Client Setup
Version 3.0.0 - Modular Refactor
"""

import os
import asyncio
import logging
from typing import Optional
from contextlib import asynccontextmanager

import httpx
from supabase import create_client, Client, ClientOptions

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

# Supabase client.
#
# supabase-py defaults `postgrest_client_timeout` to 120s (postgrest/constants.py).
# That is a webhook-killer: a single hung connection parks a Retell tool call for
# two minutes while the caller listens to silence. 10s is far longer than any
# query this service issues — the biggest table read is ~19 product rows — so a
# request still running at 10s is hung, not slow. Connect is tightened to 2s to
# match the Zep and outbound clients below; a connect failure is in sb_exec's
# "never processed" tier, so it retries cleanly rather than surfacing.
SUPABASE_TIMEOUT = httpx.Timeout(10.0, connect=2.0)

supabase: Client = (
    create_client(
        SUPABASE_URL,
        SUPABASE_KEY,
        options=ClientOptions(postgrest_client_timeout=SUPABASE_TIMEOUT),
    )
    if SUPABASE_URL and SUPABASE_KEY
    else None
)

# ============================================================================
# SUPABASE TRANSPORT RETRY (2026-09-14)
# ============================================================================
#
# The client above is a module-level singleton whose httpx client is built once
# and lives as long as the Railway pod, and postgrest-py hardcodes `http2=True`
# (postgrest/_sync/client.py). So every query rides a long-lived HTTP/2
# connection that Supabase's edge periodically retires with a *graceful* GOAWAY
# (`ConnectionTerminated error_code:0`). httpcore does not transparently replay
# a request that raced that GOAWAY — it raises RemoteProtocolError straight
# through. On 2026-09-12 that silently dropped a completed call from
# `conversations`: the webhook still returned 200 and the transcript email still
# went out, so nothing looked broken until Sentry fired.
#
# We retry only failures the server has told us it did NOT process:
#   * GOAWAY carries `last_stream_id` — streams above it were never handled.
#   * Connect/pool failures never put bytes on the wire.
#
# A ReadTimeout is deliberately NOT in that set: the request did reach
# PostgREST and we merely never saw the response, so replaying an INSERT could
# double-write. Call sites that are safe to replay anyway — every read, plus
# the fixed-value UPDATE in skills/leads.py — opt in with `idempotent=True`.

SB_RETRY_ATTEMPTS = 3
SB_RETRY_BASE_DELAY = 0.25  # seconds; doubles each attempt (0.25s, 0.5s)

# Provably never processed — safe to replay even for writes.
_SB_UNPROCESSED_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
    httpx.CloseError,
)

# Ambiguous — the request may already have been applied. Replayed only when
# the caller asserts the operation is safe to repeat.
_SB_AMBIGUOUS_ERRORS = (
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.WriteTimeout,
)


async def sb_exec(run, *, what: str = "supabase", idempotent: bool = False):
    """Run a blocking postgrest call off the event loop, retrying transport errors.

    `run` is a zero-arg callable that both builds and executes the query, e.g.
    `lambda: supabase.table("leads").select("id").execute()`. It is re-invoked
    from scratch on every attempt, so a builder is never reused across retries.

    Drop-in replacement for `asyncio.to_thread(run)` at Supabase call sites.
    Non-transport failures (PostgREST 4xx/5xx, APIError) propagate untouched on
    the first attempt — this retries the pipe, not the query.
    """
    retryable = _SB_UNPROCESSED_ERRORS + (_SB_AMBIGUOUS_ERRORS if idempotent else ())
    last_exc: Optional[BaseException] = None

    for attempt in range(1, SB_RETRY_ATTEMPTS + 1):
        try:
            return await asyncio.to_thread(run)
        except retryable as e:
            last_exc = e
            if attempt == SB_RETRY_ATTEMPTS:
                break
            delay = SB_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "[SB] %s: %s on attempt %d/%d (%s) — retrying in %.2fs",
                what, type(e).__name__, attempt, SB_RETRY_ATTEMPTS, e, delay,
            )
            await asyncio.sleep(delay)

    logger.error(
        "[SB] %s: transport failure, gave up after %d attempts (%s: %s)",
        what, SB_RETRY_ATTEMPTS, type(last_exc).__name__, last_exc,
    )
    raise last_exc

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
