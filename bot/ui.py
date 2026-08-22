### UI COMPONENTS ###

import sqlite3
from typing import Optional

import discord
from discord import Embed, Interaction

from bot.database import conn, cursor
from bot.roblox import get_roblox_id_from_username


class JoinEventModal(discord.ui.Modal, title="Join Event"):
    roblox_username = discord.ui.TextInput(
        label="Roblox Username",
        placeholder="Enter your Roblox username",
        required=True
    )

    async def on_submit(self, interaction: Interaction):
        user_id = interaction.user.id
        roblox_username = self.roblox_username.value
        roblox_id = get_roblox_id_from_username(roblox_username) or 0

        cursor.execute(
            "INSERT OR IGNORE INTO players (discord_id, roblox_id) VALUES (?, ?)",
            (user_id, roblox_id)
        )

        cursor.execute(
            "UPDATE players SET roblox_id = ? WHERE discord_id = ?",
            (roblox_id, user_id)
        )

        cursor.execute(
            "SELECT id FROM players WHERE discord_id = ?",
            (user_id,)
        )
        player_id = cursor.fetchone()[0]

        try:
            cursor.execute(
                "INSERT INTO results (player_id, player_score, event_id) VALUES (?, 0, ?)",
                (player_id, self.event_id)
            )
            conn.commit()

            await interaction.response.send_message(
                "Registered successfully ✅",
                ephemeral=True
            )
        except sqlite3.IntegrityError:
            await interaction.response.send_message(
                "You are already registered.",
                ephemeral=True
            )


class JoinEventView(discord.ui.View):
    def __init__(self, event_id: int):
        super().__init__(timeout=None)
        self.event_id = event_id
        # custom_id must be stable and unique per event so bot.add_view() can
        # re-attach this view to its button after a restart. Without this,
        # the button survives on screen but has nothing listening for it.
        self.join.custom_id = f"join_event_{event_id}"

    @discord.ui.button(label="Join Event", style=discord.ButtonStyle.green)
    async def join(self, interaction: Interaction, button: discord.ui.Button):
        modal = JoinEventModal()
        modal.event_id = self.event_id
        await interaction.response.send_modal(modal)


class ConfirmView(discord.ui.View):
    """Yes/No confirmation gate for destructive admin actions. Only the person who
    triggered the original command can respond to it."""

    def __init__(self, author_id: int, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.confirmed: Optional[bool] = None

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This confirmation isn't for you.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        self.confirmed = False
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        await interaction.response.edit_message(content="Confirmed - processing...", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction, button: discord.ui.Button):
        self.confirmed = False
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)


class PaginatorView(discord.ui.View):
    """Simple prev/next pager for embeds that would otherwise blow past Discord's
    4096-character embed description limit once enough events pile up."""

    def __init__(self, embeds: list[Embed], author_id: int, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.author_id = author_id
        self.index = 0
        self._update_buttons()

    def _update_buttons(self):
        self.previous_page.disabled = self.index == 0
        self.next_page.disabled = self.index == len(self.embeds) - 1

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: Interaction, button: discord.ui.Button):
        self.index -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.index], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: Interaction, button: discord.ui.Button):
        self.index += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.index], view=self)
