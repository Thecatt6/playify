"""HTTP + WebSocket server for the Playify Web UI."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aiohttp import WSMsgType, web

from . import actions as actions_module
from .access import connected_channels, resolve_session, resolve_sessions
from .config import OAUTH_STATE_COOKIE, SESSION_COOKIE
from .context import WebUIContext
from .oauth import OAuthError
from .security import (
    InvalidToken,
    build_session_payload,
    new_state_token,
)
from .snapshot import build_snapshot

logger = logging.getLogger("playify.webui")

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: Actions that hit the network (and therefore yt-dlp) get a stricter cooldown.
_HEAVY_ACTIONS = {"play"}
_HEAVY_COOLDOWN = 1.0
_DEFAULT_COOLDOWN = 0.25
_rate_limits: dict[tuple[int, str], float] = {}

CSP = (
    "default-src 'self'; "
    "img-src 'self' https: data:; "
    "style-src 'self'; "
    "script-src 'self'; "
    "connect-src 'self' ws: wss:; "
    "media-src 'self' https:; "
    "font-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'"
)


class ApiError(Exception):
    def __init__(self, status: int, error: str, message: str = "") -> None:
        super().__init__(message or error)
        self.status = status
        self.error = error
        self.message = message or error


# ---------------------------------------------------------------------------
# Middlewares
# ---------------------------------------------------------------------------


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except ApiError as exc:
        return web.json_response(
            {"ok": False, "error": exc.error, "message": exc.message}, status=exc.status
        )
    except web.HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unhandled error while serving %s %s", request.method, request.path)
        return web.json_response(
            {"ok": False, "error": "internal_error", "message": "Something went wrong."},
            status=500,
        )


@web.middleware
async def hardening_middleware(request: web.Request, handler):
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    if request.path.startswith(("/api", "/auth")):
        response.headers.setdefault("Cache-Control", "no-store")
    if isinstance(response, web.Response) and response.content_type == "text/html":
        response.headers.setdefault("Content-Security-Policy", CSP)
    return response


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def _origin(request: web.Request, ctx: WebUIContext) -> str:
    config = ctx.config
    if config.base_url:
        return config.base_url
    scheme = request.scheme
    host = request.host
    if config.trust_proxy:
        scheme = request.headers.get("X-Forwarded-Proto", scheme)
        host = request.headers.get("X-Forwarded-Host", host)
    return f"{scheme}://{host}"


def _is_same_origin(request: web.Request, ctx: WebUIContext) -> bool:
    if ctx.config.allow_any_origin:
        return True
    origin = request.headers.get("Origin")
    if not origin:
        return True  # Non-browser client (or same-origin browser GET).
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    allowed = {request.host.lower()}
    if ctx.config.base_url:
        allowed.add(urlparse(ctx.config.base_url).netloc.lower())
    if ctx.config.trust_proxy:
        forwarded = request.headers.get("X-Forwarded-Host")
        if forwarded:
            allowed.add(forwarded.split(",")[0].strip().lower())
    return (parsed.netloc or "").lower() in allowed


def _dev_session(ctx: WebUIContext) -> dict[str, Any] | None:
    user_id = ctx.config.dev_bypass_user_id
    if not user_id:
        return None
    now = int(time.time())
    return {
        "sub": str(user_id),
        "name": "Local developer",
        "username": "developer",
        "avatar": "",
        "iat": now,
        "exp": now + ctx.config.session_ttl,
        "dev": True,
    }


def session_from_request(request: web.Request, ctx: WebUIContext) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        try:
            payload = ctx.signer.loads(token, max_age=ctx.config.session_ttl)
        except InvalidToken:
            payload = None
        if payload and payload.get("sub"):
            return payload
    return _dev_session(ctx)


def require_session(request: web.Request, ctx: WebUIContext) -> dict[str, Any]:
    payload = session_from_request(request, ctx)
    if payload is None:
        raise ApiError(401, "unauthenticated", "Sign in with Discord to continue.")
    return payload


def _user_id(payload: dict[str, Any]) -> int:
    try:
        return int(payload.get("sub") or 0)
    except (TypeError, ValueError):
        raise ApiError(401, "unauthenticated", "Invalid session.") from None


def _guild_id(value: Any) -> int:
    try:
        guild_id = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "invalid_guild", "Missing or invalid guild id.") from None
    if guild_id <= 0:
        raise ApiError(400, "invalid_guild", "Missing or invalid guild id.")
    return guild_id


def require_access(ctx: WebUIContext, payload: dict[str, Any], guild_id: int):
    session = resolve_session(ctx.bot, _user_id(payload), guild_id)
    if session is None:
        raise ApiError(
            403,
            "not_in_voice",
            "You must be in the same voice channel as Playify to use the web UI.",
        )
    return session


def _check_rate_limit(user_id: int, action: str) -> None:
    now = time.monotonic()
    cooldown = _HEAVY_COOLDOWN if action in _HEAVY_ACTIONS else _DEFAULT_COOLDOWN
    key = (user_id, action)
    last = _rate_limits.get(key, 0.0)
    if now - last < cooldown:
        raise ApiError(429, "rate_limited", "Slow down a little!")
    _rate_limits[key] = now
    if len(_rate_limits) > 4096:  # pragma: no cover - long-running instances
        _rate_limits.clear()


async def read_json(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise ApiError(400, "invalid_json", "Expected a JSON body.") from None
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_json", "Expected a JSON object.")
    return body


def _guild_state(guild_id: int) -> Any:
    from ..core import get_guild_state

    return get_guild_state(guild_id)


def _asset_url(asset: Any) -> str:
    url = getattr(asset, "url", None)
    return str(url) if url else ""


def _bot_public(ctx: WebUIContext) -> dict[str, Any]:
    user = getattr(ctx.bot, "user", None)
    return {
        "name": getattr(user, "name", None) or "Playify",
        "avatar": _asset_url(getattr(user, "display_avatar", None)),
    }


def _set_cookie(response: web.Response, name: str, value: str, *, max_age: int, ctx: WebUIContext) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        samesite="Lax",
        secure=ctx.config.secure_cookie,
        path="/",
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_index(request: web.Request) -> web.Response:
    response = web.FileResponse(STATIC_DIR / "index.html")
    response.headers["Content-Security-Policy"] = CSP
    response.headers["Cache-Control"] = "no-cache"
    return response


async def handle_health(request: web.Request) -> web.Response:
    ctx: WebUIContext = request.app["ctx"]
    return web.json_response(
        {
            "status": "ok",
            "uptime": round(time.time() - ctx.started_at, 1),
            "oauth": ctx.config.oauth_configured,
        }
    )


async def handle_login(request: web.Request) -> web.Response:
    ctx: WebUIContext = request.app["ctx"]
    if not ctx.config.oauth_configured:
        raise ApiError(
            503,
            "oauth_not_configured",
            "Discord OAuth is not configured on this instance.",
        )
    state = new_state_token()
    response = web.HTTPFound(
        ctx.oauth.authorize_url(ctx.config.redirect_uri(_origin(request, ctx)), state)
    )
    _set_cookie(response, OAUTH_STATE_COOKIE, state, max_age=ctx.config.oauth_state_ttl, ctx=ctx)
    raise response


async def handle_callback(request: web.Request) -> web.Response:
    ctx: WebUIContext = request.app["ctx"]

    error = request.query.get("error")
    if error:
        logger.info("Discord OAuth was cancelled or refused: %s", error)
        raise web.HTTPFound("/?error=oauth_cancelled")

    code = request.query.get("code") or ""
    state = request.query.get("state") or ""
    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE) or ""
    if not code or not state or not cookie_state or not hmac.compare_digest(state, cookie_state):
        raise ApiError(400, "invalid_state", "The login attempt expired. Please try again.")

    try:
        access_token = await ctx.oauth.exchange_code(code, ctx.config.redirect_uri(_origin(request, ctx)))
        user = await ctx.oauth.fetch_user(access_token)
    except OAuthError as exc:
        logger.warning("Discord OAuth failed: %s", exc)
        raise web.HTTPFound("/?error=oauth_failed") from exc

    payload = build_session_payload(user, ctx.config.session_ttl)
    response = web.HTTPFound("/")
    _set_cookie(response, SESSION_COOKIE, ctx.signer.dumps(payload), max_age=ctx.config.session_ttl, ctx=ctx)
    response.del_cookie(OAUTH_STATE_COOKIE, path="/")
    logger.info("Web UI login for %s (%s)", payload.get("name"), payload.get("sub"))
    raise response


async def handle_logout(request: web.Request) -> web.Response:
    ctx: WebUIContext = request.app["ctx"]
    if not _is_same_origin(request, ctx):
        raise ApiError(403, "bad_origin", "Cross-origin request blocked.")
    response = web.json_response({"ok": True})
    response.del_cookie(SESSION_COOKIE, path="/")
    return response


async def handle_public_config(request: web.Request) -> web.Response:
    """Unauthenticated hints so the login screen can explain itself."""
    ctx: WebUIContext = request.app["ctx"]
    return web.json_response(
        {
            "oauth": ctx.config.oauth_configured,
            "dev_bypass": bool(ctx.config.dev_bypass_user_id),
            "bot": _bot_public(ctx),
        }
    )


async def handle_me(request: web.Request) -> web.Response:
    ctx: WebUIContext = request.app["ctx"]
    payload = require_session(request, ctx)
    user_id = _user_id(payload)
    sessions = resolve_sessions(ctx.bot, user_id)
    return web.json_response(
        {
            "ok": True,
            "user": {
                "id": str(user_id),
                "name": payload.get("name") or "Listener",
                "avatar": payload.get("avatar") or "",
                "dev": bool(payload.get("dev")),
            },
            "bot": _bot_public(ctx),
            "sessions": [session.public() for session in sessions],
            "connected_channels": connected_channels(ctx.bot),
            "login": {
                "oauth": ctx.config.oauth_configured,
                "dev_bypass": bool(ctx.config.dev_bypass_user_id),
            },
        }
    )


async def handle_state(request: web.Request) -> web.Response:
    ctx: WebUIContext = request.app["ctx"]
    payload = require_session(request, ctx)
    guild_id = _guild_id(request.query.get("guild_id"))
    session = require_access(ctx, payload, guild_id)
    snapshot = build_snapshot(_guild_state(guild_id), session, started_at=ctx.started_at)
    return web.json_response({"ok": True, "state": snapshot})


async def handle_search(request: web.Request) -> web.Response:
    ctx: WebUIContext = request.app["ctx"]
    payload = require_session(request, ctx)
    guild_id = _guild_id(request.query.get("guild_id"))
    session = require_access(ctx, payload, guild_id)
    _check_rate_limit(_user_id(payload), "search")

    query = (request.query.get("q") or "").strip()
    try:
        results = await actions_module.search(ctx, session, query)
    except actions_module.ActionError as exc:
        raise ApiError(exc.status, "search_failed", exc.message) from exc
    return web.json_response({"ok": True, "results": results})


async def handle_action(request: web.Request) -> web.Response:
    ctx: WebUIContext = request.app["ctx"]
    payload = require_session(request, ctx)
    if not _is_same_origin(request, ctx):
        raise ApiError(403, "bad_origin", "Cross-origin request blocked.")

    body = await read_json(request)
    guild_id = _guild_id(body.get("guild_id"))
    session = require_access(ctx, payload, guild_id)
    action = str(body.get("action") or "")
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    _check_rate_limit(_user_id(payload), action)

    try:
        result = await actions_module.dispatch(ctx, action, data, session)
    except actions_module.ActionError as exc:
        raise ApiError(exc.status, "action_failed", exc.message) from exc
    return web.json_response(result)


async def handle_websocket(request: web.Request) -> web.WebSocketResponse:
    ctx: WebUIContext = request.app["ctx"]
    if not _is_same_origin(request, ctx):
        raise ApiError(403, "bad_origin", "Cross-origin request blocked.")
    payload = require_session(request, ctx)
    user_id = _user_id(payload)
    guild_id = _guild_id(request.query.get("guild_id"))

    session = resolve_session(ctx.bot, user_id, guild_id)
    if session is None:
        raise ApiError(
            403,
            "not_in_voice",
            "You must be in the same voice channel as Playify to use the web UI.",
        )

    ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=256 * 1024)
    await ws.prepare(request)
    send_lock = asyncio.Lock()

    async def send(payload_obj: dict[str, Any]) -> None:
        if ws.closed:
            return
        async with send_lock:
            try:
                await ws.send_json(payload_obj)
            except (ConnectionResetError, RuntimeError):
                pass

    await send(
        {
            "type": "hello",
            "user": {"id": str(user_id), "name": payload.get("name") or "Listener"},
            "guild_id": str(guild_id),
        }
    )

    stop = asyncio.Event()
    last_signature = {"value": ""}

    async def push_state(current_session: Any, *, force: bool = False) -> None:
        snapshot = build_snapshot(
            _guild_state(guild_id), current_session, started_at=ctx.started_at
        )
        signature = json.dumps(
            {k: v for k, v in snapshot.items() if k != "server_time"}, sort_keys=True
        )
        if force or signature != last_signature["value"]:
            last_signature["value"] = signature
            await send({"type": "state", "data": snapshot})

    async def pusher() -> None:
        while not stop.is_set():
            current = resolve_session(ctx.bot, user_id, guild_id)
            if current is None:
                await send(
                    {
                        "type": "revoked",
                        "message": "You are no longer in Playify's voice channel.",
                    }
                )
                stop.set()
                break
            await push_state(current)
            await asyncio.sleep(1.0)

    async def reader() -> None:
        async for message in ws:
            if message.type == WSMsgType.ERROR:
                logger.debug("WebSocket error: %s", ws.exception())
                break
            if message.type == WSMsgType.PING:
                continue
            if message.type != WSMsgType.TEXT:
                continue
            try:
                incoming = json.loads(message.data)
            except (TypeError, ValueError):
                continue
            if not isinstance(incoming, dict):
                continue

            kind = incoming.get("type")
            if kind == "ping":
                await send({"type": "pong", "t": time.time()})
                continue
            if kind != "action":
                continue

            request_id = incoming.get("request_id")
            action = str(incoming.get("action") or "")
            data = incoming.get("data") if isinstance(incoming.get("data"), dict) else {}

            current = resolve_session(ctx.bot, user_id, guild_id)
            if current is None:
                await send(
                    {
                        "type": "result",
                        "request_id": request_id,
                        "ok": False,
                        "message": "You are no longer in Playify's voice channel.",
                    }
                )
                stop.set()
                break
            try:
                _check_rate_limit(user_id, action)
                result = await actions_module.dispatch(ctx, action, data, current)
                await send(
                    {
                        "type": "result",
                        "request_id": request_id,
                        "ok": True,
                        "message": result.get("message") or "",
                        "notices": result.get("notices") or [],
                    }
                )
            except actions_module.ActionError as exc:
                await send(
                    {
                        "type": "result",
                        "request_id": request_id,
                        "ok": False,
                        "message": exc.message,
                    }
                )
            except ApiError as exc:
                await send(
                    {
                        "type": "result",
                        "request_id": request_id,
                        "ok": False,
                        "message": exc.message,
                    }
                )
            except Exception:  # pragma: no cover - safety net
                logger.exception("WebSocket action '%s' failed", action)
                await send(
                    {
                        "type": "result",
                        "request_id": request_id,
                        "ok": False,
                        "message": "That action failed. Check the bot logs.",
                    }
                )
            await push_state(current, force=True)

    tasks = [asyncio.create_task(pusher()), asyncio.create_task(reader())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not ws.closed:
            await ws.close()
    return ws


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(ctx: WebUIContext) -> web.Application:
    app = web.Application(middlewares=[error_middleware, hardening_middleware])
    app["ctx"] = ctx

    app.router.add_get("/", handle_index)
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/auth/login", handle_login)
    app.router.add_get("/auth/callback", handle_callback)
    app.router.add_post("/auth/logout", handle_logout)

    app.router.add_get("/api/config", handle_public_config)
    app.router.add_get("/api/me", handle_me)
    app.router.add_get("/api/state", handle_state)
    app.router.add_get("/api/search", handle_search)
    app.router.add_post("/api/action", handle_action)
    app.router.add_get("/ws", handle_websocket)

    async def handle_favicon(_request: web.Request) -> web.Response:
        return web.FileResponse(STATIC_DIR / "favicon.svg")

    app.router.add_get("/favicon.ico", handle_favicon)
    app.router.add_static("/static/", STATIC_DIR, show_index=False, append_version=False)

    async def _cleanup(app: web.Application) -> None:
        await ctx.oauth.close()

    app.on_cleanup.append(_cleanup)
    return app
