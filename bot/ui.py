### UI COMPONENTS ###

import sqlite3
from typing import Optional

import discord
from discord import Embed, Interaction

from bot.config import ADMIN_USERS, STAFF_ROLES
from bot.database import conn, cursor
from bot.roblox import get_roblox_id_from_username


def _is_staff_member(interaction: Interaction) -> bool:
    """Same rule as bot.decorators.is_allowed(), duplicated as a plain function
    since views need to check this inside a button callback, not as a slash
    command decorator."""
    if interaction.user.id in ADMIN_USERS:
        return True
    if any(role.id in STAFF_ROLES for role in interaction.user.roles):
        return True
    return interaction.user.guild_permissions.manage_guild


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


class StatSubmissionReviewView(discord.ui.View):
    """Posted alongside a stat submission in the review channel. Approving copies
    the submitted value into player_stats (making it show up on /profile and any
    leaderboard for that stat_type); rejecting just closes out the queue row.

    timeout=None + a custom_id keyed on the submission's row id is what lets this
    survive a bot restart - app.py re-attaches one of these per still-pending
    submission on_ready, the same way JoinEventView does for its event id.
    """

    def __init__(self, submission_id: int):
        super().__init__(timeout=None)
        self.submission_id = submission_id
        self.approve.custom_id = f"stat_submission_approve_{submission_id}"
        self.reject.custom_id = f"stat_submission_reject_{submission_id}"

    async def _load_submission(self):
        cursor.execute(
            "SELECT player_id, stat_type, value, status FROM stat_submissions WHERE id = ?",
            (self.submission_id,)
        )
        return cursor.fetchone()

    async def _finalize(self, interaction: Interaction, *, approved: bool):
        if not _is_staff_member(interaction):
            await interaction.response.send_message("You don't have permission to review submissions.", ephemeral=True)
            return

        submission = await self._load_submission()
        if not submission:
            await interaction.response.send_message("This submission no longer exists.", ephemeral=True)
            return

        player_id, stat_type, value, status = submission
        if status != "pending":
            await interaction.response.send_message(f"This submission was already {status}.", ephemeral=True)
            return

        new_status = "approved" if approved else "rejected"
        cursor.execute(
            "UPDATE stat_submissions SET status = ?, reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_status, interaction.user.id, self.submission_id)
        )

        if approved:
            cursor.execute("""
                INSERT INTO player_stats (player_id, stat_type, value, source, verified_by, updated_at)
                VALUES (?, ?, ?, 'manual', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(player_id, stat_type) DO UPDATE SET
                    value = excluded.value, source = 'manual',
                    verified_by = excluded.verified_by, updated_at = CURRENT_TIMESTAMP
            """, (player_id, stat_type, value, interaction.user.id))

        conn.commit()

        for item in self.children:
            item.disabled = True

        verdict_line = f"\n\n{'✅ Approved' if approved else '❌ Rejected'} by {interaction.user.mention}"
        original_embed = interaction.message.embeds[0]
        original_embed.description = (original_embed.description or "") + verdict_line
        await interaction.response.edit_message(embed=original_embed, view=self)
        self.stop()

        # Best-effort DM to the submitter - a failed DM (blocked/left server)
        # shouldn't stop the review itself from going through.
        cursor.execute("SELECT discord_id FROM players WHERE id = ?", (player_id,))
        player_row = cursor.fetchone()
        if player_row:
            try:
                submitter = await interaction.client.fetch_user(player_row[0])
                if approved:
                    await submitter.send(f"✅ Your **{stat_type}** submission ({value:,}) was approved and is now on your profile.")
                else:
                    await submitter.send(f"❌ Your **{stat_type}** submission ({value:,}) was rejected.")
            except (discord.Forbidden, discord.NotFound):
                pass

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green)
    async def approve(self, interaction: Interaction, button: discord.ui.Button):
        await self._finalize(interaction, approved=True)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.red)
    async def reject(self, interaction: Interaction, button: discord.ui.Button):
        await self._finalize(interaction, approved=False)


class BadgeSubmissionReviewView(discord.ui.View):
    """Same shape as StatSubmissionReviewView, for badges roblox_owns_badge()
    couldn't confirm automatically (private inventory, or the scan didn't reach
    it) - staff approve/reject from the submitted screenshot(s) instead."""

    def __init__(self, submission_id: int):
        super().__init__(timeout=None)
        self.submission_id = submission_id
        self.approve.custom_id = f"badge_submission_approve_{submission_id}"
        self.reject.custom_id = f"badge_submission_reject_{submission_id}"

    async def _load_submission(self):
        cursor.execute(
            "SELECT player_id, badge_name, status FROM badge_submissions WHERE id = ?",
            (self.submission_id,)
        )
        return cursor.fetchone()

    async def _finalize(self, interaction: Interaction, *, approved: bool):
        if not _is_staff_member(interaction):
            await interaction.response.send_message("You don't have permission to review submissions.", ephemeral=True)
            return

        submission = await self._load_submission()
        if not submission:
            await interaction.response.send_message("This submission no longer exists.", ephemeral=True)
            return

        player_id, badge_name, status = submission
        if status != "pending":
            await interaction.response.send_message(f"This submission was already {status}.", ephemeral=True)
            return

        new_status = "approved" if approved else "rejected"
        cursor.execute(
            "UPDATE badge_submissions SET status = ?, reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_status, interaction.user.id, self.submission_id)
        )

        if approved:
            cursor.execute("""
                INSERT INTO player_badges (player_id, badge_name, awarded_by, source)
                VALUES (?, ?, ?, 'submitted')
                ON CONFLICT(player_id, badge_name) DO NOTHING
            """, (player_id, badge_name, interaction.user.id))

        conn.commit()

        for item in self.children:
            item.disabled = True

        verdict_line = f"\n\n{'✅ Approved' if approved else '❌ Rejected'} by {interaction.user.mention}"
        original_embed = interaction.message.embeds[0]
        original_embed.description = (original_embed.description or "") + verdict_line
        await interaction.response.edit_message(embed=original_embed, view=self)
        self.stop()

        cursor.execute("SELECT discord_id FROM players WHERE id = ?", (player_id,))
        player_row = cursor.fetchone()
        if player_row:
            try:
                submitter = await interaction.client.fetch_user(player_row[0])
                if approved:
                    await submitter.send(f"✅ Your **{badge_name}** badge was approved and is now on your profile.")
                else:
                    await submitter.send(f"❌ Your **{badge_name}** badge submission was rejected.")
            except (discord.Forbidden, discord.NotFound):
                pass

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green)
    async def approve(self, interaction: Interaction, button: discord.ui.Button):
        await self._finalize(interaction, approved=True)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.red)
    async def reject(self, interaction: Interaction, button: discord.ui.Button):
        await self._finalize(interaction, approved=False)
