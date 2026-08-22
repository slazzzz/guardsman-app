### BACKGROUND TASKS ###

import sqlite3
from datetime import datetime, timedelta

import discord
from discord.ext import tasks

from bot.client import bot
from bot.database import conn, cursor

REMINDER_WINDOW = timedelta(hours=1)


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
