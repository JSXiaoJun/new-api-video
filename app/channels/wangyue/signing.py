"""Wangyue pool request signing (HMAC-SHA256 over a six-line canonical text).

Canonical text, joined with LF::

    METHOD
    PATH_WITH_SORTED_QUERY
    TIMESTAMP
    NONCE
    IDEMPOTENCY_KEY
    SHA256(STABLE_JSON_BODY)

The body hash is taken over the *stable* JSON (keys sorted recursively, no
whitespace, non-ASCII kept as is, exactly like the Node ``stable()`` reference
in the partner guide). Requests are sent with that same byte string as their
body, so the hash matches whether the gateway hashes the raw bytes or
re-serializes the parsed object.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any
from urllib.parse import urlsplit


KEY_ID_HEADER = "X-Wangyue-Key-Id"
TIMESTAMP_HEADER = "X-Wangyue-Timestamp"
NONCE_HEADER = "X-Wangyue-Nonce"
SIGNATURE_HEADER = "X-Wangyue-Signature"
IDEMPOTENCY_HEADER = "X-Idempotency-Key"

EMPTY_BODY = b"{}"


class WangyueCredentialError(ValueError):
    """The upstream key is not in the ``keyId:secret`` form."""


def parse_credentials(api_key: str) -> tuple[str, str]:
    """Split the stored upstream key into ``(key_id, secret)``.

    The admin page has one key field, so the pair is stored as
    ``keyId:secret``; the secret never leaves this process.
    """
    key_id, separator, secret = (api_key or "").partition(":")
    key_id, secret = key_id.strip(), secret.strip()
    if not separator or not key_id or not secret:
        raise WangyueCredentialError("望月号池的 API Key 必须填写为 keyId:secret")
    return key_id, secret


def stable_json(value: Any) -> str:
    """Serialize like the guide's ``stable()``: sorted keys, arrays in order."""
    if isinstance(value, dict):
        return "{" + ",".join(
            f"{json.dumps(str(key), ensure_ascii=False)}:{stable_json(value[key])}"
            for key in sorted(value, key=str)
        ) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(stable_json(item) for item in value) + "]"
    if isinstance(value, float) and value.is_integer():
        # JavaScript prints 15.0 as 15; the hashes must agree.
        return str(int(value))
    return json.dumps(value, ensure_ascii=False)


def encode_body(body: dict[str, Any] | None) -> bytes:
    return stable_json(body).encode("utf-8") if body else EMPTY_BODY


def sign(
    secret: str,
    method: str,
    path: str,
    timestamp: int,
    nonce: str,
    idempotency_key: str,
    body: bytes,
) -> str:
    canonical = "\n".join((
        method.upper(),
        path,
        str(timestamp),
        nonce,
        idempotency_key,
        hashlib.sha256(body).hexdigest(),
    ))
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def signed_path(url: str) -> str:
    parts = urlsplit(url)
    if parts.query:
        # The guide does not pin down the query encoding, and the relay sends
        # no signed request with a query, so refuse instead of guessing.
        raise ValueError("signed Wangyue requests must not carry a query string")
    return parts.path or "/"


def signed_headers(
    api_key: str,
    method: str,
    url: str,
    body: bytes = EMPTY_BODY,
    idempotency_key: str = "",
) -> dict[str, str]:
    """Headers for one request. A fresh nonce each call: never reuse them."""
    key_id, secret = parse_credentials(api_key)
    timestamp = int(time.time())
    nonce = secrets.token_hex(16)
    headers = {
        KEY_ID_HEADER: key_id,
        TIMESTAMP_HEADER: str(timestamp),
        NONCE_HEADER: nonce,
        SIGNATURE_HEADER: sign(secret, method, signed_path(url), timestamp, nonce, idempotency_key, body),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if idempotency_key:
        headers[IDEMPOTENCY_HEADER] = idempotency_key
    return headers
