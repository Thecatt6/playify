"""Environment-driven configuration for the Playify Web UI.

This module deliberately avoids importing :mod:`discord` (or anything from the
bot runtime) so that the web layer stays importable in isolation, which keeps
it easy to test.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("playify.webui")

_TRUTHY = {"1", "true", "yes", "y", "on"}
_FALSY = {"0", "false", "no", "n", "off"}

#: Root of the repository (…/playify), used to locate the ``data`` folder.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

SESSION_COOKIE = "playify_webui"
OAUTH_STATE_COOKIE = "playify_webui_oauth"


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return default


def _env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = _env(name)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _load_or_create_secret(data_dir: Path) -> str:
    """Return a stable signing secret, creating ``data/webui_secret`` if needed."""
    env_secret = _env("WEBUI_SECRET")
    if env_secret:
        return env_secret

    secret_file = data_dir / "webui_secret"
    try:
        if secret_file.exists():
            stored = secret_file.read_text(encoding="utf-8").strip()
            if stored:
                return stored
    except OSError as exc:  # pragma: no cover - unlikely, but keep the bot alive
        logger.warning("Could not read %s: %s", secret_file, exc)

    secret = secrets.token_urlsafe(48)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(secret + "\n", encoding="utf-8")
        try:
            secret_file.chmod(0o600)
        except OSError:
            pass
        logger.info("Generated a new Web UI signing secret at %s", secret_file)
    except OSError as exc:  # pragma: no cover
        logger.warning(
            "Could not persist the Web UI signing secret (%s); sessions will not "
            "survive a restart. Set WEBUI_SECRET to fix this.",
            exc,
        )
    return secret


@dataclass(frozen=True)
class WebUIConfig:
    """Immutable runtime configuration for the Web UI."""

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    base_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    session_secret: str = ""
    session_ttl: int = 12 * 60 * 60
    oauth_state_ttl: int = 10 * 60
    secure_cookie: bool = False
    trust_proxy: bool = False
    dev_bypass_user_id: int = 0
    allow_any_origin: bool = False
    action_cooldown: float = 0.35

    # ------------------------------------------------------------------ helpers
    @property
    def oauth_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def login_enabled(self) -> bool:
        """OAuth is the only way in — unless an explicit dev bypass is active."""
        return self.oauth_configured or bool(self.dev_bypass_user_id)

    def redirect_uri(self, origin: str) -> str:
        base = (self.base_url or origin or "").rstrip("/")
        return f"{base}/auth/callback"

    def missing_settings(self) -> list[str]:
        required = {
            "DISCORD_CLIENT_ID": self.client_id,
            "DISCORD_CLIENT_SECRET": self.client_secret,
        }
        return [name for name, value in required.items() if not value]


def load_config() -> WebUIConfig:
    """Build a :class:`WebUIConfig` from the process environment."""
    base_url = _env("WEBUI_BASE_URL").rstrip("/")
    dev_bypass = _env_int("WEBUI_DEV_BYPASS_USER_ID", 0, minimum=0)

    config = WebUIConfig(
        enabled=_env_bool("WEBUI_ENABLED", True),
        host=_env("WEBUI_HOST", "0.0.0.0") or "0.0.0.0",
        port=_env_int("WEBUI_PORT", 8080, minimum=1, maximum=65535),
        base_url=base_url,
        client_id=_env("DISCORD_CLIENT_ID") or _env("WEBUI_DISCORD_CLIENT_ID"),
        client_secret=_env("DISCORD_CLIENT_SECRET") or _env("WEBUI_DISCORD_CLIENT_SECRET"),
        session_secret=_load_or_create_secret(PROJECT_ROOT / "data"),
        session_ttl=_env_int("WEBUI_SESSION_TTL", 12 * 60 * 60, minimum=300),
        secure_cookie=_env_bool("WEBUI_SECURE_COOKIES", base_url.startswith("https://")),
        trust_proxy=_env_bool("WEBUI_TRUST_PROXY", bool(base_url)),
        dev_bypass_user_id=dev_bypass,
        allow_any_origin=_env_bool("WEBUI_ALLOW_ANY_ORIGIN", False),
    )

    if config.allow_any_origin:
        logger.warning(
            "WEBUI_ALLOW_ANY_ORIGIN is set: Origin checks are disabled. Only use "
            "this for local development or demos."
        )

    if config.dev_bypass_user_id:
        logger.warning(
            "WEBUI_DEV_BYPASS_USER_ID is set: the Web UI will treat requests as "
            "Discord user %s without OAuth. Never use this in production.",
            config.dev_bypass_user_id,
        )

    return config
