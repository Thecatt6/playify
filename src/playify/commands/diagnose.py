"""``/diagnose`` — reports why playback might be silent, straight into Discord.

The most common "the bot is in the channel but I hear nothing" reports are a
missing voice dependency (ffmpeg/PyNaCl/davey), a dead voice connection, a
volume stuck at 0 %, a failed audio source — or the bot being muted by the
server while everything else looks perfect. All of that is visible here.

It ships both as a slash command (``/diagnose``) and as a classic prefixed
command (``!diagnose``, alias ``!diag``): global slash commands can take a while
before a client sees them, and the prefixed one works immediately.
"""

from typing import Any

from ..core import *

# Anything quieter than this (in dBFS) is indistinguishable from silence.
_SILENT_DB = -60.0


def _format_seconds(value: float) -> str:
    total = max(0, int(value or 0))
    return f"{total // 60}:{total % 60:02d}"


def _ffmpeg_process(voice_client: discord.VoiceClient) -> str:
    """State of the FFmpeg process behind the current source, when readable."""
    source = getattr(voice_client, "source", None)
    while hasattr(source, "original"):
        source = source.original
    process = getattr(source, "_process", None) or getattr(source, "process", None)
    if process is None:
        return "unknown source"
    poll = getattr(process, "poll", None)
    code = poll() if callable(poll) else None
    if code is None:
        return f"running (pid {getattr(process, 'pid', '?')})"
    return f"exited with code {code}"


def _bot_voice_line(member: Any) -> str | None:
    """Mute flags that actually stop a *bot* from being heard.

    Being self-deafened is the default for a music bot and only affects the
    audio the bot receives, so it is deliberately not reported here.
    """
    voice = getattr(member, "voice", None)
    if voice is None:
        return None
    flags = []
    if getattr(voice, "mute", False):
        flags.append("**server-muted**")
    if getattr(voice, "self_mute", False):
        flags.append("self-muted")
    channel = getattr(voice, "channel", None)
    where = f"**#{channel.name}**" if channel is not None else "not in a voice channel"
    return f"{where} ({', '.join(flags)})" if flags else where


async def _probe_loudness(player: Any) -> str | None:
    """Measure the loudness of the current stream with FFmpeg's ``volumedetect``.

    This answers the only question left when the bot is connected, playing and
    unmuted: is there actually sound in the audio we are sending?
    """
    info = getattr(player, "current_info", None) or {}
    url = info.get("url")
    ffmpeg = shutil.which("ffmpeg")
    if not url or not ffmpeg or info.get("source_type") == "file":
        return None

    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-t",
            "4",
            "-i",
            url,
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    except asyncio.TimeoutError:
        if process is not None:
            process.kill()
        return "measurement timed out (slow network)"
    except Exception as error:  # pragma: no cover - environment specific
        return f"could not measure ({type(error).__name__})"

    text = (stderr or b"").decode("utf-8", "replace")
    mean = max_ = None
    for line in text.splitlines():
        if "mean_volume:" in line:
            mean = line.split("mean_volume:")[1].split("dB")[0].strip()
        elif "max_volume:" in line:
            max_ = line.split("max_volume:")[1].split("dB")[0].strip()
    if mean is None:
        return "could not measure (FFmpeg gave no volume data)"

    try:
        loud = float(max_ if max_ is not None else mean)
    except ValueError:
        return f"mean {mean} dB"

    verdict = "silent ❌" if loud <= _SILENT_DB else "audible ✅"
    return f"{verdict} — mean {mean} dB, peak {max_ or mean} dB"


