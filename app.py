### GUARDSMAN APP - ENTRYPOINT ###
# This file just wires everything together and starts the bot. Feature code
# lives in bot/ - see bot/cogs/ for the actual slash commands, grouped by
# domain (events, players, teams, seasons).

import logging

import discord
from discord import Interaction, app_commands

from bot.client import bot, handler, token, tree
from bot.config import COGS, GUILD, GUILD_ID, MEMBER_ROLES
from bot.database import conn, cursor
from bot.tasks import drill_expiry_loop, drill_tempban_expiry_loop, reminder_loop, stat_leaderboard_loop
from bot.views import restore_all_views


async def setup_hook():
    """Runs once, before the bot connects to Discord's gateway - the
    recommended place to load extensions (discord.py awaits this
    automatically as part of bot.run()/bot.start())."""
    for extension in COGS:
        await bot.load_extension(extension)


bot.setup_hook = setup_hook


@tree.error
async def on_app_command_error(interaction: Interaction, error):
    if isinstance(error, app_commands.errors.CommandOnCooldown):
        # Checked first since CommandOnCooldown is itself a CheckFailure
        # subclass - without this branch it'd fall into the generic
        # "no permission" message below, which is the wrong reason.
        retry_after = round(error.retry_after)
        minutes, seconds = divmod(retry_after, 60)
        wait_str = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
        await interaction.response.send_message(
            f"You're using that too often - try again in {wait_str}.",
            ephemeral=True
        )
    elif isinstance(error, app_commands.errors.CheckFailure):
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

    # Re-register every persistent view (Join Event, drill rosters, stat/
    # badge submission reviews) so their buttons keep working after a
    # restart - see bot/views.py for what each one does. on_ready can fire
    # more than once (e.g. after a reconnect), so guard against re-adding
    # the same views repeatedly. To re-run this on demand without a full
    # restart (e.g. after a hand-edited DB row), staff/admins can use
    # /admin_restore_views instead - see bot/cogs/admin.py.
    if not getattr(bot, "_views_restored", False):
        restored = restore_all_views()
        if restored["join_event"]:
            print("Restored Join Event view ✅")
        if restored["submission_reviews"]:
            print(f"Restored {restored['submission_reviews']} pending submission review view(s) ✅")
        if restored["drills"]:
            print(f"Restored {restored['drills']} active drill roster view(s) ✅")
        bot._views_restored = True

    if not reminder_loop.is_running():
        reminder_loop.start()

    if not stat_leaderboard_loop.is_running():
        stat_leaderboard_loop.start()

    if not drill_expiry_loop.is_running():
        drill_expiry_loop.start()

    if not drill_tempban_expiry_loop.is_running():
        drill_tempban_expiry_loop.start()

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