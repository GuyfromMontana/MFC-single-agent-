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
from openai import OpenAI

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
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

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

# OpenAI client
openai_client = OpenAI(api_key=OPENAI_API_KEY, timeout=5.0, max_retries=1)

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


def get_zep_client() -> Optional[httpx.AsyncClient]:
    """Get the persistent Zep HTTP client."""
    return _zep_client


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def normalize_phone(phone: str) -> str:
    """Normalize phone number for consistent user IDs."""
    return phone.replace("+", "").replace(" ", "").replace("-", "")


# ============================================================================
# APPLICATION LIFESPAN MANAGER
# ============================================================================

@asynccontextmanager
async def lifespan(app):
    """Manage application lifespan - setup and teardown."""
    global _zep_client
    
    # Startup: create persistent HTTP client
    _zep_client = httpx.AsyncClient(
        timeout=httpx.Timeout(5.0, connect=2.0),
        limits=httpx.Limits(max_keepalive_connections=5, max_connections=10)
    )
    logger.info("✓ Started persistent Zep HTTP client")
    
    yield
    
    # Shutdown: close client
    if _zep_client:
        await _zep_client.aclose()
        logger.info("✓ Closed Zep HTTP client")
