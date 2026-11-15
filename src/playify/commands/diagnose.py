"""``/diagnose`` — reports why playback might be silent, straight into Discord.

The most common "the bot is in the channel but I hear nothing" reports are a
missing voice dependency (ffmpeg/PyNaCl/davey), a dead voice connection, a
volume stuck at 0 % or a failed audio source. All of that is visible here, which
saves a round trip to the logs.

It ships both as a slash command (``/diagnose``) and as a classic prefixed
command (``!diagnose``, alias ``!diag``): global slash commands can take a while
before a client sees them, and the prefixed one works immediately.
"""

from ..core import *


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


def build_diagnose_embed(guild: discord.Guild) -> Embed:
    """Everything we know about voice playback in ``guild``, as one embed."""
    from ..services.voice_health import (
        playback_position,
        voice_dependencies,
        voice_readiness_problems,
    )

    guild_id = guild.id
    state = get_guild_state(guild_id)
    player = state.music_player
    voice_client = guild.voice_client or player.voice_client
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
    embed.add_field(name="Connection", value=connection, inline=False)

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
    lines = [
        f"ffmpeg: {'✅ ' + str(ffmpeg) if ffmpeg else '❌ not found in PATH'}",
        f"PyNaCl: {'✅ installed' if deps.get('pynacl') else '❌ missing'}",
        f"davey: {'✅ installed' if deps.get('davey') else '❌ missing (E2EE channels need it)'}",
        f"yt-dlp: {'✅ installed' if deps.get('yt_dlp') else '❌ missing'}",
    ]
    embed.add_field(name="Dependencies", value="\n".join(lines), inline=False)

    if problems:
        embed.add_field(
            name="Fix",
            value="\n".join(f"• {problem}" for problem in problems)
            + "\n```\npip install -r requirements.txt\n```",
            inline=False,
        )

    embed.set_footer(
        text="If everything is ✅ but you still hear nothing, check Discord: "
        "server mute/deafen, channel region, or the bot being self-deafened."
    )
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

    await interaction.response.send_message(
        embed=build_diagnose_embed(interaction.guild),
        ephemeral=True,
        silent=SILENT_MESSAGES,
    )


@bot.command(name="diagnose", aliases=["diag"])
async def diagnose_text(ctx: commands.Context):
    """Prefix fallback (``!diagnose``): the slash command can lag behind a deploy."""
    if not ctx.guild:
        await ctx.send("This command can only be used inside a server.")
        return

    await ctx.send(embed=build_diagnose_embed(ctx.guild))
