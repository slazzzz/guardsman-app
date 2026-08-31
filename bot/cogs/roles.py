### ROLE COMMANDS ###

import discord
from discord import Embed, Interaction, Member, app_commands
from discord.ext import commands

from bot.shared import *

# Each entry is a mutually-exclusive tier ladder - adding a role from one of
# these lists to a member auto-removes any other role they hold from that
# same list first (a "promotion" swap rather than stacking tiers). Guardsman
# Access is handled as a side effect only for RANK_ROLE_IDS specifically
# (see guardsman_role_add/remove below) - the achievement ladders
# (Endless Record/Win/Firewall) don't touch it.
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
    @is_admin_or_staff_or_helper()
    async def guardsman_role_add(self, interaction: Interaction, member: Member, role: discord.Role):
        if not any(role.id in role_id_tiers for role_id_tiers in ROLE_ID_TIERS_CATEGORIES):
            await interaction.response.send_message("That role is not a valid Guardsman role.", ephemeral=True)
            return

        member_roles = [r.id for r in member.roles]

        if role.id in member_roles:
            await interaction.response.send_message(f"{member.mention} already has the {role.mention} role.", ephemeral=True)
            return

        for role_id_tiers in ROLE_ID_TIERS_CATEGORIES:
            if role.id not in role_id_tiers:
                continue

            # For the rank ladder specifically: no existing role from this
            # category means the applicant is brand new to the division, so
            # Guardsman Access (the role that actually carries channel
            # perms now) needs to be granted alongside their qualifying
            # tier - staff handing out a tier role in the ticket is what
            # used to grant channel access on its own, so this keeps that
            # one action doing the same job it always did.
            needs_access_role = (
                role_id_tiers is TIER_ROLES
                and GUARDSMAN_ACCESS_ROLE
                and GUARDSMAN_ACCESS_ROLE not in member_roles
                and not any(r in TIER_ROLES for r in member_roles)
            )

            for r in role_id_tiers:
                if r in member_roles:
                    target_role = interaction.guild.get_role(r)
                    await member.remove_roles(target_role)

            roles_to_add = [role]
            access_note = ""
            if needs_access_role:
                access_role = interaction.guild.get_role(GUARDSMAN_ACCESS_ROLE)
                if access_role:
                    roles_to_add.append(access_role)
                    access_note = f" and granted {access_role.mention} (first rank role)"

            await member.add_roles(*roles_to_add)
            await interaction.response.send_message(f"Added {role.mention} to {member.mention}{access_note}.", ephemeral=True)
            return


    @app_commands.command(
        name="guardsman_role_remove",
        description="Remove a Guardsman role from a user."
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.describe(
        member="The member to remove the role from.",
        role="The Guardsman role to remove."
    )
    @is_admin_or_staff_or_helper()
    async def guardsman_role_remove(self, interaction: Interaction, member: Member, role: discord.Role):
        if not any(role.id in role_id_tiers for role_id_tiers in ROLE_ID_TIERS_CATEGORIES):
            await interaction.response.send_message("That role is not a valid Guardsman role.", ephemeral=True)
            return

        member_roles = [r.id for r in member.roles]

        if not role.id in member_roles:
            await interaction.response.send_message(f"{member.mention} does not have the {role.mention} role.", ephemeral=True)
            return

        await member.remove_roles(role)

        # If that was their last rank role, they've fully left the division
        # tier ladder - strip Guardsman Access too rather than leaving
        # channel access behind with no tier role to justify it.
        access_note = ""
        if role.id in TIER_ROLES and GUARDSMAN_ACCESS_ROLE and GUARDSMAN_ACCESS_ROLE in member_roles:
            remaining_rank_roles = [r for r in member_roles if r != role.id and r in TIER_ROLES]
            if not remaining_rank_roles:
                access_role = interaction.guild.get_role(GUARDSMAN_ACCESS_ROLE)
                if access_role:
                    await member.remove_roles(access_role)
                    access_note = f" and removed {access_role.mention} (no rank roles remaining)"

        await interaction.response.send_message(f"Removed {role.mention} from {member.mention}{access_note}.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(RolesCog(bot))