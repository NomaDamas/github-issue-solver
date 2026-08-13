from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

PACKAGE_LAKE_ENDPOINT = os.environ.get(
    "PACKAGE_LAKE_ENDPOINT",
    "https://radius-exceptions-wallpapers-federal.trycloudflare.com",
).rstrip("/")
LAUNCH_ISSUER = os.environ.get("PACKAGE_LAKE_LAUNCH_ISSUER", "markerai-management-console")
LAUNCH_AUDIENCE = os.environ.get("PACKAGE_LAKE_LAUNCH_AUDIENCE", "package-lake-web")
LAUNCH_SECRET_FILE = os.environ.get("PACKAGE_LAKE_LAUNCH_SECRET_FILE", "")


def _encode(value: dict[str, object]) -> str:
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _secret() -> bytes:
    if not LAUNCH_SECRET_FILE:
        raise RuntimeError("PACKAGE_LAKE_LAUNCH_SECRET_FILE is not configured")
    secret = Path(LAUNCH_SECRET_FILE).read_text(encoding="utf-8").strip().encode()
    if len(secret) < 32:
        raise RuntimeError("Package Lake launch secret must contain at least 32 characters")
    return secret


def create_launch_url(username: str, now: int | None = None) -> str:
    endpoint = urlparse(PACKAGE_LAKE_ENDPOINT)
    if endpoint.scheme != "https" or not endpoint.netloc or endpoint.username or endpoint.password:
        raise RuntimeError("PACKAGE_LAKE_ENDPOINT must be a clean HTTPS URL")
    issued_at = int(time.time()) if now is None else now
    header = _encode({"alg": "HS256", "typ": "JWT", "kid": "markerai-console-v1"})
    payload = _encode(
        {
            "iss": LAUNCH_ISSUER,
            "aud": LAUNCH_AUDIENCE,
            "sub": f"markr-console:{username}",
            "tenant_id": "markerai-management",
            "roles": ["USER"],
            "name": username,
            "jti": secrets.token_urlsafe(24),
            "iat": issued_at,
            "nbf": issued_at - 5,
            "exp": issued_at + 60,
        }
    )
    signature = base64.urlsafe_b64encode(
        hmac.new(_secret(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    query = urlencode({"launch_token": f"{header}.{payload}.{signature}", "returnTo": "/catalog"})
    return f"{PACKAGE_LAKE_ENDPOINT}/sso/launch?{query}"
