import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse

_logger = logging.getLogger(__name__)

# Values that disable Retell signature enforcement. Anything outside this set
# (including typos, unexpected casing) is treated as "enforce" — fail-safe.
_DISABLED_VALUES = {"false", "0", "no", "off"}

# Retell signs webhooks Stripe-style: `v={timestamp_ms},d={hex_hmac_sha256}`.
# The HMAC input is `body_string + str(timestamp_ms)`, not just the body.
# See RetellAI/retell-python-sdk src/retell/lib/webhook_auth.py.
_SIG_RE = re.compile(r"v=(\d+),d=(.*)")
_FIVE_MINUTES_MS = 5 * 60 * 1000


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


def _verify(body: bytes, signature: str, *, now_ms: Optional[int] = None) -> bool:
    """Verify a Retell webhook signature.

    Retell's format is `v={timestamp_ms},d={hex_hmac_sha256}` (Stripe-style).
    The HMAC is computed over `body_string + str(timestamp_ms)` using the
    Retell API key as the symmetric secret, and the signature is rejected
    if the timestamp is more than 5 minutes off wall-clock (replay window).
    """
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
        match = _SIG_RE.search(signature)
        if not match:
            _logger.warning("Retell signature did not match expected v=...,d=... format")
            return False

        poststamp = int(match.group(1))
        post_digest = match.group(2)

        if now_ms is None:
            now_ms = int(time.time() * 1000)
        if abs(now_ms - poststamp) > _FIVE_MINUTES_MS:
            _logger.warning(
                "Retell signature timestamp outside 5-minute window "
                f"(drift={(now_ms - poststamp) / 1000:.1f}s)"
            )
            return False

        body_str = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body
        message = (body_str + str(poststamp)).encode("utf-8")
        expected = hmac.new(api_key.encode("utf-8"), message, hashlib.sha256).hexdigest()

        return hmac.compare_digest(expected, post_digest)
    except Exception as e:
        _logger.warning(f"Retell signature verification raised: {e}")
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
