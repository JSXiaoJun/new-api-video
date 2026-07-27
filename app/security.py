from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque
from threading import Lock

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


SESSION_COOKIE = "pidoi_admin_session"
SESSION_TTL_SECONDS = 12 * 60 * 60


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(value: str) -> str:
    return _b64encode(hmac.new(settings.session_secret.encode(), value.encode(), hashlib.sha256).digest())


def create_session(username: str) -> str:
    payload = {
        "username": username,
        "expires_at": int(time.time()) + SESSION_TTL_SECONDS,
        "nonce": secrets.token_urlsafe(18),
    }
    encoded = _b64encode(json.dumps(payload, separators=(",", ":")).encode())
    return f"{encoded}.{_sign(encoded)}"


def read_session(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(encoded)):
        return None
    try:
        payload = json.loads(_b64decode(encoded))
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("expires_at", 0) < int(time.time()):
        return None
    if payload.get("username") != settings.admin_username:
        return None
    return payload


def csrf_token(session_token: str) -> str:
    return _sign(f"csrf:{session_token}")


def verify_csrf(session_token: str, token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(token, csrf_token(session_token))


def verify_admin_credentials(username: str, password: str) -> bool:
    return hmac.compare_digest(username, settings.admin_username) and hmac.compare_digest(
        password, settings.admin_password
    )


def verify_adapter_key(authorization: str | None) -> bool:
    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    token = authorization[7:].strip()
    return hmac.compare_digest(token, settings.adapter_api_key)


class LoginLimiter:
    def __init__(self, maximum: int = 5, window_seconds: int = 600) -> None:
        self.maximum = maximum
        self.window_seconds = window_seconds
        self.attempts: dict[str, deque[float]] = defaultdict(deque)
        self.lock = Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            attempts = self.attempts[key]
            while attempts and attempts[0] < now - self.window_seconds:
                attempts.popleft()
            if len(attempts) >= self.maximum:
                return False
            attempts.append(now)
            return True

    def clear(self, key: str) -> None:
        with self.lock:
            self.attempts.pop(key, None)


login_limiter = LoginLimiter()


class SecretBox:
    def __init__(self, key: str) -> None:
        try:
            self.fernet = Fernet(key.encode())
        except ValueError as exc:
            raise RuntimeError("ENCRYPTION_KEY must be a valid Fernet key") from exc

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self.fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise RuntimeError("Unable to decrypt an upstream API key") from exc


secret_box = SecretBox(settings.encryption_key)

