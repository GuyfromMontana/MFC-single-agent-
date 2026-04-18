import hashlib
import hmac
import json
import os
from typing import Tuple

from fastapi import Request
from fastapi.responses import JSONResponse


def _verify(body: bytes, signature: str) -> bool:
    api_key = os.getenv("RETELL_API_KEY", "").strip()
    enforce = os.getenv("RETELL_SIGNATURE_ENFORCE", "true").lower() != "false"

    if not api_key:
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
