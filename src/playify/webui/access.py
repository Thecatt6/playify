"""Voice-channel based access control for the Playify Web UI.

The rule is simple and strict: a visitor can only see and control a player if
they are *currently* connected to the very same voice channel as the bot. Every
check is re-evaluated live against the bot's voice state, so leaving the channel
revokes access immediately.

All objects are accessed through duck typing on purpose (``.voice_clients``,
``.channel``, ``.members`` …) so this module stays importable without discord.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger("playify.webui")


@dataclass
class VoiceSession:
    """A guild where both the bot and the visitor are in the same voice channel."""

    guild_id: int
    guild_name: str
    guild_icon: str | None
    channel_id: int
    channel_name: str
    listeners: list[dict[str, Any]] = field(default_factory=list)
    member_name: str = ""
    is_manager: bool = False
    connected: bool = True
    member: Any = field(default=None, repr=False, compare=False)
    voice_client: Any = field(default=None, repr=False, compare=False)

    def public(self) -> dict[str, Any]:
        return {
            "guild_id": str(self.guild_id),
            "guild_name": self.guild_name,
            "guild_icon": self.guild_icon,
            "channel_id": str(self.channel_id),
            "channel_name": self.channel_name,
            "listeners": self.listeners,
            "listener_count": len([m for m in self.listeners if not m["bot"]]),
            "is_manager": self.is_manager,
            # False means the gateway still shows the bot in the channel but its
            # voice client died ("zombie"): the UI offers a reconnect button.
            "connected": self.connected,
        }


def _asset_url(asset: Any) -> str | None:
    url = getattr(asset, "url", None)
    if url:
        return str(url)
    if isinstance(asset, str) and asset.startswith("http"):
        return asset
    return None


def _display_name(member: Any) -> str:
    return (
        getattr(member, "display_name", None)
        or getattr(member, "global_name", None)
        or getattr(member, "name", None)
        or "Listener"
    )


def _member_payload(member: Any) -> dict[str, Any]:
    avatar = getattr(member, "display_avatar", None)
    avatar_url = _asset_url(avatar) or _asset_url(getattr(member, "avatar", None)) or ""
    return {
        "id": str(getattr(member, "id", "")),
        "name": _display_name(member),
        "avatar": avatar_url,
        "bot": bool(getattr(member, "bot", False)),
    }


def _is_connected(voice_client: Any) -> bool:
    check = getattr(voice_client, "is_connected", None)
    try:
        return bool(check()) if callable(check) else bool(check)
    except Exception:  # pragma: no cover - defensive
        return False


def _bot_voice_channel(guild: Any) -> Any:
    """Where the *gateway* believes the bot is.

    ``guild.me.voice.channel`` is updated from voice state events, so it stays
    valid even when the voice client itself is dead (zombie connection). That is
    the exact situation where the Web UI used to answer "you are not in the
    channel" while Discord was happily showing the bot in it.
    """
    me = getattr(guild, "me", None)
    voice = getattr(me, "voice", None)
    channel = getattr(voice, "channel", None)
    if channel is not None:
        return channel
    return getattr(getattr(guild, "voice_client", None), "channel", None)


def _connected_voice_clients(bot: Any) -> Iterable[Any]:
    """Voice clients that still know which channel they belong to."""
    clients = getattr(bot, "voice_clients", None) or []
    try:
        clients = list(clients)
    except TypeError:  # pragma: no cover - defensive
        return []
    for vc in clients:
        channel = getattr(vc, "channel", None)
        if channel is None:
            guild = getattr(vc, "guild", None)
            channel = _bot_voice_channel(guild) if guild is not None else None
        if channel is None:
            continue
        yield vc


def _iter_bot_channels(bot: Any) -> Iterable[tuple[Any, Any, Any]]:
    """Yield ``(guild, channel, voice_client)`` for every guild the bot is in.

    Union of ``bot.voice_clients`` and the gateway voice states, so a single
    stale object can never hide the bot from the access checks.
    """
    seen: set[tuple[Any, Any]] = set()

    for vc in _connected_voice_clients(bot):
        channel = getattr(vc, "channel", None)
        guild = getattr(channel, "guild", None) if channel is not None else None
        if guild is None:
            guild = getattr(vc, "guild", None)
            channel = _bot_voice_channel(guild) if guild is not None else None
        if guild is None or channel is None:
            continue
        key = (getattr(guild, "id", None), getattr(channel, "id", None))
        if key in seen:
            continue
        seen.add(key)
        yield guild, channel, vc

    for guild in getattr(bot, "guilds", None) or []:
        channel = _bot_voice_channel(guild)
        if channel is None:
            continue
        key = (getattr(guild, "id", None), getattr(channel, "id", None))
        if key in seen:
            continue
        seen.add(key)
        yield guild, channel, getattr(guild, "voice_client", None)


def _voice_state_members(channel: Any) -> list[Any]:
    """Everyone the gateway reports as connected to ``channel``.

    ``VoiceChannel.members`` relies on the member cache, which can be empty on
    servers where the ``members`` intent is unavailable. The raw voice state map
    is authoritative: ``voice_state.member`` may be ``None`` there, so we fall
    back to ``voice_state.user`` (always present) instead of dropping the entry.
    """
    guild = getattr(channel, "guild", None)
    states = getattr(guild, "voice_states", None)
    if not isinstance(states, dict):
        return []

    channel_id = getattr(channel, "id", None)
    get_member = getattr(guild, "get_member", None)
    members = []
    for user_id, voice_state in states.items():
        state_channel = getattr(voice_state, "channel", None)
        if state_channel is None or getattr(state_channel, "id", None) != channel_id:
            continue
        member = getattr(voice_state, "member", None)
        if member is None:
            member = getattr(voice_state, "user", None)
        if member is None and callable(get_member):
            member = get_member(user_id)
        if member is not None:
            members.append(member)
    return members


def _channel_members(channel: Any) -> list[Any]:
    """Everyone currently connected to ``channel`` (deduplicated, cache-safe)."""
    found: dict[Any, Any] = {}
    try:
        for member in list(getattr(channel, "members", None) or []):
            found[getattr(member, "id", None)] = member
    except TypeError:  # pragma: no cover - defensive
        pass
    for member in _voice_state_members(channel):
        found.setdefault(getattr(member, "id", None), member)
    return [member for key, member in found.items() if key is not None]


def _find_member(channel: Any, user_id: int) -> Any:
    """Return the member/user for ``user_id`` if they are in ``channel``."""
    for member in _channel_members(channel):
        if getattr(member, "id", None) == user_id:
            return member

    # Fallback: the member cache may not contain the user at all.
    guild = getattr(channel, "guild", None)
    states = getattr(guild, "voice_states", None)
    if isinstance(states, dict):
        voice_state = states.get(user_id)
        member_channel = getattr(voice_state, "channel", None) if voice_state else None
        if member_channel is not None and getattr(member_channel, "id", None) == getattr(
            channel, "id", None
        ):
            member = getattr(voice_state, "member", None) or getattr(
                voice_state, "user", None
            )
            if member is not None:
                return member

    get_member = getattr(guild, "get_member", None)
    if callable(get_member):
        member = get_member(user_id)
        voice = getattr(member, "voice", None) if member else None
        member_channel = getattr(voice, "channel", None)
        if member_channel is not None and getattr(member_channel, "id", None) == getattr(
            channel, "id", None
        ):
            return member
    return None


def resolve_sessions(bot: Any, user_id: int) -> list[VoiceSession]:
    """Every guild where ``user_id`` shares a voice channel with the bot."""
    sessions: list[VoiceSession] = []
    for guild, channel, vc in _iter_bot_channels(bot):
        member = _find_member(channel, user_id)
        if member is None:
            continue

        # Permissions are only available on cached Member objects; fall back to
        # the guild cache when the voice state gave us a bare User.
        permissions = getattr(member, "guild_permissions", None)
        if permissions is None:
            get_member = getattr(guild, "get_member", None)
            cached = get_member(user_id) if callable(get_member) else None
            permissions = getattr(cached, "guild_permissions", None)

        guild_id = int(getattr(guild, "id", 0) or 0)
        sessions.append(
            VoiceSession(
                guild_id=guild_id,
                guild_name=str(getattr(guild, "name", "Server")),
                guild_icon=_asset_url(getattr(guild, "icon", None)),
                channel_id=int(getattr(channel, "id", 0) or 0),
                channel_name=str(getattr(channel, "name", "Voice")),
                listeners=[
                    _member_payload(m) for m in _channel_members(channel)
                ],
                member_name=_display_name(member),
                is_manager=bool(getattr(permissions, "manage_guild", False)),
                connected=_is_connected(vc),
                member=member,
                voice_client=vc,
            )
        )

    sessions.sort(key=lambda s: s.guild_name.lower())
    return sessions


def resolve_session(bot: Any, user_id: int, guild_id: int) -> VoiceSession | None:
    for session in resolve_sessions(bot, user_id):
        if session.guild_id == guild_id:
            return session
    return None


def connected_channels(bot: Any) -> list[dict[str, Any]]:
    """Public description of every channel the bot is currently playing in."""
    channels = []
    seen: set[Any] = set()
    for guild, channel, vc in _iter_bot_channels(bot):
        key = (getattr(guild, "id", None), getattr(channel, "id", None))
        if key in seen:
            continue
        seen.add(key)
        channels.append(
            {
                "guild_id": str(getattr(guild, "id", "")),
                "guild_name": str(getattr(guild, "name", "Server")),
                "guild_icon": _asset_url(getattr(guild, "icon", None)),
                "channel_id": str(getattr(channel, "id", "")),
                "channel_name": str(getattr(channel, "name", "Voice")),
                "connected": _is_connected(vc),
            }
        )
    return channels
