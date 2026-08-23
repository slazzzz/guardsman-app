### SEASON COMMANDS ###

import csv
from datetime import datetime, timedelta
from io import StringIO
from typing import Optional

import discord
from discord import Embed, Interaction, Member, app_commands
from discord.ext import commands

from bot.shared import *  # noqa: F401,F403

class SeasonsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="season_current",
        description="Show info about the current season.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_allowed()
    async def season_current(self, interaction: Interaction):
        season_id = get_active_season_id()
        cursor.execute("SELECT season_number, started_at FROM seasons WHERE id = ?", (season_id,))
        season_number, started_at = cursor.fetchone()

        cursor.execute("SELECT COUNT(*) FROM events WHERE season_id = ?", (season_id,))
        event_count = cursor.fetchone()[0]

        embed = Embed(
            title=f"Season {season_number} (active)",
            description=f"Started: {started_at}\nEvents so far: {event_count}"
        )
        await interaction.response.send_message(embed=embed)


    @app_commands.command(
        name="season_list",
        description="List every season, past and current.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_allowed()
    async def season_list(self, interaction: Interaction):
        cursor.execute("SELECT season_number, started_at, ended_at FROM seasons ORDER BY id")
        seasons = cursor.fetchall()

        if not seasons:
            await interaction.response.send_message("No seasons yet.", ephemeral=True)
            return

        desc = "\n".join(
            f"**Season {number}** - {started} → {ended or 'ongoing'}"
            for number, started, ended in seasons
        )
        await interaction.response.send_message(embed=Embed(title="Seasons", description=desc))


    @app_commands.command(
        name="season_leaderboard",
        description="Cumulative leaderboard for a season (default: the current one).",
    )
    @app_commands.guilds(GUILD_ID)
    @is_staff()
    async def season_leaderboard(self, interaction: Interaction, season_number: int = 0, top: int = 10):
        if top <= 0:
            await interaction.response.send_message("top must be greater than 0.", ephemeral=True)
            return

        if season_number == 0:
            season_id = get_active_season_id()
        else:
            cursor.execute("SELECT id FROM seasons WHERE season_number = ?", (season_number,))
            row = cursor.fetchone()
            if not row:
                await interaction.response.send_message(f"Season {season_number} not found.", ephemeral=True)
                return
            season_id = row[0]

        cursor.execute("SELECT season_number, ended_at FROM seasons WHERE id = ?", (season_id,))
        resolved_number, ended_at = cursor.fetchone()

        cursor.execute("""
            SELECT players.discord_id, SUM(results.player_score) as total_score
            FROM results
            JOIN players ON results.player_id = players.id
            JOIN events ON results.event_id = events.id
            WHERE events.season_id = ?
            GROUP BY results.player_id
            ORDER BY total_score DESC
            LIMIT ?
        """, (season_id, top))
        leaderboard = cursor.fetchall()

        desc = "".join(placement_line(i, discord_id, score) for i, (discord_id, score) in enumerate(leaderboard))
        status = "ongoing" if ended_at is None else f"ended {ended_at}"
        embed = Embed(
            title=f"🏆 Season {resolved_number} Leaderboard - TOP {top} ({status})",
            description="No results found." if desc == "" else desc
        )
        await interaction.response.send_message(embed=embed)


    @app_commands.command(
        name="season_reset",
        description="Close the current season and start a new one. Nothing is deleted - past events keep their data.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin()
    async def season_reset(self, interaction: Interaction, top: int = 10):
        if top <= 0:
            await interaction.response.send_message("top must be greater than 0.", ephemeral=True)
            return

        season_id = get_active_season_id()
        cursor.execute("SELECT season_number, started_at FROM seasons WHERE id = ?", (season_id,))
        season_number, started_at = cursor.fetchone()

        cursor.execute("""
            SELECT players.discord_id, SUM(results.player_score) as total_score
            FROM results
            JOIN players ON results.player_id = players.id
            JOIN events ON results.event_id = events.id
            WHERE events.season_id = ?
            GROUP BY results.player_id
            ORDER BY total_score DESC
            LIMIT ?
        """, (season_id, top))
        leaderboard = cursor.fetchall()
        desc = "".join(placement_line(i, discord_id, score) for i, (discord_id, score) in enumerate(leaderboard))

        preview_embed = Embed(
            title=f"⚠️ End Season {season_number}?",
            description=(
                f"This closes Season {season_number} (started {started_at}) and opens Season {season_number + 1}. "
                f"No data is deleted - past events and results stay exactly as they are.\n\n"
                f"**Top {top} for this season** (hand out rewards from this manually):\n\n"
                + ("No results found." if desc == "" else desc)
            )
        )

        view = ConfirmView(interaction.user.id)
        await interaction.response.send_message(embed=preview_embed, view=view, ephemeral=True)
        await view.wait()

        if not view.confirmed:
            return

        cursor.execute("UPDATE seasons SET ended_at = ? WHERE id = ?", (datetime.now().isoformat(), season_id))
        cursor.execute("INSERT INTO seasons (season_number) VALUES (?)", (season_number + 1,))
        conn.commit()

        final_embed = Embed(
            title=f"🏁 Season {season_number} Final Standings - TOP {top}",
            description="No results found." if desc == "" else desc
        )
        await interaction.followup.send(
            content=f"Season {season_number} closed. Season {season_number + 1} is now active.",
            embed=final_embed
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(SeasonsCog(bot))
