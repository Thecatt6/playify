"""Slash command that hands out the Web UI link."""

from ..core import *


def _user_in_bot_channel(interaction: discord.Interaction) -> bool | None:
    """``True``/``False`` when the bot is connected, ``None`` when it is not."""
    voice_client = interaction.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        return None
    channel = voice_client.channel
    if not channel:
        return None
    return any(member.id == interaction.user.id for member in channel.members)


@bot.tree.command(
    name="webui", description="Get the link to the Playify web interface."
)
async def webui_command(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message(
            get_messages("command.error.guild_only", interaction.guild_id),
            ephemeral=True,
            silent=SILENT_MESSAGES,
        )
        return

    guild_id = interaction.guild.id
    is_kawaii = get_guild_state(guild_id).locale == Locale.EN_X_KAWAII
    color = 0xB5EAD7 if is_kawaii else discord.Color.blurple()

    from ..webui.runtime import current_url, is_running

    url = current_url()
    if not is_running() or not url:
        embed = Embed(
            title=get_messages("webui.title", guild_id),
            description=get_messages("webui.not_configured", guild_id),
            color=0xFF9AA2 if is_kawaii else discord.Color.red(),
        )
        await interaction.response.send_message(
            embed=embed, ephemeral=True, silent=SILENT_MESSAGES
        )
        return

    description = get_messages(
        "webui.description", guild_id, bot_name=interaction.client.user.name
    )
    in_channel = _user_in_bot_channel(interaction)
    if in_channel is False:
        description += "\n\n" + get_messages(
            "webui.not_in_voice", guild_id, bot_name=interaction.client.user.name
        )
    elif in_channel is None:
        description += "\n\n" + get_messages("webui.not_connected", guild_id)

    embed = Embed(
        title=get_messages("webui.title", guild_id),
        description=description,
        color=color,
    )
    embed.add_field(
        name=get_messages("webui.link_label", guild_id),
        value=f"[{url}]({url})",
        inline=False,
    )
    embed.set_footer(text=get_messages("webui.footer", guild_id))

    await interaction.response.send_message(
        embed=embed, ephemeral=True, silent=SILENT_MESSAGES
    )
