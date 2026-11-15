"""Attach the Web UI server to the running Playify bot."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from .config import WebUIConfig, load_config
from .context import WebUIContext
from .oauth import DiscordOAuth
from .security import TokenSigner
from .server import create_app

logger = logging.getLogger("playify.webui")

_context: WebUIContext | None = None
_runner: web.AppRunner | None = None
_starting = False


def is_running() -> bool:
    return _runner is not None


def current_url() -> str | None:
    """Best-effort public URL of the Web UI (``None`` when it is not running)."""
    if _context is None:
        return None
    config = _context.config
    if config.base_url:
        return config.base_url
    host = "localhost" if config.host in {"0.0.0.0", "::"} else config.host
    return f"http://{host}:{config.port}"


def _log_startup_hint(config: WebUIConfig) -> None:
    if config.oauth_configured and not config.base_url:
        logger.warning(
            "WEBUI_BASE_URL is not set: the OAuth redirect URI will be derived from "
            "each request. Set it (for example https://music.example.com) and add "
            "%s/auth/callback to your Discord application's redirect URLs.",
            current_url() or "",
        )


async def start_webui(bot: Any, config: WebUIConfig | None = None) -> WebUIContext | None:
    """Start the HTTP server (idempotent). Returns the context when listening."""
    global _context, _runner, _starting

    if _runner is not None or _starting:
        return _context

    _starting = True
    try:
        config = config or load_config()
        if not config.enabled:
            logger.info("Web UI is disabled (WEBUI_ENABLED=false).")
            return None

        if not config.login_enabled:
            missing = ", ".join(config.missing_settings()) or "DISCORD_CLIENT_ID"
            logger.warning(
                "Web UI not started: Discord OAuth is not configured (missing %s). "
                "Add the credentials to your .env — see the Web UI documentation.",
                missing,
            )
            return None

        _log_startup_hint(config)

        context = WebUIContext(
            bot=bot,
            config=config,
            signer=TokenSigner(config.session_secret),
            oauth=DiscordOAuth(config.client_id, config.client_secret),
        )
        app = create_app(context)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, config.host, config.port)
        try:
            await site.start()
        except OSError as exc:
            logger.error(
                "Web UI could not bind %s:%s (%s). Is the port already in use?",
                config.host,
                config.port,
                exc,
            )
            await runner.cleanup()
            return None

        _context, _runner = context, runner
        logger.info(
            "🌐 Playify Web UI listening on %s (OAuth redirect URI: %s/auth/callback)",
            current_url(),
            (config.base_url or current_url() or "").rstrip("/"),
        )
        return context
    finally:
        _starting = False


async def stop_webui() -> None:
    global _context, _runner
    if _runner is None:
        return
    runner, _runner = _runner, None
    try:
        await runner.cleanup()
        logger.info("Web UI stopped.")
    finally:
        _context = None
