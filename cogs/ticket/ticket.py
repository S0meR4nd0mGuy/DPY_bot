"""
cogs/ticket/ticket.py

Main cog for ticket-related functionality. Currently exposes a single
slash command, /transcript, which saves the calling channel's message
history (minus bot messages) to a text file.

Add further ticket commands (open, close, claim, etc.) to this same
TicketCog class as your system grows.
"""

import discord
from discord import app_commands
from discord.ext import commands

from pathlib import Path
import os
import json

from loguru import logger

from .transcript import generate_transcript

config_path = Path(os.getcwd()) / "data" / "config.json"

ticket_config_path = Path(os.getcwd()) / "data" / "Ticket" / "server_ticket_config.json"

open_tickets_path = Path(os.getcwd()) / "data" / "Ticket" / "open_tickets.json"

with open(config_path, "r", encoding="utf-8") as f:
    config = json.load(f)

DEV_GUILD_ID = config.get("devGuildId")

@app_commands.guilds(discord.Object(id=int(DEV_GUILD_ID)))
class TicketGroup(app_commands.Group):
    def __init__(self) -> None:
        super().__init__(name="ticket", description="Ticket Main Group")

    @app_commands.command(name="close", description="Close the current Ticket.")
    async def close(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        await interaction.followup.send("This command has not been fully implemented yet.", ephemeral=True)

    @app_commands.command(name="open", description="Opens a new Ticket.")
    async def open(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        await interaction.followup.send("This command has not been fully implemented yet.", ephemeral=True)




class TicketCog(commands.Cog):

    TicketGroup = TicketGroup()

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="transcript",
        description="Save a transcript of this channel's messages (bot messages excluded).",
    )
    @app_commands.guilds(discord.Object(id=int(DEV_GUILD_ID)))
    async def transcript(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)

        if str(interaction.user.id) not in config.get("devUserId"):
            logger.warning(
                f"[/transcript] blocked non-dev user {interaction.user.id} ({interaction.user}) "
                f"in guild {interaction.guild_id}"
            )
            await interaction.followup.send("You do not have access to this Command (devOnlyCommand)", ephemeral=True)
            return

        logger.info(
            f"[/transcript] generating transcript for #{interaction.channel} "
            f"({interaction.channel_id}) requested by {interaction.user} ({interaction.user.id})"
        )

        try:
            file_path = await generate_transcript(interaction.channel)
        except discord.Forbidden:
            logger.warning(f"[/transcript] missing permissions to read history of channel {interaction.channel_id}")
            await interaction.followup.send(
                "I don't have permission to read this channel's history.",
                ephemeral=True,
            )
            return
        except Exception:
            logger.exception(f"[/transcript] failed to generate transcript for channel {interaction.channel_id}")
            await interaction.followup.send("Something went wrong generating the transcript.", ephemeral=True,)
            return

        logger.info(f"[/transcript] saved transcript to {file_path}")
        await interaction.followup.send(f"Transcript saved: `{file_path}`", ephemeral=True,)


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketCog(bot))
    logger.debug("[ticket cog] TicketCog loaded")