### UI COMPONENTS ###

import sqlite3
from typing import Optional

import discord
from discord import Embed, Interaction

from bot.config import ADMIN_USERS, STAFF_ROLES
from bot.database import conn, cursor
from bot.drills import (
    get_active_participant_count,
    get_active_participant_discord_ids,
    is_user_blocked_from_drill,
    refresh_drill_message,
)
from bot.leaderboard import resolve_display_names
from bot.lookups import upsert_player_roblox_id
from bot.roblox import get_roblox_id_from_username
from bot.verification import VERIFICATION_TTL, clear_verification, start_verification, verify_pending


def _is_staff_member(interaction: Interaction) -> bool:
    """Same rule as bot.decorators.is_allowed(), duplicated as a plain function
    since views need to check this inside a button callback, not as a slash
    command decorator."""
    if interaction.user.id in ADMIN_USERS:
        return True
    if any(role.id in STAFF_ROLES for role in interaction.user.roles):
        return True
    return interaction.user.guild_permissions.manage_guild


async def _register_for_event(interaction: Interaction, roblox_id: int, event_id: int):
    """Shared tail end of joining an event: upsert the player row with
    roblox_id, then insert their event registration. Used both when a Roblox
    username was just verified (JoinEventModal, via RobloxVerificationView)
    and when the member already had a verified account linked (JoinEventView
    .join skips the modal/verification entirely in that case)."""
    user_id = interaction.user.id
    upsert_player_roblox_id(user_id, roblox_id)

    cursor.execute("SELECT id FROM players WHERE discord_id = ?", (user_id,))
    player_id = cursor.fetchone()[0]

    try:
        cursor.execute(
            "INSERT INTO results (player_id, player_score, event_id) VALUES (?, 0, ?)",
            (player_id, event_id)
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


class RobloxVerificationView(discord.ui.View):
    """Shown after start_verification() generates a code - the member pastes
    it into their Roblox profile's About section, then presses Confirm to
    have it checked against the live Roblox API. Reused by both the event
    join flow below and /roblox_link (players.py) since both need the same
    "prove you own this account" step before a link is accepted.

    on_verified is an async callback(interaction, roblox_id) that performs
    whatever the caller actually wanted once ownership is confirmed (e.g.
    upserting the players table, or also registering for an event) - this
    view only handles the proof step, not what happens after.
    """

    def __init__(self, discord_id: int, on_verified):
        super().__init__(timeout=VERIFICATION_TTL.total_seconds())
        self.discord_id = discord_id
        self._on_verified = on_verified

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.discord_id:
            await interaction.response.send_message("This verification isn't for you.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        clear_verification(self.discord_id)

    @discord.ui.button(label="I've added the code - Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: Interaction, button: discord.ui.Button):
        success, roblox_id, error = verify_pending(self.discord_id)
        if not success:
            await interaction.response.send_message(error, ephemeral=True)
            return

        for item in self.children:
            item.disabled = True
        self.stop()

        await self._on_verified(interaction, roblox_id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction, button: discord.ui.Button):
        clear_verification(self.discord_id)
        self.stop()
        await interaction.response.edit_message(content="Verification cancelled.", view=None)


def verification_prompt(code: str) -> str:
    """Shared copy for the "paste this code into your Roblox profile" message,
    so /roblox_link and the event join form read identically."""
    minutes = int(VERIFICATION_TTL.total_seconds() // 60)
    return (
        f"To prove this is your account, open your Roblox profile, edit your **About** "
        f"section, and paste this code anywhere in it:\n\n`{code}`\n\n"
        f"Then press **Confirm** below. This expires in {minutes} minutes - you can remove "
        f"the code from your profile afterward."
    )


class JoinEventModal(discord.ui.Modal, title="Join Event"):
    roblox_username = discord.ui.TextInput(
        label="Roblox Username",
        placeholder="Enter your Roblox username",
        required=True
    )

    async def on_submit(self, interaction: Interaction):
        roblox_username = self.roblox_username.value
        roblox_id = get_roblox_id_from_username(roblox_username)

        if roblox_id is None:
            await interaction.response.send_message(
                f"Couldn't find a Roblox account named '{roblox_username}' - double check the spelling and try again.",
                ephemeral=True
            )
            return

        event_id = self.event_id
        code = start_verification(interaction.user.id, roblox_id)

        async def _on_verified(confirm_interaction: Interaction, verified_roblox_id: int):
            await _register_for_event(confirm_interaction, verified_roblox_id, event_id)

        await interaction.response.send_message(
            verification_prompt(code),
            view=RobloxVerificationView(interaction.user.id, _on_verified),
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
        # If this member already has a verified Roblox account linked
        # (self-service /roblox_link, or a staff /player_roblox_id_update),
        # skip the modal and verification entirely and register them
        # directly with the account already on file - no reason to make them
        # re-prove ownership every time they join an event.
        cursor.execute("SELECT roblox_id FROM players WHERE discord_id = ?", (interaction.user.id,))
        row = cursor.fetchone()
        if row and row[0]:
            await _register_for_event(interaction, row[0], self.event_id)
            return

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


class ReviewReasonModal(discord.ui.Modal):
    """Optional-reason popup shown after a staff member presses Approve/Reject
    on a stat or badge submission. Whatever's typed here (or nothing) gets
    passed straight through to the review view's _finalize() so it can include
    it in the DM sent to the submitter and in the edited review embed."""

    reason = discord.ui.TextInput(
        label="Reason (optional)",
        style=discord.TextStyle.paragraph,
        placeholder="Shown to the submitter in their DM - leave blank to skip",
        required=False,
        max_length=500,
    )

    def __init__(self, *, approved: bool, finalize_callback):
        super().__init__(title="Approve submission" if approved else "Reject submission")
        self.approved = approved
        self._finalize_callback = finalize_callback

    async def on_submit(self, interaction: Interaction):
        await self._finalize_callback(interaction, approved=self.approved, reason=self.reason.value.strip() or None)


class StatBatchSubmissionReviewView(discord.ui.View):
    """Posted alongside a /stat_submit call in the review channel. A single
    /stat_submit can fill in several stats at once (see stats.py) - this
    reviews all of them together with one Approve/Reject press instead of
    forcing staff to click through each stat_submissions row individually.

    Approving copies every pending row's value into player_stats (making it
    show up on /profile and any leaderboard for that stat_type); rejecting
    just closes out all the queue rows without touching player_stats. Either
    button opens a ReviewReasonModal first so staff can attach an optional
    note, which gets included in the submitter's DM and the edited embed.

    timeout=None + a custom_id keyed on the submission ids is what lets this
    survive a bot restart - app.py re-attaches one of these per still-pending
    batch on_ready, the same way JoinEventView does for its event id.
    """

    def __init__(self, submission_ids: list[int]):
        super().__init__(timeout=None)
        self.submission_ids = submission_ids
        ids_key = "-".join(str(i) for i in submission_ids)
        self.approve.custom_id = f"stat_batch_approve_{ids_key}"
        self.reject.custom_id = f"stat_batch_reject_{ids_key}"

    def _load_submissions(self):
        placeholders = ",".join("?" * len(self.submission_ids))
        cursor.execute(
            f"SELECT id, player_id, stat_type, value, status FROM stat_submissions WHERE id IN ({placeholders})",
            self.submission_ids
        )
        return cursor.fetchall()

    async def _finalize(self, interaction: Interaction, *, approved: bool, reason: Optional[str] = None):
        rows = self._load_submissions()
        if not rows:
            await interaction.response.send_message("This submission no longer exists.", ephemeral=True)
            return

        pending_rows = [row for row in rows if row[4] == "pending"]
        if not pending_rows:
            await interaction.response.send_message(f"This submission was already {rows[0][4]}.", ephemeral=True)
            return

        new_status = "approved" if approved else "rejected"
        player_id = pending_rows[0][1]

        for submission_id, _player_id, stat_type, value, _status in pending_rows:
            cursor.execute(
                "UPDATE stat_submissions SET status = ?, reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_status, interaction.user.id, submission_id)
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
        if reason:
            verdict_line += f"\n**Reason:** {reason}"
        original_embed = interaction.message.embeds[0]
        original_embed.description = (original_embed.description or "") + verdict_line
        await interaction.response.edit_message(embed=original_embed, view=self)
        self.stop()

        # Best-effort DM to the submitter - a failed DM (blocked/left server)
        # shouldn't stop the review itself from going through.
        cursor.execute("SELECT discord_id FROM players WHERE id = ?", (player_id,))
        player_row = cursor.fetchone()
        if player_row:
            stat_lines = "\n".join(f"- **{stat_type}**: {value:,}" for _id, _pid, stat_type, value, _status in pending_rows)
            dm_message = (
                f"✅ Your stat submission was approved and is now on your profile:\n{stat_lines}"
                if approved else
                f"❌ Your stat submission was rejected:\n{stat_lines}"
            )
            if reason:
                dm_message += f"\n\n**Staff note:** {reason}"
            try:
                submitter = await interaction.client.fetch_user(player_row[0])
                await submitter.send(dm_message)
            except (discord.Forbidden, discord.NotFound):
                pass

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green)
    async def approve(self, interaction: Interaction, button: discord.ui.Button):
        if not _is_staff_member(interaction):
            await interaction.response.send_message("You don't have permission to review submissions.", ephemeral=True)
            return
        await interaction.response.send_modal(ReviewReasonModal(approved=True, finalize_callback=self._finalize))

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.red)
    async def reject(self, interaction: Interaction, button: discord.ui.Button):
        if not _is_staff_member(interaction):
            await interaction.response.send_message("You don't have permission to review submissions.", ephemeral=True)
            return
        await interaction.response.send_modal(ReviewReasonModal(approved=False, finalize_callback=self._finalize))


class BadgeSubmissionReviewView(discord.ui.View):
    """Same shape as StatBatchSubmissionReviewView, for /badge_submit calls that
    couldn't auto-award via a linked role (BADGE_ROLE_IDS) - staff approve/reject
    from the submitted screenshot(s) instead."""

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

    async def _finalize(self, interaction: Interaction, *, approved: bool, reason: Optional[str] = None):
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
        if reason:
            verdict_line += f"\n**Reason:** {reason}"
        original_embed = interaction.message.embeds[0]
        original_embed.description = (original_embed.description or "") + verdict_line
        await interaction.response.edit_message(embed=original_embed, view=self)
        self.stop()

        cursor.execute("SELECT discord_id FROM players WHERE id = ?", (player_id,))
        player_row = cursor.fetchone()
        if player_row:
            dm_message = (
                f"✅ Your **{badge_name}** badge was approved and is now on your profile."
                if approved else
                f"❌ Your **{badge_name}** badge submission was rejected."
            )
            if reason:
                dm_message += f"\n\n**Staff note:** {reason}"
            try:
                submitter = await interaction.client.fetch_user(player_row[0])
                await submitter.send(dm_message)
            except (discord.Forbidden, discord.NotFound):
                pass

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green)
    async def approve(self, interaction: Interaction, button: discord.ui.Button):
        if not _is_staff_member(interaction):
            await interaction.response.send_message("You don't have permission to review submissions.", ephemeral=True)
            return
        await interaction.response.send_modal(ReviewReasonModal(approved=True, finalize_callback=self._finalize))

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.red)
    async def reject(self, interaction: Interaction, button: discord.ui.Button):
        if not _is_staff_member(interaction):
            await interaction.response.send_message("You don't have permission to review submissions.", ephemeral=True)
            return
        await interaction.response.send_modal(ReviewReasonModal(approved=False, finalize_callback=self._finalize))

class DrillRosterView(discord.ui.View):
    """Posted alongside a drill's roster embed in the drills channel.
    custom_id is keyed on drill_id so bot.add_view() can re-attach this after
    a restart, the same pattern as JoinEventView.

    Joining/leaving a drill deliberately does NOT require a linked Roblox
    account (unlike JoinEventView) - a drill roster is just "who's showing
    up", so it reuses whatever player row already exists for the member
    (every division member gets one automatically in app.py's on_ready),
    creating one on the fly for the rare case that's missing.
    """

    def __init__(self, drill_id: int):
        super().__init__(timeout=None)
        self.drill_id = drill_id
        self.join.custom_id = f"drill_join_{drill_id}"
        self.leave.custom_id = f"drill_leave_{drill_id}"
        self.view_roster.custom_id = f"drill_view_roster_{drill_id}"

    def _get_or_create_player_id(self, discord_id: int) -> int:
        cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
        row = cursor.fetchone()
        if row:
            return row[0]
        cursor.execute("INSERT INTO players (discord_id) VALUES (?)", (discord_id,))
        conn.commit()
        return cursor.lastrowid

    @discord.ui.button(label="Join Drill", style=discord.ButtonStyle.green)
    async def join(self, interaction: Interaction, button: discord.ui.Button):
        cursor.execute("SELECT status, max_participants, drill_name FROM drills WHERE id = ?", (self.drill_id,))
        drill = cursor.fetchone()
        if not drill:
            await interaction.response.send_message("This drill no longer exists.", ephemeral=True)
            return

        status, max_participants, drill_name = drill
        if status not in ("recruiting", "ready"):
            await interaction.response.send_message("This drill is no longer accepting participants.", ephemeral=True)
            return

        if is_user_blocked_from_drill(self.drill_id, interaction.user):
            await interaction.response.send_message("You're not able to join this drill.", ephemeral=True)
            return

        player_id = self._get_or_create_player_id(interaction.user.id)

        cursor.execute(
            "SELECT left_at FROM drill_participants WHERE drill_id = ? AND player_id = ?",
            (self.drill_id, player_id)
        )
        existing = cursor.fetchone()
        if existing and existing[0] is None:
            await interaction.response.send_message("You're already in this drill.", ephemeral=True)
            return

        if max_participants and get_active_participant_count(self.drill_id) >= max_participants:
            await interaction.response.send_message("This drill is full.", ephemeral=True)
            return

        # Upserts rather than a plain INSERT so rejoining after an earlier
        # Leave clears left_at instead of hitting the UNIQUE(drill_id, player_id)
        # constraint.
        cursor.execute("""
            INSERT INTO drill_participants (drill_id, player_id) VALUES (?, ?)
            ON CONFLICT(drill_id, player_id) DO UPDATE SET left_at = NULL, joined_at = CURRENT_TIMESTAMP
        """, (self.drill_id, player_id))
        conn.commit()

        if status == "recruiting" and max_participants and get_active_participant_count(self.drill_id) >= max_participants:
            cursor.execute("UPDATE drills SET status = 'ready' WHERE id = ?", (self.drill_id,))
            conn.commit()

        await interaction.response.send_message(f"Joined **{drill_name}** ✅", ephemeral=True)
        await refresh_drill_message(self.drill_id)

    @discord.ui.button(label="Leave Drill", style=discord.ButtonStyle.red)
    async def leave(self, interaction: Interaction, button: discord.ui.Button):
        cursor.execute("SELECT id FROM players WHERE discord_id = ?", (interaction.user.id,))
        player_row = cursor.fetchone()

        cursor.execute(
            "SELECT left_at FROM drill_participants WHERE drill_id = ? AND player_id = ?",
            (self.drill_id, player_row[0] if player_row else -1)
        )
        existing = cursor.fetchone()
        if not player_row or not existing or existing[0] is not None:
            await interaction.response.send_message("You're not in this drill.", ephemeral=True)
            return

        cursor.execute(
            "UPDATE drill_participants SET left_at = CURRENT_TIMESTAMP WHERE drill_id = ? AND player_id = ?",
            (self.drill_id, player_row[0])
        )

        # A "ready" drill (roster was full) drops back to "recruiting" once a
        # spot opens up - the count below already reflects the Leave above,
        # since it runs on the same uncommitted transaction.
        cursor.execute("SELECT status, max_participants FROM drills WHERE id = ?", (self.drill_id,))
        status, max_participants = cursor.fetchone()
        if status == "ready" and (not max_participants or get_active_participant_count(self.drill_id) < max_participants):
            cursor.execute("UPDATE drills SET status = 'recruiting' WHERE id = ?", (self.drill_id,))
        conn.commit()

        await interaction.response.send_message("Left the drill.", ephemeral=True)
        await refresh_drill_message(self.drill_id)

    @discord.ui.button(label="View Roster", style=discord.ButtonStyle.secondary)
    async def view_roster(self, interaction: Interaction, button: discord.ui.Button):
        discord_ids = get_active_participant_discord_ids(self.drill_id)
        if not discord_ids:
            await interaction.response.send_message("No one has joined yet.", ephemeral=True)
            return

        names = await resolve_display_names(interaction, discord_ids)
        desc = "\n".join(f"{i + 1}. {names.get(uid, f'Unknown ({uid})')}" for i, uid in enumerate(discord_ids))
        await interaction.response.send_message(embed=Embed(title="Drill Roster", description=desc), ephemeral=True)