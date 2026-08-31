### BACKGROUND TASKS ###

import sqlite3
from datetime import datetime, timedelta

import discord
from discord.ext import tasks

from bot.client import bot
from bot.config import DRILL_STALE_CANCEL_HOURS, DRILL_STALE_WARNING_HOURS
from bot.database import conn, cursor
from bot.drills import drill_as_dict, refresh_drill_message, sync_drill_vc_permissions
from bot.leaderboard import build_stat_leaderboard_embed, build_stat_leaderboard_image

REMINDER_WINDOW = timedelta(hours=1)
STAT_LEADERBOARD_TICK_MINUTES = 5
DRILL_EXPIRY_TICK_MINUTES = 15
DRILL_TEMPBAN_EXPIRY_TICK_MINUTES = 1


@tasks.loop(minutes=1)
async def reminder_loop():
    now = datetime.now()
    window_end = now + REMINDER_WINDOW

    try:
        cursor.execute("""
            SELECT id, event_name, event_start_time FROM events
            WHERE event_start_time IS NOT NULL AND reminder_sent = 0
        """)
        candidates = cursor.fetchall()
    except sqlite3.Error as e:
        print(f"reminder_loop: could not query events, skipping this tick: {e}")
        return

    for event_id, event_name, start_time_str in candidates:
        try:
            start_time = datetime.strptime(start_time_str, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue

        if not (now <= start_time <= window_end):
            continue

        try:
            cursor.execute("""
                SELECT players.discord_id FROM results
                JOIN players ON results.player_id = players.id
                WHERE results.event_id = ?
            """, (event_id,))
            discord_ids = [row[0] for row in cursor.fetchall()]

            for discord_id in discord_ids:
                try:
                    user = await bot.fetch_user(discord_id)
                    await user.send(f"⏰ **{event_name}** starts in about an hour - get ready!")
                except (discord.Forbidden, discord.NotFound) as e:
                    print(f"Could not DM reminder to {discord_id}: {e}")

            cursor.execute("UPDATE events SET reminder_sent = 1 WHERE id = ?", (event_id,))
            conn.commit()

            print(f"Sent reminders for event {event_id} ({event_name}) to {len(discord_ids)} player(s).")
        except sqlite3.Error as e:
            # Don't let one bad event stop the rest of the events in this tick,
            # or crash the loop entirely (which would silently kill all future
            # reminders for the process's lifetime).
            print(f"reminder_loop: failed processing event {event_id} ({event_name}): {e}")
            continue


@reminder_loop.error
async def reminder_loop_error(error: Exception):
    # Safety net: if something outside the try/except above still slips
    # through (e.g. a Discord API outage), log it loudly instead of letting
    # discord.py silently stop the loop, and restart it so reminders keep
    # working rather than going dark for the rest of the process's life.
    print(f"reminder_loop crashed unexpectedly, restarting it: {error}")
    if not reminder_loop.is_running():
        reminder_loop.restart()


@reminder_loop.before_loop
async def before_reminder_loop():
    await bot.wait_until_ready()


@tasks.loop(minutes=STAT_LEADERBOARD_TICK_MINUTES)
async def stat_leaderboard_loop():
    """Ticks every STAT_LEADERBOARD_TICK_MINUTES and re-renders any enabled
    stat leaderboard (see stat_leaderboards.enabled, toggled via
    /leaderboard_stats_disable and /leaderboard_stats_enable) whose own
    update_interval_minutes has elapsed since it was last posted. The tick
    interval is deliberately shorter than most boards' update_interval so each
    board's actual cadence stays close to what staff configured via
    /leaderboard_stats_setup, without a separate asyncio task per board."""
    now = datetime.now()

    try:
        cursor.execute("""
            SELECT stat_type, channel_id, message_id, update_interval_minutes, last_updated_at, use_image
            FROM stat_leaderboards
            WHERE enabled = 1
        """)
        boards = cursor.fetchall()
    except sqlite3.Error as e:
        print(f"stat_leaderboard_loop: could not query stat_leaderboards, skipping this tick: {e}")
        return

    for stat_type, channel_id, message_id, interval_minutes, last_updated_at, use_image in boards:
        if last_updated_at:
            try:
                last_updated = datetime.strptime(last_updated_at, "%Y-%m-%d %H:%M:%S")
                if now - last_updated < timedelta(minutes=interval_minutes):
                    continue
            except (TypeError, ValueError):
                pass  # malformed timestamp - fall through and just refresh it

        try:
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)

            if use_image:
                file = await build_stat_leaderboard_image(channel.guild, stat_type)
                if file is None:
                    continue

                message = None
                if message_id:
                    try:
                        message = await channel.fetch_message(message_id)
                    except (discord.NotFound, discord.Forbidden):
                        message = None

                if message:
                    await message.edit(attachments=[file])
                else:
                    message = await channel.send(file=file)
            else:
                embed = await build_stat_leaderboard_embed(channel.guild, stat_type)
                if embed is None:
                    continue

                message = None
                if message_id:
                    try:
                        message = await channel.fetch_message(message_id)
                    except (discord.NotFound, discord.Forbidden):
                        message = None

                if message:
                    await message.edit(embed=embed)
                else:
                    message = await channel.send(embed=embed)

            cursor.execute(
                "UPDATE stat_leaderboards SET message_id = ?, last_updated_at = CURRENT_TIMESTAMP WHERE stat_type = ?",
                (message.id, stat_type)
            )
            conn.commit()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            # Channel deleted, perms revoked, etc. - skip this board for now
            # rather than letting one bad board stop the rest from updating.
            print(f"stat_leaderboard_loop: could not update '{stat_type}' board: {e}")
            continue


