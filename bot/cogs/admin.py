### ADMIN UTILITY COMMANDS ###
# Operational "break glass" tools - not for day-to-day division management
# (that's what the other cogs are for), but for when something's gone wrong
# with the bot itself: a bad edit needs testing without a full restart, the
# command tree is stale, a view stopped responding, or someone just wants a
# safety-net copy of the database before doing something risky. Every
# command here is is_admin() - restricted to bot.config.ADMIN_USERS, not
# the wider staff tier - since these can affect the whole bot process, not
# just one drill/event/player.
#
# Deliberately NOT included: a raw-SQL or eval-style command. Something that
# executes arbitrary code/SQL from a Discord command is a much bigger risk
# than the mistakes it'd be fixing - use a real DB tool against the file
# from /admin_db_backup for anything that specific.

import sqlite3
import tempfile
from pathlib import Path

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from bot.shared import *  # noqa: F401,F403


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="admin_db_backup",
        description="[Admin] Download a snapshot of the current database file.",
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.default_permissions(administrator=True)
    @is_admin()
    async def admin_db_backup(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)

        conn.commit()

        # sqlite3's backup API takes a live, consistent snapshot even while
        # the bot keeps using `conn` concurrently - copying the .db file
        # directly could grab it mid-write and produce a corrupt copy.
        with tempfile.TemporaryDirectory() as tmp_dir:
            backup_path = Path(tmp_dir) / f"leaderboard_{DB_YEAR}_backup.db"
            backup_conn = sqlite3.connect(backup_path)
            try:
                conn.backup(backup_conn)
            finally:
                backup_conn.close()

            await interaction.followup.send(
                "Here's a snapshot of the current database ✅\n"
                "-# Good idea to grab one of these before anything risky (bulk edits, a season reset, etc.)",
                file=discord.File(backup_path),
                ephemeral=True
            )


    @app_commands.command(
        name="admin_reload_cog",
        description="[Admin] Hot-reload one cog's code without restarting the whole bot.",
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.choices(
        cog=[app_commands.Choice(name=extension.split(".")[-1], value=extension) for extension in COGS]
    )
    @app_commands.default_permissions(administrator=True)
    @is_admin()
    async def admin_reload_cog(self, interaction: Interaction, cog: str):
        try:
            await self.bot.reload_extension(cog)
        except commands.ExtensionError as e:
            await interaction.response.send_message(f"Could not reload `{cog}`: {e}", ephemeral=True)
            return

        try:
            synced = await self.bot.tree.sync(guild=GUILD)
            sync_note = f", {len(synced)} command(s) re-synced"
        except Exception as e:
            sync_note = f" (command re-sync failed: {e})"

        await interaction.response.send_message(f"Reloaded `{cog}`{sync_note} ✅", ephemeral=True)


    @app_commands.command(
        name="admin_resync_commands",
        description="[Admin] Force a full slash command re-sync, without restarting the bot.",
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.default_permissions(administrator=True)
    @is_admin()
    async def admin_resync_commands(self, interaction: Interaction):
        try:
            synced = await self.bot.tree.sync(guild=GUILD)
        except Exception as e:
            await interaction.response.send_message(f"Could not sync commands: {e}", ephemeral=True)
            return

        await interaction.response.send_message(
            f"{len(synced)} command{'s' if len(synced) != 1 else ''} synchronized ✅", ephemeral=True
        )


    @app_commands.command(
        name="admin_restore_views",
        description="[Admin] Re-attach persistent button views (Join Event, drill rosters, submission reviews) on demand.",
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.default_permissions(administrator=True)
    @is_admin()
    async def admin_restore_views(self, interaction: Interaction):
        restored = restore_all_views()

        lines = [
            f"- Join Event view: {'restored' if restored['join_event'] else 'nothing to restore (no active event)'}",
            f"- Submission review views: {restored['submission_reviews']} restored",
            f"- Drill roster views: {restored['drills']} restored",
        ]
        await interaction.response.send_message(
            "Persistent views re-attached ✅\n" + "\n".join(lines), ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
