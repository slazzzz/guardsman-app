### EVENT COMMANDS ###

import asyncio
import csv
from datetime import datetime, timedelta
from io import StringIO
from typing import Optional

import discord
from discord import Embed, Interaction, Member, app_commands
from discord.ext import commands

from bot.shared import *  # noqa: F401,F403

class EventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="event_add",
        description="Register a new event.",
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.choices(
        mode=[app_commands.Choice(name=mode_name, value=mode_name.lower()) for mode_name in EVENT_MODES],
        type=[app_commands.Choice(name=type_name, value=type_name.lower()) for type_name in EVENT_TYPES]
    )
    @is_staff()
    async def event_add(self, interaction: Interaction, name: str, mode: str, type: str, prize_pool: int = 0, ignore_weekly_condition: bool = False):
        if prize_pool < 0:
            await interaction.response.send_message("prize_pool must be greater than 0.", ephemeral=True)
            return

        now = datetime.now()
        event_week = now.strftime("%W")

        if not ignore_weekly_condition:
            cursor.execute("SELECT event_date FROM events ORDER BY id DESC LIMIT 1")
            last_event = cursor.fetchone()

            if last_event:
                last_event_week = last_event[0].split("-")[0]

                if last_event_week == event_week:
                    await interaction.response.send_message("Event already exists for this week.", ephemeral=True)
                    return

        event_date = now.strftime(EVENT_DATE_FORMAT)

        try:
            cursor.execute(
                "INSERT INTO events (event_date, event_name, event_mode, event_type, event_prize, season_id) VALUES (?, ?, ?, ?, ?, ?)",
                (event_date, name, mode, type, prize_pool, get_active_season_id())
            )
            conn.commit()

            await interaction.response.send_message("Event registered ✅", ephemeral=True)
        except Exception as e:
            print(f"Could not initialize event in database: {e}")

            await interaction.response.send_message(f"Error occured: {e}", ephemeral=True)


    @app_commands.command(
        name="event_remove",
        description="Remove recent event.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    async def event_remove(self, interaction: Interaction, event_number: int = 0):
        event = await require_event(interaction, event_number)
        if event is None:
            return

        event_id, event_name = event[0], event[2]

        view = ConfirmView(interaction.user.id)
        await interaction.response.send_message(
            f"Are you sure you want to remove **{event_name}**? This also deletes all of its results and cannot be undone.",
            view=view,
            ephemeral=True
        )
        await view.wait()

        if not view.confirmed:
            return

        try:
            cursor.execute("DELETE FROM results WHERE event_id = ?", (event_id,))
            cursor.execute("DELETE FROM events WHERE id = ?", (event_id,))
            conn.commit()

            await interaction.followup.send("Event removed ✅", ephemeral=True)
        except Exception as e:
            print(f"Could not remove event from database: {e}")

            await interaction.followup.send(f"Error occured: {e}", ephemeral=True)


    @app_commands.command(
        name="event_list",
        description="Get a list of all hosted events.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_allowed()
    async def event_list(self, interaction: Interaction):
        cursor.execute("SELECT * FROM events")
        events = cursor.fetchall()

        if len(events) == 0:
            await interaction.response.send_message("No events registered.", ephemeral=True)
            return

        lines = []

        for idx, event in enumerate(events):
            # (id, event_date, event_name, event_mode, event_type, event_prize)

            event_id = event[0]
            raw_date = event[1]
            event_name = event[2]
            event_mode = event[3]
            event_type = event[4]
            event_prize = event[5]

            cursor.execute("SELECT id FROM results WHERE event_id = ?", (event_id,))
            event_player_count = len(cursor.fetchall())

            dt_object = datetime.strptime(raw_date, EVENT_DATE_FORMAT)
            new_date = dt_object.strftime(f"%B %d, {DB_YEAR}")

            participants_suffix = "s" if event_player_count != 1 else ""
            lines.append(
                f"**{idx + 1}.** {event_name} - {capitalize(event_mode)} {capitalize(event_type)} - "
                f"{new_date} - {event_player_count} Participant{participants_suffix} - {event_prize} Robux"
            )

        pages = [lines[i:i + EVENTS_PER_PAGE] for i in range(0, len(lines), EVENTS_PER_PAGE)]
        embeds = [
            Embed(title=f"Event list - {DB_YEAR}", description="\n".join(page))
            .set_footer(text=f"Page {i + 1}/{len(pages)}")
            for i, page in enumerate(pages)
        ]

        if len(embeds) == 1:
            await interaction.response.send_message(embed=embeds[0])
        else:
            view = PaginatorView(embeds, interaction.user.id)
            await interaction.response.send_message(embed=embeds[0], view=view)


    @app_commands.command(
        name="results_export",
        description="Export event results to a CSV file for archiving.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    async def results_export(self, interaction: Interaction, first_event_number: int = 0, final_event_number: int = 0):
        if first_event_number < 0 or final_event_number < 0:
            await interaction.response.send_message("Event numbers must be greater than 0.", ephemeral=True)
            return

        if first_event_number == 0 and final_event_number == 0:
            # No range given - export the whole season.
            cursor.execute("SELECT id FROM events ORDER BY id")
            event_ids = [row[0] for row in cursor.fetchall()]
        else:
            first = first_event_number or 1
            final = final_event_number or first

            if final < first:
                await interaction.response.send_message("final_event_number must be greater than first_event_number.", ephemeral=True)
                return

            cursor.execute(
                "SELECT id FROM events ORDER BY id LIMIT ? OFFSET ?",
                (final - first + 1, first - 1)
            )
            event_ids = [row[0] for row in cursor.fetchall()]

        if not event_ids:
            await interaction.response.send_message("No events found.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "event_id", "event_date", "event_name", "event_mode", "event_type", "event_prize",
            "discord_id", "roblox_id", "player_score", "placement"
        ])

        for event_id in event_ids:
            cursor.execute(
                "SELECT event_date, event_name, event_mode, event_type, event_prize FROM events WHERE id = ?",
                (event_id,)
            )
            event = cursor.fetchone()
            if not event:
                continue
            event_date, event_name, event_mode, event_type, event_prize = event

            cursor.execute("""
                SELECT players.discord_id, players.roblox_id, results.player_score
                FROM results
                JOIN players ON results.player_id = players.id
                WHERE results.event_id = ?
                ORDER BY results.player_score DESC
            """, (event_id,))

            for placement, (discord_id, roblox_id, player_score) in enumerate(cursor.fetchall(), start=1):
                writer.writerow([
                    event_id, event_date, event_name, event_mode, event_type, event_prize,
                    discord_id, roblox_id, player_score, placement
                ])

        csv_file = BytesIO(buffer.getvalue().encode("utf-8"))
        filename = f"results_export_{DB_YEAR}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

        await interaction.followup.send(file=discord.File(csv_file, filename=filename), ephemeral=True)


    @app_commands.command(
        name="event_leaderboard",
        description="Display a leaderboard compiling the result of an event.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_staff()
    async def event_leaderboard(self, interaction: Interaction, event_number: int = 0, display_in_channel: bool = False, generate_image: bool = False):
        event = await require_event(interaction, event_number)
        if event is None:
            return

        event_id, event_name = event[0], event[2]

        try:
            cursor.execute("""
                SELECT players.discord_id, results.player_score, players.roblox_id
                FROM results
                JOIN players ON results.player_id = players.id
                WHERE results.event_id = ?
                GROUP BY results.player_id
                ORDER BY results.player_score DESC
            """, (event_id,))
            event_results = cursor.fetchall()
        except Exception as e:
            print(f"Could not fetch event leaderboard: {e}")
            await interaction.response.send_message(f"Error occured: {e}", ephemeral=True)
            return

        desc = "".join(placement_line(i, row[0], row[1]) for i, row in enumerate(event_results))

        embed = Embed(title=f"🏆 {event_name} Leaderboard", description="No results found." if desc == "" else desc)
        await interaction.response.send_message(embed=embed)

        if display_in_channel:
            cursor.execute(
                "SELECT leaderboard_message_id, leaderboard_channel_id FROM events WHERE id = ?",
                (event_id,)
            )
            existing_message_id, existing_channel_id = cursor.fetchone() or (None, None)

            posted_message = await post_or_update_leaderboard_message(
                LEADERBOARD_CHANNEL_ID, embed, existing_message_id, existing_channel_id
            )

            cursor.execute(
                "UPDATE events SET leaderboard_message_id = ?, leaderboard_channel_id = ? WHERE id = ?",
                (posted_message.id, posted_message.channel.id, event_id)
            )
            conn.commit()

        if generate_image and event_results:
            names = await resolve_display_names(interaction, [row[0] for row in event_results])
            # leaderboard_image() makes blocking Roblox API/image requests. Run it in a
            # worker thread so it can't freeze the bot's single event loop (heartbeats,
            # other users' commands, etc.) while it waits on the network.
            image = await asyncio.to_thread(
                leaderboard_image, event_results, names, f"{event_name} Leaderboard"
            )
            await interaction.followup.send(file=image)


    @app_commands.command(
        name="event_update",
        description="Update an event.",
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.choices(
        field=[app_commands.Choice(name=field_name, value=field_name) for field_name in ["event_name", "event_mode", "event_type", "event_prize"]]
    )
    @is_staff()
    async def event_update(self, interaction: Interaction, field: str, value: str, event_number: int = 0):
        event = await require_event(interaction, event_number)
        if event is None:
            return

        event_id = event[0]

        try:
            cursor.execute(
                f"UPDATE events SET {field} = ? WHERE id = ?",
                (int(value) if field == "event_prize" else value if field == "event_name" else value.lower(), event_id)
            )
            conn.commit()

            await interaction.response.send_message(f"{field} updated ✅", ephemeral=True)
        except Exception as e:
            print(f"Could not update event field value: {e}")

            await interaction.response.send_message(f"Error occured: {e}", ephemeral=True)


    @app_commands.command(
        name="event_form",
        description="Open a form of an event in the forms channel.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_staff()
    async def event_form(self, interaction: Interaction, event_number: int = 0):
        event = await require_event(interaction, event_number)
        if event is None:
            return

        event_id, event_name = event[0], event[2]

        form_data.update({"active_event_id": event_id})
        bot_data.update({"active_event_data": form_data})
        save_json("bot_data.json", bot_data)

        view = JoinEventView(event_id)

        active_event_channel = bot.get_channel(FORM_CHANNEL_ID)
        if active_event_channel is None:
            await interaction.response.send_message("Form channel not found - check FORM_CHANNEL_ID.", ephemeral=True)
            return

        await active_event_channel.send(f"Click below to join **{event_name}**!", view=view)

        await interaction.response.send_message(f"Form created for {event_name} ✅", ephemeral=True)


    @app_commands.command(
        name="event_set_start_time",
        description="Set when an event starts so registered players get a reminder DM ~1 hour before.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_staff()
    async def event_set_start_time(self, interaction: Interaction, day: int, month: int, hour: int, minute: int, event_number: int = 0):
        event = await require_event(interaction, event_number)
        if event is None:
            return

        event_id, event_name = event[0], event[2]

        try:
            # Scoped to DB_YEAR since each season gets its own database file.
            start_time = datetime(DB_YEAR, month, day, hour, minute)
        except ValueError as e:
            await interaction.response.send_message(f"Invalid date/time: {e}", ephemeral=True)
            return

        cursor.execute(
            "UPDATE events SET event_start_time = ?, reminder_sent = 0 WHERE id = ?",
            (start_time.strftime("%Y-%m-%d %H:%M:%S"), event_id)
        )
        conn.commit()

        await interaction.response.send_message(
            f"Start time for **{event_name}** set to {start_time.strftime('%B %d, %H:%M')} - "
            f"registered players will get a reminder DM about an hour before ✅"
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(EventsCog(bot))
