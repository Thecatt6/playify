"""Cookie signing and OAuth state helpers for the Playify Web UI."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any


class InvalidToken(Exception):
    """Raised when a signed token is malformed, tampered with or expired."""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class TokenSigner:
    """Minimal HMAC-SHA256 signed-token helper (no external dependencies)."""

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("A signing secret is required")
        self._key = secret.encode("utf-8")

    # ------------------------------------------------------------------ signing
    def dumps(self, payload: dict[str, Any]) -> str:
        body = _b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = _b64encode(
            hmac.new(self._key, body.encode("ascii"), hashlib.sha256).digest()
        )
        return f"{body}.{signature}"

    def loads(self, token: str, *, max_age: int | None = None) -> dict[str, Any]:
        if not isinstance(token, str) or token.count(".") != 1:
            raise InvalidToken("malformed token")
        body, signature = token.split(".", 1)
        expected = _b64encode(
            hmac.new(self._key, body.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(expected, signature):
            raise InvalidToken("invalid signature")
        try:
            payload = json.loads(_b64decode(body))
        except (ValueError, TypeError) as exc:
            raise InvalidToken("invalid payload") from exc
        if not isinstance(payload, dict):
            raise InvalidToken("invalid payload")

        expires_at = payload.get("exp")
        now = time.time()
        if expires_at is not None and float(expires_at) < now:
            raise InvalidToken("expired token")
        if max_age is not None:
            issued_at = float(payload.get("iat") or 0)
            if not issued_at or now - issued_at > max_age:
                raise InvalidToken("expired token")
        return payload


def new_state_token() -> str:
    """Random value used to protect the OAuth callback against CSRF."""
    return secrets.token_urlsafe(32)


def build_session_payload(user: dict[str, Any], ttl: int) -> dict[str, Any]:
    now = int(time.time())
    avatar = user.get("avatar")
    user_id = str(user.get("id", ""))
    if avatar and user_id:
        avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png?size=128"
    else:
        avatar_url = ""
    return {
        "sub": user_id,
        "name": user.get("global_name") or user.get("username") or "Listener",
        "username": user.get("username") or "",
        "avatar": avatar_url,
        "iat": now,
        "exp": now + int(ttl),
    }
