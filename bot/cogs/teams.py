### TEAM COMMANDS ###

import csv
from datetime import datetime, timedelta
from io import StringIO
from typing import Optional

import discord
from discord import Embed, Interaction, Member, app_commands
from discord.ext import commands

from bot.shared import *  # noqa: F401,F403

class TeamsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="team_add",
        description="Create a new team.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    @is_staff()
    async def team_add(self, interaction: Interaction, name: str):
        try:
            cursor.execute("INSERT INTO teams (team_name) VALUES (?)", (name,))
            conn.commit()
            await interaction.response.send_message(f"Team **{name}** created ✅", ephemeral=True)
        except sqlite3.IntegrityError:
            await interaction.response.send_message(f"A team named **{name}** already exists.", ephemeral=True)


    @app_commands.command(
        name="team_remove",
        description="Delete a team. Members keep their individual results but lose their team tag.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    @is_staff()
    async def team_remove(self, interaction: Interaction, name: str):
        cursor.execute("SELECT id FROM teams WHERE team_name = ?", (name,))
        team = cursor.fetchone()
        if not team:
            await interaction.response.send_message(f"Team **{name}** not found.", ephemeral=True)
            return
        team_id = team[0]

        view = ConfirmView(interaction.user.id)
        await interaction.response.send_message(
            f"Delete team **{name}**? Members keep their individual results but lose their team tag in every event.",
            view=view,
            ephemeral=True
        )
        await view.wait()

        if not view.confirmed:
            return

        cursor.execute("UPDATE results SET team_id = NULL WHERE team_id = ?", (team_id,))
        cursor.execute("DELETE FROM teams WHERE id = ?", (team_id,))
        conn.commit()

        await interaction.followup.send(f"Team **{name}** deleted ✅", ephemeral=True)


    @app_commands.command(
        name="team_assign",
        description="Assign a player to a team for a specific event.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    @is_staff()
    async def team_assign(self, interaction: Interaction, user: Member, team: str, event_number: int = 0):
        event = await require_event(interaction, event_number)
        if event is None:
            return
        event_id, event_name = event[0], event[2]

        cursor.execute("SELECT id FROM teams WHERE team_name = ?", (team,))
        team_row = cursor.fetchone()
        if not team_row:
            await interaction.response.send_message(f"Team **{team}** not found - create it first with /team_add.", ephemeral=True)
            return
        team_id = team_row[0]

        cursor.execute("SELECT id FROM players WHERE discord_id = ?", (user.id,))
        player = cursor.fetchone()
        if not player:
            cursor.execute("INSERT INTO players (discord_id) VALUES (?)", (user.id,))
            conn.commit()
            cursor.execute("SELECT id FROM players WHERE discord_id = ?", (user.id,))
            player = cursor.fetchone()
        player_id = player[0]

        # Team assignment lives on the results row (per event), not the player - a
        # player can be on a different team, or no team, in each event. Assigning a
        # team also registers the player for the event (score 0) if they weren't
        # already, same as /player_register.
        cursor.execute("SELECT id FROM results WHERE player_id = ? AND event_id = ?", (player_id, event_id))
        result = cursor.fetchone()
        if result:
            cursor.execute("UPDATE results SET team_id = ? WHERE id = ?", (team_id, result[0]))
        else:
            cursor.execute(
                "INSERT INTO results (player_id, player_score, event_id, team_id) VALUES (?, 0, ?, ?)",
                (player_id, event_id, team_id)
            )
        conn.commit()

        await interaction.response.send_message(f"<@{user.id}> assigned to **{team}** for **{event_name}** ✅", ephemeral=True)


    @app_commands.command(
        name="team_unassign",
        description="Remove a player from their team for a specific event.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    @is_staff()
    async def team_unassign(self, interaction: Interaction, user: Member, event_number: int = 0):
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

        cursor.execute("UPDATE results SET team_id = NULL WHERE player_id = ? AND event_id = ?", (player_id, event_id))
        conn.commit()

        await interaction.response.send_message(f"<@{user.id}> removed from their team for this event ✅", ephemeral=True)


    @app_commands.command(
        name="team_list",
        description="List all teams and their member counts for an event.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    @is_staff()
    async def team_list(self, interaction: Interaction, event_number: int = 0):
        event = await require_event(interaction, event_number)
        if event is None:
            return
        event_id, event_name = event[0], event[2]

        cursor.execute("""
            SELECT teams.team_name, COUNT(results.id)
            FROM teams
            LEFT JOIN results ON results.team_id = teams.id AND results.event_id = ?
            GROUP BY teams.id
            ORDER BY teams.team_name
        """, (event_id,))
        teams = cursor.fetchall()

        if not teams:
            await interaction.response.send_message("No teams created yet.", ephemeral=True)
            return

        desc = "\n".join(f"**{name}** - {count} member{'s' if count != 1 else ''}" for name, count in teams)
        await interaction.response.send_message(embed=Embed(title=f"Teams - {event_name}", description=desc))


    @app_commands.command(
        name="team_leaderboard",
        description="Display team totals for an event. Players without a team still show up individually.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    @is_staff()
    async def team_leaderboard(self, interaction: Interaction, event_number: int = 0, display_in_channel: bool = False):
        event = await require_event(interaction, event_number)
        if event is None:
            return
        event_id, event_name = event[0], event[2]

        cursor.execute("""
            SELECT results.team_id, teams.team_name, players.discord_id, results.player_score
            FROM results
            JOIN players ON results.player_id = players.id
            LEFT JOIN teams ON results.team_id = teams.id
            WHERE results.event_id = ?
        """, (event_id,))
        rows = cursor.fetchall()

        # Players on a team get pooled into a team total; players with no team (we
        # don't force everyone onto one) keep their own individual score and rank
        # alongside the teams rather than getting dropped from the board.
        team_totals: dict[int, list] = {}
        unassigned: list[tuple[int, int]] = []

        for team_id, team_name, discord_id, score in rows:
            if team_id is not None:
                if team_id not in team_totals:
                    team_totals[team_id] = [team_name, 0]
                team_totals[team_id][1] += score
            else:
                unassigned.append((discord_id, score))

        combined = [(name, total) for name, total in team_totals.values()]
        combined += [(f"<@{discord_id}> *(no team)*", score) for discord_id, score in unassigned]
        combined.sort(key=lambda entry: entry[1], reverse=True)

        desc = "".join(team_placement_line(i, label, score) for i, (label, score) in enumerate(combined))
        embed = Embed(
            title=f"🏆 {event_name} Team Leaderboard",
            description="No results found." if desc == "" else desc
        )
        await interaction.response.send_message(embed=embed)

        if display_in_channel:
            cursor.execute(
                "SELECT team_leaderboard_message_id, team_leaderboard_channel_id FROM events WHERE id = ?",
                (event_id,)
            )
            existing_message_id, existing_channel_id = cursor.fetchone() or (None, None)

            posted_message = await post_or_update_leaderboard_message(
                LEADERBOARD_CHANNEL_ID, embed, existing_message_id, existing_channel_id
            )

            cursor.execute(
                "UPDATE events SET team_leaderboard_message_id = ?, team_leaderboard_channel_id = ? WHERE id = ?",
                (posted_message.id, posted_message.channel.id, event_id)
            )
            conn.commit()


    @app_commands.command(
        name="team_bulk_add",
        description="Bulk create teams from a CSV/text file (one team name per line).",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    async def team_bulk_add(self, interaction: Interaction, file: discord.Attachment):
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
        if rows and rows[0][0].strip().lower() in ("team_name", "team", "name"):
            rows = rows[1:]  # optional header row

        created = 0
        errors: list[str] = []

        for line_number, row in enumerate(rows, start=1):
            name = row[0].strip()
            if not name:
                continue
            try:
                cursor.execute("INSERT INTO teams (team_name) VALUES (?)", (name,))
                created += 1
            except sqlite3.IntegrityError:
                errors.append(f"Row {line_number}: team **{name}** already exists.")

        conn.commit()

        summary = f"Bulk team creation: {created} created"
        summary += " ✅" if not errors else f", {len(errors)} skipped:\n" + "\n".join(errors[:15])
        if len(errors) > 15:
            summary += f"\n...and {len(errors) - 15} more."

        await interaction.followup.send(summary[:2000], ephemeral=True)


    @app_commands.command(
        name="team_bulk_remove",
        description="Bulk delete teams from a CSV/text file (one team name per line). Members keep their results.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    async def team_bulk_remove(self, interaction: Interaction, file: discord.Attachment):
        try:
            text = (await file.read()).decode("utf-8-sig")
        except Exception as e:
            await interaction.response.send_message(f"Could not read attachment: {e}", ephemeral=True)
            return

        rows = [row for row in csv.reader(StringIO(text)) if row and any(cell.strip() for cell in row)]
        if rows and rows[0][0].strip().lower() in ("team_name", "team", "name"):
            rows = rows[1:]  # optional header row

        names = [row[0].strip() for row in rows if row and row[0].strip()]
        if not names:
            await interaction.response.send_message("No team names found in file.", ephemeral=True)
            return

        view = ConfirmView(interaction.user.id)
        await interaction.response.send_message(
            f"Delete {len(names)} team(s)? Members keep their individual results but lose the team tag in every event.",
            view=view,
            ephemeral=True
        )
        await view.wait()

        if not view.confirmed:
            return

        deleted = 0
        not_found: list[str] = []

        for name in names:
            cursor.execute("SELECT id FROM teams WHERE team_name = ?", (name,))
            team = cursor.fetchone()
            if not team:
                not_found.append(name)
                continue
            cursor.execute("UPDATE results SET team_id = NULL WHERE team_id = ?", (team[0],))
            cursor.execute("DELETE FROM teams WHERE id = ?", (team[0],))
            deleted += 1

        conn.commit()

        summary = f"Deleted {deleted} team(s)."
        if not_found:
            shown = ", ".join(not_found[:15])
            summary += f" Not found: {shown}" + (f" (+{len(not_found) - 15} more)" if len(not_found) > 15 else "")

        await interaction.followup.send(summary[:2000], ephemeral=True)


    @app_commands.command(
        name="team_bulk_update",
        description="Bulk rename teams from a CSV/text file (old_name,new_name).",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    async def team_bulk_update(self, interaction: Interaction, file: discord.Attachment):
        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            text = (await file.read()).decode("utf-8-sig")
        except Exception as e:
            await interaction.followup.send(f"Could not read attachment: {e}", ephemeral=True)
            return

        rows = [row for row in csv.reader(StringIO(text)) if row and any(cell.strip() for cell in row)]
        if rows and rows[0][0].strip().lower() in ("old_name", "team_name", "team"):
            rows = rows[1:]  # optional header row

        updated = 0
        errors: list[str] = []

        for line_number, row in enumerate(rows, start=1):
            if len(row) < 2:
                errors.append(f"Row {line_number}: expected old_name,new_name.")
                continue

            old_name, new_name = row[0].strip(), row[1].strip()
            if not old_name or not new_name:
                errors.append(f"Row {line_number}: both old_name and new_name are required.")
                continue

            cursor.execute("SELECT id FROM teams WHERE team_name = ?", (old_name,))
            team = cursor.fetchone()
            if not team:
                errors.append(f"Row {line_number}: team **{old_name}** not found.")
                continue

            try:
                cursor.execute("UPDATE teams SET team_name = ? WHERE id = ?", (new_name, team[0]))
                updated += 1
            except sqlite3.IntegrityError:
                errors.append(f"Row {line_number}: a team named **{new_name}** already exists.")

        conn.commit()

        summary = f"Bulk rename: {updated} updated"
        summary += " ✅" if not errors else f", {len(errors)} error(s):\n" + "\n".join(errors[:15])
        if len(errors) > 15:
            summary += f"\n...and {len(errors) - 15} more."

        await interaction.followup.send(summary[:2000], ephemeral=True)


    @app_commands.command(
        name="team_bulk_assign",
        description="Bulk assign players to teams for an event from a CSV file (discord_id,team_name).",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    async def team_bulk_assign(self, interaction: Interaction, file: discord.Attachment, event_number: int = 0):
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

        assigned = 0
        errors: list[str] = []
        team_cache: dict[str, Optional[int]] = {}  # avoids re-querying the same team name every row

        for line_number, row in enumerate(rows, start=1):
            if len(row) < 2:
                errors.append(f"Row {line_number}: expected discord_id,team_name.")
                continue

            try:
                discord_id = int(row[0].strip())
            except ValueError:
                errors.append(f"Row {line_number}: discord_id must be a number.")
                continue

            team_name = row[1].strip()
            if not team_name:
                errors.append(f"Row {line_number}: team_name is required.")
                continue

            if team_name not in team_cache:
                cursor.execute("SELECT id FROM teams WHERE team_name = ?", (team_name,))
                team_row = cursor.fetchone()
                team_cache[team_name] = team_row[0] if team_row else None

            team_id = team_cache[team_name]
            if team_id is None:
                errors.append(f"Row {line_number}: team **{team_name}** not found.")
                continue

            cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
            player = cursor.fetchone()
            if not player:
                cursor.execute("INSERT INTO players (discord_id) VALUES (?)", (discord_id,))
                cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
                player = cursor.fetchone()
            player_id = player[0]

            cursor.execute("SELECT id FROM results WHERE player_id = ? AND event_id = ?", (player_id, event_id))
            result = cursor.fetchone()
            if result:
                cursor.execute("UPDATE results SET team_id = ? WHERE id = ?", (team_id, result[0]))
            else:
                cursor.execute(
                    "INSERT INTO results (player_id, player_score, event_id, team_id) VALUES (?, 0, ?, ?)",
                    (player_id, event_id, team_id)
                )
            assigned += 1

        conn.commit()

        summary = f"Bulk team assignment for **{event_name}**: {assigned} assigned"
        summary += " ✅" if not errors else f", {len(errors)} error(s):\n" + "\n".join(errors[:15])
        if len(errors) > 15:
            summary += f"\n...and {len(errors) - 15} more."

        await interaction.followup.send(summary[:2000], ephemeral=True)


    @app_commands.command(
        name="team_bulk_unassign",
        description="Bulk remove players from their teams for an event from a CSV file (one discord_id per line).",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    async def team_bulk_unassign(self, interaction: Interaction, file: discord.Attachment, event_number: int = 0):
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

        await interaction.response.defer(thinking=True, ephemeral=True)

        unassigned = 0
        for discord_id in discord_ids:
            cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
            player = cursor.fetchone()
            if not player:
                continue
            cursor.execute(
                "UPDATE results SET team_id = NULL WHERE player_id = ? AND event_id = ?",
                (player[0], event_id)
            )
            unassigned += cursor.rowcount

        conn.commit()

        summary = f"Removed team assignment for {unassigned} player(s) in **{event_name}**"
        if skipped:
            summary += f" ({skipped} row(s) couldn't be parsed and were skipped)"
        summary += " ✅"

        await interaction.followup.send(summary[:2000], ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(TeamsCog(bot))
