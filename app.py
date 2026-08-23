### GUARDSMAN APP - ENTRYPOINT ###
# This file just wires everything together and starts the bot. Feature code
# lives in bot/ - see bot/cogs/ for the actual slash commands, grouped by
# domain (events, players, teams, seasons).

import logging

import discord
from discord import Interaction, app_commands

from bot.client import bot, handler, token, tree
from bot.config import GUILD, GUILD_ID, MEMBER_ROLES, bot_data
from bot.database import conn, cursor
from bot.tasks import reminder_loop, stat_leaderboard_loop
from bot.ui import BadgeSubmissionReviewView, JoinEventView, StatSubmissionReviewView

COGS = (
    "bot.cogs.events",
    "bot.cogs.players",
    "bot.cogs.teams",
    "bot.cogs.seasons",
    "bot.cogs.stats",
)


async def setup_hook():
    """Runs once, before the bot connects to Discord's gateway - the
    recommended place to load extensions (discord.py awaits this
    automatically as part of bot.run()/bot.start())."""
    for extension in COGS:
        await bot.load_extension(extension)


bot.setup_hook = setup_hook


@tree.error
async def on_app_command_error(interaction: Interaction, error):
    if isinstance(error, app_commands.errors.CheckFailure):
        await interaction.response.send_message(
            "You don't have permission to use this command.",
            ephemeral=True
        )
    else:
        print(f"Unhandled app command error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Something went wrong running that command.",
                ephemeral=True
            )


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync(guild=GUILD)
        print(f"{len(synced)} command{'s' if len(synced) != 1 else ''} synchronized.")
    except Exception as e:
        print(f"Could not synchronize commands: {e}")
    print(f"Bot is ready: {bot.user.name}")

    # Re-register the Join Event button so it keeps working after a restart.
    # on_ready can fire more than once (e.g. after a reconnect), so guard
    # against re-adding the same view repeatedly.
    if not getattr(bot, "_join_view_restored", False):
        active_event_id = bot_data.get("active_event_data", {}).get("active_event_id")
        if active_event_id:
            bot.add_view(JoinEventView(active_event_id))
            print(f"Restored Join Event view for event {active_event_id} ✅")
        bot._join_view_restored = True

    if not reminder_loop.is_running():
        reminder_loop.start()

    if not stat_leaderboard_loop.is_running():
        stat_leaderboard_loop.start()

    # Re-register a review view per still-pending stat/badge submission, same
    # reason as the Join Event view above - button callbacks don't survive a
    # restart unless the view (with its matching custom_id) is re-attached.
    if not getattr(bot, "_stat_review_views_restored", False):
        cursor.execute("SELECT id FROM stat_submissions WHERE status = 'pending'")
        pending_stat_ids = [row[0] for row in cursor.fetchall()]
        for submission_id in pending_stat_ids:
            bot.add_view(StatSubmissionReviewView(submission_id))

        cursor.execute("SELECT id FROM badge_submissions WHERE status = 'pending'")
        pending_badge_ids = [row[0] for row in cursor.fetchall()]
        for submission_id in pending_badge_ids:
            bot.add_view(BadgeSubmissionReviewView(submission_id))

        restored_count = len(pending_stat_ids) + len(pending_badge_ids)
        if restored_count:
            print(f"Restored {restored_count} pending submission review view(s) ✅")
        bot._stat_review_views_restored = True

    print("Fetching division members...")
    try:
        current_guild: discord.Guild = bot.get_guild(GUILD_ID)

        division_members = [
            member for member in current_guild.members
            if any(role.id in MEMBER_ROLES for role in member.roles)
        ]

        for member in division_members:
            cursor.execute("SELECT id FROM players WHERE discord_id = ?", (member.id,))
            player = cursor.fetchone()

            if not player:
                cursor.execute("INSERT INTO players (discord_id) VALUES (?)", (member.id,))
                conn.commit()

        print("Player database updated ✅")
    except Exception as e:
        print(f"Could not fetch division members: {e}")


@bot.event
async def on_disconnect():
    # NOTE: this fires on every websocket drop, including the brief blips
    # discord.py auto-reconnects from - it does NOT mean the bot is shutting
    # down. Do not close shared resources like the db connection here, or
    # every reconnect after the first disconnect leaves the app running
    # against a closed connection.
    print("The bot is now offline (will attempt to reconnect if this wasn't a shutdown).")


if __name__ == "__main__":
    try:
        bot.run(token, log_handler=handler, log_level=logging.DEBUG)
    finally:
        # This only runs once the process is actually exiting (bot.run returns
        # or raises), so it's the right place to close the db connection - unlike
        # on_disconnect, which can fire many times over the bot's lifetime.
        conn.close()