@stat_leaderboard_loop.error
async def stat_leaderboard_loop_error(error: Exception):
    print(f"stat_leaderboard_loop crashed unexpectedly, restarting it: {error}")
    if not stat_leaderboard_loop.is_running():
        stat_leaderboard_loop.restart()


@stat_leaderboard_loop.before_loop
async def before_stat_leaderboard_loop():
    await bot.wait_until_ready()


@tasks.loop(minutes=DRILL_EXPIRY_TICK_MINUTES)
async def drill_expiry_loop():
    """Handles the "host creates a drill, then never runs /drill_start"
    case: a drill that's been sitting in recruiting/ready with no
    started_at for DRILL_STALE_WARNING_HOURS gets its host a one-time nudge
    DM (and a warning line on the posted embed, via build_drill_embed in
    bot/drills.py); if it's STILL not started by DRILL_STALE_CANCEL_HOURS,
    it's auto-cancelled the same way /drill_cancel would. Either threshold
    can be set to 0 in bot_data.json (drill_data.stale_warning_hours /
    stale_cancel_hours) to disable that stage - see bot/config.py.

    Deliberately scoped to drills that never started at all - an abandoned
    in_progress drill (started but never /drill_end'd) is a different
    problem with a different fix (staff should reach for /drill_force_status
    or /drill_end directly), not something this loop touches.
    """
    if not DRILL_STALE_WARNING_HOURS and not DRILL_STALE_CANCEL_HOURS:
        return

    now = datetime.now()

    try:
        cursor.execute("SELECT * FROM drills WHERE status IN ('recruiting', 'ready') AND started_at IS NULL")
        candidates = cursor.fetchall()
    except sqlite3.Error as e:
        print(f"drill_expiry_loop: could not query drills, skipping this tick: {e}")
        return

    for drill in candidates:
        d = drill_as_dict(drill)

        try:
            created_at = datetime.strptime(d["created_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue

        age_hours = (now - created_at).total_seconds() / 3600

        try:
            if DRILL_STALE_CANCEL_HOURS and age_hours >= DRILL_STALE_CANCEL_HOURS:
                cursor.execute(
                    "UPDATE drills SET status = 'cancelled', ended_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (d["id"],)
                )
                conn.commit()
                await refresh_drill_message(d["id"])

                try:
                    user = await bot.fetch_user(d["host_discord_id"])
                    await user.send(
                        f"🛡️ Your drill **{d['drill_name']}** (#{d['id']}) was auto-cancelled - it sat in "
                        f"recruiting for {DRILL_STALE_CANCEL_HOURS:.0f}+ hours without ever being started."
                    )
                except (discord.Forbidden, discord.NotFound):
                    pass

                print(f"drill_expiry_loop: auto-cancelled drill {d['id']} (inactive {age_hours:.1f}h)")

            elif DRILL_STALE_WARNING_HOURS and age_hours >= DRILL_STALE_WARNING_HOURS and not d["stale_warned"]:
                cursor.execute("UPDATE drills SET stale_warned = 1 WHERE id = ?", (d["id"],))
                conn.commit()
                await refresh_drill_message(d["id"])

                cancel_note = (
                    f" It'll auto-cancel at {DRILL_STALE_CANCEL_HOURS:.0f}h total if nothing changes."
                    if DRILL_STALE_CANCEL_HOURS else ""
                )
                try:
                    user = await bot.fetch_user(d["host_discord_id"])
                    await user.send(
                        f"🛡️ Your drill **{d['drill_name']}** (#{d['id']}) hasn't been started yet - "
                        f"run /drill_start when you're ready, or /drill_cancel if it's not happening.{cancel_note}"
                    )
                except (discord.Forbidden, discord.NotFound):
                    pass

                print(f"drill_expiry_loop: warned host of stale drill {d['id']} (inactive {age_hours:.1f}h)")
        except sqlite3.Error as e:
            # Don't let one bad drill stop the rest of this tick, or crash
            # the loop entirely - same reasoning as reminder_loop above.
            print(f"drill_expiry_loop: failed processing drill {d['id']}: {e}")
            continue


@drill_expiry_loop.error
async def drill_expiry_loop_error(error: Exception):
    print(f"drill_expiry_loop crashed unexpectedly, restarting it: {error}")
    if not drill_expiry_loop.is_running():
        drill_expiry_loop.restart()


@drill_expiry_loop.before_loop
async def before_drill_expiry_loop():
    await bot.wait_until_ready()


@tasks.loop(minutes=DRILL_TEMPBAN_EXPIRY_TICK_MINUTES)
async def drill_tempban_expiry_loop():
    """Sweeps drill_banned_users for /drill_tempban rows whose banned_until
    has passed and lifts them - permanent bans (banned_until IS NULL) are
    never touched here. Every read of drill_banned_users elsewhere
    (is_user_blocked_from_drill/sync_drill_vc_permissions in bot/drills.py,
    /drill_ban_list in bot/cogs/drills.py) already filters expired rows out
    on its own, so this loop isn't what makes an expired tempban stop
    working - it's just what actually deletes the row and re-syncs any
    live drill VC so the member can reconnect without staff having to run
    /drill_unban by hand.

    banned_until is compared directly against SQLite's CURRENT_TIMESTAMP
    (both UTC) rather than parsed into a Python datetime and compared
    against datetime.now() - deliberately avoiding the local-vs-UTC
    mismatch that pattern would introduce here.
    """
    try:
        cursor.execute(
            "SELECT discord_id FROM drill_banned_users WHERE banned_until IS NOT NULL AND banned_until <= CURRENT_TIMESTAMP"
        )
        expired_discord_ids = [row[0] for row in cursor.fetchall()]
    except sqlite3.Error as e:
        print(f"drill_tempban_expiry_loop: could not query drill_banned_users, skipping this tick: {e}")
        return

    if not expired_discord_ids:
        return

    try:
        cursor.execute(
            "DELETE FROM drill_banned_users WHERE banned_until IS NOT NULL AND banned_until <= CURRENT_TIMESTAMP"
        )
        conn.commit()
    except sqlite3.Error as e:
        print(f"drill_tempban_expiry_loop: could not delete expired bans, skipping this tick: {e}")
        return

    # Mirrors /drill_unban: re-sync every in-progress drill's VC so the
    # newly-unbanned member(s) can actually reconnect, not just so the DB
    # row is gone. Doesn't attempt to disconnect/reconnect anyone itself -
    # lifting a ban only needs to stop denying future connects, unlike
    # /drill_ban and /drill_tempban, which do actively disconnect on the
    # way IN.
    for guild in bot.guilds:
        cursor.execute("SELECT id FROM drills WHERE status = 'in_progress' AND vc_channel_id IS NOT NULL")
        for (drill_id,) in cursor.fetchall():
            await sync_drill_vc_permissions(guild, drill_id)

    print(f"drill_tempban_expiry_loop: lifted {len(expired_discord_ids)} expired drill tempban(s): {expired_discord_ids}")


@drill_tempban_expiry_loop.error
async def drill_tempban_expiry_loop_error(error: Exception):
    print(f"drill_tempban_expiry_loop crashed unexpectedly, restarting it: {error}")
    if not drill_tempban_expiry_loop.is_running():
        drill_tempban_expiry_loop.restart()


@drill_tempban_expiry_loop.before_loop
async def before_drill_tempban_expiry_loop():
    await bot.wait_until_ready()