def build_diagnose_embed(guild: discord.Guild, user: Any = None) -> Embed:
    """Everything we know about voice playback in ``guild``, as one embed."""
    from ..services.voice_health import (
        playback_position,
        voice_dependencies,
        voice_readiness_problems,
    )

    guild_id = guild.id
    state = get_guild_state(guild_id)
    player = state.music_player
    player_vc = getattr(player, "voice_client", None)
    guild_vc = getattr(guild, "voice_client", None)
    # The audio is emitted by the client the *player* holds, which is not
    # necessarily the one Discord shows in the server.
    voice_client = player_vc or guild_vc
    deps = voice_dependencies()
    problems = voice_readiness_problems()

    embed = Embed(
        title="🎧 Playify diagnostics",
        color=discord.Color.red() if problems else discord.Color.blurple(),
    )

    # --- voice connection -------------------------------------------------
    if voice_client is None:
        connection = "❌ the bot is **not** connected to a voice channel"
    elif not voice_client.is_connected():
        connection = "❌ the voice client is dead (zombie connection)"
    else:
        channel_name = getattr(getattr(voice_client, "channel", None), "name", "voice")
        connection = f"✅ connected to **{channel_name}**"

    me = getattr(guild, "me", None)
    me_state = _bot_voice_line(me) if me is not None else None
    if me_state:
        connection += f"\nbot: {me_state}"
        voice = getattr(me, "voice", None)
        if getattr(voice, "mute", False):
            connection += (
                "\n⚠️ **A moderator server-muted the bot**: it is transmitting silence. "
                "Unmute it in the member list (right-click → Unmute)."
            )
        if getattr(voice, "self_mute", False):
            connection += "\n⚠️ the bot is self-muted: it transmits no audio."
    if player_vc is not None and guild_vc is not None and player_vc is not guild_vc:
        connection += (
            "\n⚠️ the player is attached to a **different voice client** than the one in the "
            "server: audio goes to the old connection. Run `/reconnect`."
            f" (player connected: {player_vc.is_connected()}, "
            f"server connected: {guild_vc.is_connected()})"
        )
    embed.add_field(name="Connection", value=connection, inline=False)

    # --- you --------------------------------------------------------------
    if user is not None:
        member = user
        if not hasattr(user, "voice") and hasattr(guild, "get_member"):
            member = guild.get_member(user.id) or user
        voice = getattr(member, "voice", None)
        if voice is None:
            you = "not in a voice channel"
        else:
            channel = getattr(voice, "channel", None)
            you = f"**#{channel.name}**" if channel is not None else "not in a voice channel"
            bot_channel = getattr(voice_client, "channel", None) or getattr(
                getattr(me, "voice", None), "channel", None
            )
            if channel is not None and bot_channel is not None and channel.id != bot_channel.id:
                you += f" — the bot is in **#{bot_channel.name}**, a different channel"
            flags = []
            if getattr(voice, "self_mute", False):
                flags.append("your mic is muted (harmless for listening)")
            if getattr(voice, "self_deaf", False):
                flags.append("**you are deafened: you hear nothing at all**")
            if getattr(voice, "deaf", False):
                flags.append("you are server-deafened")
            if flags:
                you += "\n" + ", ".join(flags)
        embed.add_field(name="You", value=you, inline=False)

    # --- playback ---------------------------------------------------------
    info = player.current_info or {}
    if voice_client is not None and voice_client.is_playing():
        position = playback_position(player)
        playback = (
            f"▶️ **{info.get('title') or 'unknown track'}** — "
            f"{_format_seconds(position)} / {_format_seconds(info.get('duration'))}"
        )
    elif voice_client is not None and voice_client.is_paused():
        playback = "⏸️ paused"
    else:
        playback = "⏹️ nothing is playing"
    embed.add_field(name="Playback", value=playback, inline=False)

    # --- the usual culprits ----------------------------------------------
    volume = round(float(getattr(player, "volume", 1.0) or 0.0) * 100)
    volume_line = f"{volume}%"
    if volume < 2:
        volume_line += "  ⚠️ **silent** — raise it with `/volume`"
    embed.add_field(name="Volume", value=volume_line, inline=True)

    embed.add_field(
        name="Filters",
        value=", ".join(sorted(getattr(state, "server_filters", set()) or [])) or "none",
        inline=True,
    )
    embed.add_field(
        name="24/7 keep-alive",
        value="on" if getattr(state, "_24_7_mode", False) else "off",
        inline=True,
    )

    if voice_client is not None and (voice_client.is_playing() or voice_client.is_paused()):
        embed.add_field(
            name="Audio source", value=_ffmpeg_process(voice_client), inline=False
        )

    last_error = getattr(player, "last_error", None)
    if last_error:
        embed.add_field(
            name="Last playback error",
            value=f"```\n{str(last_error)[:300]}\n```",
            inline=False,
        )

    # --- dependencies -----------------------------------------------------
    ffmpeg = deps.get("ffmpeg")
    davey_version = deps.get("davey_version")
    if not deps.get("davey"):
        davey_line = "❌ missing (E2EE channels need it)"
    elif deps.get("dave_compatible", True):
        davey_line = f"✅ {davey_version or 'installed'}"
    else:
        davey_line = (
            f"❌ {davey_version} — cannot rotate DAVE keys with discord.py "
            f"{deps.get('discord_py_version') or '?'}: **every listener hears silence**"
        )
    lines = [
        f"ffmpeg: {'✅ ' + str(ffmpeg) if ffmpeg else '❌ not found in PATH'}",
        f"PyNaCl: {'✅ installed' if deps.get('pynacl') else '❌ missing'}",
        "davey (E2EE voice): " + davey_line,
        f"yt-dlp: {'✅ installed' if deps.get('yt_dlp') else '❌ missing'}",
        f"discord.py: {deps.get('discord_py_version') or 'unknown'}",
    ]
    embed.add_field(name="Dependencies", value="\n".join(lines), inline=False)

    if problems:
        fix = "\n".join(f"• {problem}" for problem in problems)
        if not deps.get("dave_compatible", True):
            fix += '\n```\npip install "davey>=0.1.6,<0.2"\n```'
        else:
            fix += "\n```\npip install -r requirements.txt\n```"
        embed.add_field(name="Fix", value=fix, inline=False)

    if not deps.get("dave_compatible", True):
        embed.color = discord.Color.red()

    embed.set_footer(
        text="Everything ✅ but still quiet? Right-click the bot in the voice channel "
        "→ Volume (a per-user slider, invisible to the bot), and check your output device."
    )
    return embed


