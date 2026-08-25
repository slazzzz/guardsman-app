### ROLE COMMANDS ###

import discord
from discord import Embed, Interaction, Member, app_commands
from discord.ext import commands

from bot.shared import *

class RolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="guardsman_role_add",
        description="Add a Guardsman role to a user."
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.describe(
        member="The member to add the role to.",
        role="The Guardsman role to add."
    )
    @is_admin_or_staff()
    async def guardsman_role_add(self, interaction: Interaction, member: Member, role: discord.Role):
        if role.id not in ENDLESS_RECORD_ROLE_IDS and role.id not in WIN_ROLE_IDS and role.id not in ENDLESS_FIREWALL_ROLE_IDS:
            await interaction.response.send_message("That role is not a valid Guardsman role.", ephemeral=True)
            return

        await member.add_roles(role)
        await interaction.response.send_message(f"Added {role.mention} to {member.mention}.", ephemeral=True)

    @app_commands.command(
        name="guardsman_role_remove",
        description="Remove a Guardsman role from a user."
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.describe(
        member="The member to remove the role from.",
        role="The Guardsman role to remove."
    )
    @is_admin_or_staff()
    async def guardsman_role_remove(self, interaction: Interaction, member: Member, role: discord.Role):
        if role.id not in ENDLESS_RECORD_ROLE_IDS and role.id not in WIN_ROLE_IDS and role.id not in ENDLESS_FIREWALL_ROLE_IDS:
            await interaction.response.send_message("That role is not a valid Guardsman role.", ephemeral=True)
            return

        await member.remove_roles(role)
        await interaction.response.send_message(f"Removed {role.mention} from {member.mention}.", ephemeral=True)