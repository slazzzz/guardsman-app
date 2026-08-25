### ROLE COMMANDS ###

import discord
from discord import Embed, Interaction, Member, app_commands
from discord.ext import commands

from bot.shared import *

ROLE_ID_TIERS_CATEGORIES = [ENDLESS_RECORD_ROLE_IDS, WIN_ROLE_IDS, ENDLESS_FIREWALL_ROLE_IDS]

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
    async def guardsman_role_update(self, interaction: Interaction, member: Member, role: discord.Role):
        if role.id not in ENDLESS_RECORD_ROLE_IDS and role.id not in WIN_ROLE_IDS and role.id not in ENDLESS_FIREWALL_ROLE_IDS:
            await interaction.response.send_message("That role is not a valid Guardsman role.", ephemeral=True)
            return

        member_roles = [r.id for r in member.roles]

        if role.id in member_roles:
            await interaction.response.send_message(f"{member.mention} already has the {role.mention} role.", ephemeral=True)
            return

        for role_id_tiers in ROLE_ID_TIERS_CATEGORIES:
            if role.id in role_id_tiers:
                for r in role_id_tiers:
                    if r in member_roles:
                        target_role = interaction.guild.get_role(r)
                        await member.remove_roles(target_role)
                await member.add_roles(role)
                await interaction.response.send_message(f"Added {role.mention} to {member.mention}.", ephemeral=True)
                return

async def setup(bot: commands.Bot):
    await bot.add_cog(RolesCog(bot))