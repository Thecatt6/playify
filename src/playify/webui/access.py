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


def _connected_voice_clients(bot: Any) -> Iterable[Any]:
    clients = getattr(bot, "voice_clients", None) or []
    try:
        clients = list(clients)
    except TypeError:  # pragma: no cover - defensive
        return []
    for vc in clients:
        channel = getattr(vc, "channel", None)
        if channel is None:
            continue
        is_connected = getattr(vc, "is_connected", None)
        if callable(is_connected) and not is_connected():
            continue
        yield vc


def _voice_state_members(channel: Any) -> list[Any]:
    """Members derived from ``guild.voice_states``.

    ``VoiceChannel.members`` relies on the member cache; on large servers with
    the ``members`` intent disabled some entries can be missing, so the raw
    voice-state map is used as an extra source of truth.
    """
    guild = getattr(channel, "guild", None)
    states = getattr(guild, "voice_states", None)
    get_member = getattr(guild, "get_member", None)
    if not isinstance(states, dict) or not callable(get_member):
        return []

    channel_id = getattr(channel, "id", None)
    members = []
    for user_id, voice_state in states.items():
        state_channel = getattr(voice_state, "channel", None)
        if state_channel is None or getattr(state_channel, "id", None) != channel_id:
            continue
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
    """Return the voice-state member for ``user_id`` if they are in ``channel``."""
    for member in _channel_members(channel):
        if getattr(member, "id", None) == user_id:
            return member

    # Fallback: the voice state cache may be incomplete on very large guilds.
    guild = getattr(channel, "guild", None)
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
    for vc in _connected_voice_clients(bot):
        channel = getattr(vc, "channel", None)
        guild = getattr(channel, "guild", None)
        if guild is None:
            continue

        member = _find_member(channel, user_id)
        if member is None:
            continue

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
                is_manager=bool(
                    getattr(getattr(member, "guild_permissions", None), "manage_guild", False)
                ),
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
    for vc in _connected_voice_clients(bot):
        channel = getattr(vc, "channel", None)
        guild = getattr(channel, "guild", None)
        if guild is None:
            continue
        channels.append(
            {
                "guild_name": str(getattr(guild, "name", "Server")),
                "channel_name": str(getattr(channel, "name", "Voice")),
            }
        )
    return channels
