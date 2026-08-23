### PLAYER COMMANDS ###

import csv
from datetime import datetime, timedelta
from io import StringIO
from typing import Optional

import discord
from discord import Embed, Interaction, Member, app_commands
from discord.ext import commands

from bot.shared import *  # noqa: F401,F403

class PlayersCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="player_register",
        description="Register a player to the active event",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin_or_staff()
    async def player_register(self, interaction: Interaction, user: Member, roblox_id: int = 0, event_number: int = 0):
        event = await require_event(interaction, event_number)
        if event is None:
            return

        event_id = event[0]
        user_id = user.id

        cursor.execute("SELECT id FROM players WHERE discord_id = ?", (user_id,))
        player = cursor.fetchone()

        if not player:
            try:
                cursor.execute(
                    "INSERT INTO players (discord_id, roblox_id) VALUES (?, ?)",
                    (user_id, roblox_id)
                )
                conn.commit()

                cursor.execute("SELECT id FROM players WHERE discord_id = ?", (user_id,))
                player = cursor.fetchone()
            except Exception as e:
                print(f"Could not initialize player in database: {e}")

                await interaction.response.send_message(f"Error occured: {e}", ephemeral=True)
                return

        player_id = player[0]

        cursor.execute("SELECT id FROM results WHERE player_id = ? AND event_id = ?", (player_id, event_id))
        existing_results = cursor.fetchone()

        if existing_results:
            await interaction.response.send_message("Player already registered.", ephemeral=True)
            return

        try:
            cursor.execute(
                "INSERT INTO results (player_id, player_score, event_id) VALUES (?, ?, ?)",
                (player_id, 0, event_id)
            )
            conn.commit()

            await interaction.response.send_message(f"<@{user_id}> registered ✅", ephemeral=True)
        except Exception as e:
            print(f"Could not initialize player results in database: {e}")

            await interaction.response.send_message(f"Error occured: {e}", ephemeral=True)


    @app_commands.command(
        name="player_results_update",
        description="Update a player's score in an event",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin_or_staff()
    async def player_results_update(self, interaction: Interaction, user: Member, event_number: int = 0, player_score: int = 0):
        if player_score < 0:
            await interaction.response.send_message("player_score must be greater than 0.", ephemeral=True)
            return

        event = await require_event(interaction, event_number)
        if event is None:
            return
        event_id = event[0]

        player = await require_player(interaction, user)
        if player is None:
            return
        player_id = player[0]

        if await require_results(interaction, player_id, event_id) is None:
            return

        try:
            cursor.execute(
                "UPDATE results SET player_score = ? WHERE player_id = ? AND event_id = ?",
                (player_score, player_id, event_id)
            )
            conn.commit()

            await interaction.response.send_message(f"Results updated for <@{user.id}> ✅", ephemeral=True)
        except Exception as e:
            print(f"Could not update player results in database: {e}")

            await interaction.response.send_message(f"Error occured: {e}", ephemeral=True)


    @app_commands.command(
        name="player_results_delete",
        description="Delete a player's score in an event",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    async def player_results_delete(self, interaction: Interaction, user: Member, event_number: int = 0):
        event = await require_event(interaction, event_number)
        if event is None:
            return
        event_id = event[0]

        player = await require_player(interaction, user)
        if player is None:
            return
        player_id = player[0]

        if await require_results(interaction, player_id, event_id) is None:
            return

        view = ConfirmView(interaction.user.id)
        await interaction.response.send_message(
            f"Delete <@{user.id}>'s results for this event? This cannot be undone.",
            view=view,
            ephemeral=True
        )
        await view.wait()

        if not view.confirmed:
            return

        try:
            cursor.execute(
                "DELETE FROM results WHERE (player_id, event_id) = (?, ?)",
                (player_id, event_id)
            )
            conn.commit()

            await interaction.followup.send(f"Results deleted for <@{user.id}> ✅", ephemeral=True)
        except Exception as e:
            print(f"Could not delete player results from database: {e}")

            await interaction.followup.send(f"Error occured: {e}", ephemeral=True)


    @app_commands.command(
        name="player_info",
        description="Get a player's event information",
    )
    @app_commands.guilds(GUILD_ID)
    @is_allowed()
    async def player_info(self, interaction: Interaction, user: Member):
        player = await require_player(interaction, user)
        if player is None:
            return
        player_id = player[0]

        cursor.execute("SELECT * FROM results WHERE player_id = ?", (player_id,))
        player_results = cursor.fetchall()

        fetched_user = await bot.fetch_user(user.id)

        embed = Embed(title=f"🏅 {fetched_user.name} Global Results")
        embed.set_thumbnail(url=fetched_user.display_avatar.url)
        embed.set_footer(text=f"User ID: {user.id}")

        if len(player_results) == 0:
            embed.description = "No event results found."
        else:
            for result in player_results:
                player_score = result[2]
                event_id = result[3]

                cursor.execute("SELECT * FROM events WHERE id = ?", (event_id,))
                event = cursor.fetchone()

                if not event:
                    continue

                cursor.execute(
                    "SELECT player_id FROM results WHERE event_id = ? ORDER BY player_score DESC",
                    (event_id,)
                )
                rankings = [row[0] for row in cursor.fetchall()]

                player_ranking = rankings.index(player_id) + 1
                event_name = event[2]

                embed.add_field(name=event_name, value=f"Score: {player_score} - Placement: {player_ranking}")

        await interaction.response.send_message(embed=embed)


    @app_commands.command(
        name="roblox_link",
        description="Link your own Roblox account so your avatar shows up on /profile",
    )
    @app_commands.guilds(GUILD_ID)
    @is_allowed()
    async def roblox_link(self, interaction: Interaction, roblox_id_or_username: str):
        roblox_id, error = resolve_roblox_ref(roblox_id_or_username)
        if error:
            await interaction.response.send_message(f"Couldn't link that - {error}.", ephemeral=True)
            return
        if roblox_id is None:
            await interaction.response.send_message("Give a Roblox username or numeric ID.", ephemeral=True)
            return

        cursor.execute("SELECT id FROM players WHERE discord_id = ?", (interaction.user.id,))
        player = cursor.fetchone()
        if player:
            cursor.execute("UPDATE players SET roblox_id = ? WHERE discord_id = ?", (roblox_id, interaction.user.id))
        else:
            cursor.execute("INSERT INTO players (discord_id, roblox_id) VALUES (?, ?)", (interaction.user.id, roblox_id))
        conn.commit()

        await interaction.response.send_message(
            "Linked ✅ - your Roblox account is set, your avatar will show up on `/profile` now.",
            ephemeral=True
        )


    @app_commands.command(
        name="player_roblox_id_update",
        description="Update a player's Roblox ID.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin_or_staff()
    async def player_roblox_id_update(self, interaction: Interaction, user: Member, roblox_id: int):
        if roblox_id <= 0:
            await interaction.response.send_message("roblox_id must be greater than 0.", ephemeral=True)
            return

        player = await require_player(interaction, user)
        if player is None:
            return

        try:
            cursor.execute(
                "UPDATE players SET roblox_id = ? WHERE discord_id = ?",
                (roblox_id, user.id)
            )
            conn.commit()

            await interaction.response.send_message(f"Roblox ID updated for <@{user.id}> ✅", ephemeral=True)
        except Exception as e:
            print(f"Could not update player results in database: {e}")

            await interaction.response.send_message(f"Error occured: {e}", ephemeral=True)


    @app_commands.command(
        name="player_bulk_register",
        description="Bulk-create players from a file (discord_id[,roblox_id_or_username]).",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    async def player_bulk_register(self, interaction: Interaction, file: discord.Attachment):
        if not file.filename.lower().endswith((".csv", ".txt")):
            await interaction.response.send_message("Please attach a .csv or .txt file.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            text = (await file.read()).decode("utf-8-sig")
        except Exception as e:
            await interaction.followup.send(f"Could not read attachment: {e}", ephemeral=True)
            return

        rows = [row for row in csv.reader(StringIO(text)) if row and any(cell.strip() for cell in row)]
        if rows and rows[0][0].strip().lower() in ("discord_id", "discord id", "id"):
            rows = rows[1:]  # optional header row

        if not rows:
            await interaction.followup.send("File is empty.", ephemeral=True)
            return

        created = skipped = 0
        errors: list[str] = []

        for line_number, row in enumerate(rows, start=1):
            if not row or not row[0].strip():
                errors.append(f"Row {line_number}: missing discord_id.")
                continue

            try:
                discord_id = int(row[0].strip())
            except ValueError:
                errors.append(f"Row {line_number}: discord_id must be a number.")
                continue

            roblox_id = None
            if len(row) >= 2 and row[1].strip():
                roblox_id, resolve_error = resolve_roblox_ref(row[1])
                if resolve_error:
                    errors.append(f"Row {line_number}: {resolve_error}.")
                    continue

            cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
            if cursor.fetchone():
                skipped += 1
                continue

            try:
                cursor.execute(
                    "INSERT INTO players (discord_id, roblox_id) VALUES (?, ?)",
                    (discord_id, roblox_id or 0)
                )
                created += 1
            except Exception as e:
                errors.append(f"Row {line_number}: {e}")

        conn.commit()

        summary = f"Bulk register: {created} created, {skipped} already registered"
        summary += " ✅" if not errors else f", {len(errors)} error(s):\n" + "\n".join(errors[:15])
        if len(errors) > 15:
            summary += f"\n...and {len(errors) - 15} more."

        await interaction.followup.send(summary[:2000], ephemeral=True)


    @app_commands.command(
        name="player_bulk_update",
        description="Bulk-update players' Roblox IDs from a file (discord_id,roblox_id_or_username).",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    async def player_bulk_update(self, interaction: Interaction, file: discord.Attachment):
        if not file.filename.lower().endswith((".csv", ".txt")):
            await interaction.response.send_message("Please attach a .csv or .txt file.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            text = (await file.read()).decode("utf-8-sig")
        except Exception as e:
            await interaction.followup.send(f"Could not read attachment: {e}", ephemeral=True)
            return

        rows = [row for row in csv.reader(StringIO(text)) if row and any(cell.strip() for cell in row)]
        if rows and rows[0][0].strip().lower() in ("discord_id", "discord id", "id"):
            rows = rows[1:]  # optional header row

        if not rows:
            await interaction.followup.send("File is empty.", ephemeral=True)
            return

        updated = not_found = 0
        errors: list[str] = []

        for line_number, row in enumerate(rows, start=1):
            if len(row) < 2 or not row[0].strip() or not row[1].strip():
                errors.append(f"Row {line_number}: expected discord_id,roblox_id_or_username.")
                continue

            try:
                discord_id = int(row[0].strip())
            except ValueError:
                errors.append(f"Row {line_number}: discord_id must be a number.")
                continue

            roblox_id, resolve_error = resolve_roblox_ref(row[1])
            if resolve_error:
                errors.append(f"Row {line_number}: {resolve_error}.")
                continue
            if roblox_id is None:
                errors.append(f"Row {line_number}: roblox_id_or_username is required.")
                continue

            cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
            if not cursor.fetchone():
                not_found += 1
                continue

            cursor.execute("UPDATE players SET roblox_id = ? WHERE discord_id = ?", (roblox_id, discord_id))
            updated += 1

        conn.commit()

        summary = f"Bulk update: {updated} updated, {not_found} not registered"
        summary += " ✅" if not errors else f", {len(errors)} error(s):\n" + "\n".join(errors[:15])
        if len(errors) > 15:
            summary += f"\n...and {len(errors) - 15} more."

        await interaction.followup.send(summary[:2000], ephemeral=True)


    @app_commands.command(
        name="player_bulk_remove",
        description="Bulk-remove players and ALL their results from a file (one discord_id per line).",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    async def player_bulk_remove(self, interaction: Interaction, file: discord.Attachment):
        try:
            text = (await file.read()).decode("utf-8-sig")
        except Exception as e:
            await interaction.response.send_message(f"Could not read attachment: {e}", ephemeral=True)
            return

        rows = [row for row in csv.reader(StringIO(text)) if row and any(cell.strip() for cell in row)]
        if rows and rows[0][0].strip().lower() in ("discord_id", "discord id", "id"):
            rows = rows[1:]  # optional header row

        discord_ids: list[int] = []
        skipped = 0
        for row in rows:
            try:
                discord_ids.append(int(row[0].strip()))
            except (ValueError, IndexError):
                skipped += 1

        if not discord_ids:
            await interaction.response.send_message("No valid discord_id values found in file.", ephemeral=True)
            return

        view = ConfirmView(interaction.user.id)
        warning = f"Remove {len(discord_ids)} player(s) and ALL of their event results? This cannot be undone."
        if skipped:
            warning += f"\n({skipped} row(s) couldn't be parsed and will be skipped.)"
        await interaction.response.send_message(warning, view=view, ephemeral=True)
        await view.wait()

        if not view.confirmed:
            return

        removed = not_found = 0
        for discord_id in discord_ids:
            cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
            player = cursor.fetchone()
            if not player:
                not_found += 1
                continue

            player_id = player[0]
            cursor.execute("DELETE FROM results WHERE player_id = ?", (player_id,))
            cursor.execute("DELETE FROM players WHERE id = ?", (player_id,))
            removed += 1

        conn.commit()

        await interaction.followup.send(f"Removed {removed} player(s) ({not_found} not found) ✅", ephemeral=True)


    @app_commands.command(
        name="player_results_bulk_update",
        description="Bulk add/update player results for an event from a CSV file (discord_id,player_score[,roblox_id]).",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    async def player_results_bulk_update(self, interaction: Interaction, file: discord.Attachment, event_number: int = 0):
        event = await require_event(interaction, event_number)
        if event is None:
            return
        event_id, event_name = event[0], event[2]

        if not file.filename.lower().endswith((".csv", ".txt")):
            await interaction.response.send_message("Please attach a .csv or .txt file.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            text = (await file.read()).decode("utf-8-sig")
        except Exception as e:
            await interaction.followup.send(f"Could not read attachment: {e}", ephemeral=True)
            return

        rows = [row for row in csv.reader(StringIO(text)) if row and any(cell.strip() for cell in row)]
        if rows and rows[0][0].strip().lower() in ("discord_id", "discord id", "id"):
            rows = rows[1:]  # optional header row

        if not rows:
            await interaction.followup.send("File is empty.", ephemeral=True)
            return

        created = updated = 0
        errors: list[str] = []

        for line_number, row in enumerate(rows, start=1):
            if len(row) < 2:
                errors.append(f"Row {line_number}: expected at least discord_id,player_score.")
                continue

            try:
                discord_id = int(row[0].strip())
                player_score = int(row[1].strip())
            except ValueError:
                errors.append(f"Row {line_number}: discord_id and player_score must be numbers.")
                continue

            if player_score < 0:
                errors.append(f"Row {line_number}: player_score must be >= 0.")
                continue

            roblox_id = None
            if len(row) >= 3 and row[2].strip():
                try:
                    roblox_id = int(row[2].strip())
                except ValueError:
                    errors.append(f"Row {line_number}: roblox_id must be a number if provided.")
                    continue

            cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
            player = cursor.fetchone()

            if not player:
                cursor.execute(
                    "INSERT INTO players (discord_id, roblox_id) VALUES (?, ?)",
                    (discord_id, roblox_id or 0)
                )
                cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
                player = cursor.fetchone()
            elif roblox_id:
                cursor.execute("UPDATE players SET roblox_id = ? WHERE discord_id = ?", (roblox_id, discord_id))

            player_id = player[0]

            cursor.execute("SELECT id FROM results WHERE player_id = ? AND event_id = ?", (player_id, event_id))
            if cursor.fetchone():
                cursor.execute(
                    "UPDATE results SET player_score = ? WHERE player_id = ? AND event_id = ?",
                    (player_score, player_id, event_id)
                )
                updated += 1
            else:
                cursor.execute(
                    "INSERT INTO results (player_id, player_score, event_id) VALUES (?, ?, ?)",
                    (player_id, player_score, event_id)
                )
                created += 1

        conn.commit()

        summary = f"Bulk update for **{event_name}**: {created} created, {updated} updated"
        summary += " ✅" if not errors else f", {len(errors)} error(s):\n" + "\n".join(errors[:15])
        if len(errors) > 15:
            summary += f"\n...and {len(errors) - 15} more."

        await interaction.followup.send(summary[:2000], ephemeral=True)


    @app_commands.command(
        name="player_results_bulk_delete",
        description="Bulk delete player results for an event from a CSV file (one discord_id per line).",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    async def player_results_bulk_delete(self, interaction: Interaction, file: discord.Attachment, event_number: int = 0):
        event = await require_event(interaction, event_number)
        if event is None:
            return
        event_id, event_name = event[0], event[2]

        try:
            text = (await file.read()).decode("utf-8-sig")
        except Exception as e:
            await interaction.response.send_message(f"Could not read attachment: {e}", ephemeral=True)
            return

        rows = [row for row in csv.reader(StringIO(text)) if row and any(cell.strip() for cell in row)]
        if rows and rows[0][0].strip().lower() in ("discord_id", "discord id", "id"):
            rows = rows[1:]  # optional header row

        discord_ids: list[int] = []
        skipped = 0
        for row in rows:
            try:
                discord_ids.append(int(row[0].strip()))
            except (ValueError, IndexError):
                skipped += 1

        if not discord_ids:
            await interaction.response.send_message("No valid discord_id values found in file.", ephemeral=True)
            return

        view = ConfirmView(interaction.user.id)
        warning = f"Delete results for {len(discord_ids)} player(s) in **{event_name}**? This cannot be undone."
        if skipped:
            warning += f"\n({skipped} row(s) couldn't be parsed and will be skipped.)"
        await interaction.response.send_message(warning, view=view, ephemeral=True)
        await view.wait()

        if not view.confirmed:
            return

        deleted = 0
        for discord_id in discord_ids:
            cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
            player = cursor.fetchone()
            if not player:
                continue
            cursor.execute("DELETE FROM results WHERE player_id = ? AND event_id = ?", (player[0], event_id))
            deleted += cursor.rowcount

        conn.commit()

        await interaction.followup.send(f"Deleted results for {deleted} player(s) in **{event_name}** ✅", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(PlayersCog(bot))