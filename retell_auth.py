import hashlib
import hmac
import json
import logging
import os
from typing import Tuple

from fastapi import Request
from fastapi.responses import JSONResponse

_logger = logging.getLogger(__name__)

# Values that disable Retell signature enforcement. Anything outside this set
# (including typos, unexpected casing) is treated as "enforce" — fail-safe.
_DISABLED_VALUES = {"false", "0", "no", "off"}


def _enforce_enabled() -> bool:
    raw = os.getenv("RETELL_SIGNATURE_ENFORCE", "true").strip().lower()
    return raw not in _DISABLED_VALUES


def verify_admin_token(request: Request) -> bool:
    """Check for a matching X-Admin-Token header. Fails closed if
    ADMIN_API_TOKEN is not configured so admin endpoints can never be
    publicly callable on a misconfigured deploy."""
    expected = os.getenv("ADMIN_API_TOKEN", "").strip()
    if not expected:
        return False
    provided = request.headers.get("x-admin-token", "").strip()
    if not provided:
        return False
    return hmac.compare_digest(expected, provided)


def forbidden_response() -> JSONResponse:
    return JSONResponse(status_code=403, content={"error": "forbidden"})


def _verify(body: bytes, signature: str) -> bool:
    api_key = os.getenv("RETELL_API_KEY", "").strip()
    enforce = _enforce_enabled()

    if not api_key:
        if not enforce:
            _logger.warning(
                "Retell signature verification DISABLED "
                "(RETELL_SIGNATURE_ENFORCE=false and no RETELL_API_KEY set). "
                "This is only safe for local dev."
            )
        return not enforce

    if not signature:
        return False

    try:
        expected = hmac.new(
            api_key.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception:
        return False


async def read_and_verify(request: Request) -> Tuple[bool, bytes, dict]:
    body = await request.body()
    signature = request.headers.get("x-retell-signature", "")
    ok = _verify(body, signature)
    if not ok:
        return False, body, {}
    try:
        parsed = json.loads(body) if body else {}
    except Exception:
        return False, body, {}
    return True, body, parsed


def unauthorized_response() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": "invalid signature"})