async def build_diagnose_embed_async(guild: discord.Guild, user: Any = None) -> Embed:
    """:func:`build_diagnose_embed` plus the loudness measurement of the stream."""
    embed = build_diagnose_embed(guild, user)

    state = get_guild_state(guild.id)
    player = state.music_player
    voice_client = getattr(player, "voice_client", None) or guild.voice_client
    if voice_client is not None and voice_client.is_playing():
        probe = await _probe_loudness(player)
        if probe:
            embed.insert_field_at(1, name="Audio measurement", value=probe, inline=False)
    return embed


@bot.tree.command(
    name="diagnose",
    description="Check the voice connection, volume and playback dependencies.",
)
async def diagnose_command(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message(
            get_messages("command.error.guild_only", interaction.guild_id),
            ephemeral=True,
            silent=SILENT_MESSAGES,
        )
        return

    # The loudness probe takes a couple of seconds: acknowledge first.
    await interaction.response.defer(ephemeral=True, thinking=True)
    embed = await build_diagnose_embed_async(interaction.guild, interaction.user)
    await interaction.followup.send(embed=embed, ephemeral=True, silent=SILENT_MESSAGES)


@bot.command(name="diagnose", aliases=["diag"])
async def diagnose_text(ctx: commands.Context):
    """Prefix fallback (``!diagnose``): the slash command can lag behind a deploy."""
    if not ctx.guild:
        await ctx.send("This command can only be used inside a server.")
        return

    embed = await build_diagnose_embed_async(ctx.guild, ctx.author)
    await ctx.send(embed=embed)
