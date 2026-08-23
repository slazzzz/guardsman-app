### PLAYER STATS SHOWCASE ###
# /stat_submit -> fill in whichever stats you have proof for in one go (leave
#   the rest blank - don't put 0, that's a real value for stats like deaths).
#   Queued as one row per filled-in stat in stat_submissions, all sharing a
#   batch_id, posted to STATS_REVIEW_CHANNEL_ID (pinging guild_data.staff_roles)
#   as a single message with a StatBatchSubmissionReviewView so staff
#   approve/reject the whole batch at once. Approve/Reject opens a small modal
#   for an optional reason, which gets DMed to the submitter alongside the verdict.
# /stat_add -> trusted-admin direct write, bypasses the queue entirely.
# /badge_submit -> auto-awards if the submitter already holds the Discord role
#   linked to that badge in badge_role_ids; otherwise falls into a
#   staff-reviewed queue (badge_submissions, also pinging staff_roles) same as
#   before, just without the Roblox website ownership check (too unreliable -
#   see roblox.py history). Same optional-reason-on-review as /stat_submit.
# /badge_role_sync -> staff-run: re-checks one member's currently-held roles
#   against badge_role_ids and awards anything that matches but isn't on their
#   profile yet. Needed because those roles are granted manually and can go
#   stale between when someone gets the role and when they run /badge_submit.
# /badge_add, /badge_remove -> trusted-admin direct write, same as /stat_add.
# /profile -> shows a member's verified stats, badges, and highest
#   endless-record/win role. Those roles are granted manually by staff via
#   ticket for specific win/endless thresholds, entirely outside this bot -
#   nothing here computes, assigns, or revokes them; this only reads whichever
#   role the member already holds and displays it.
# /leaderboard_stats_setup -> registers a channel + interval for a stat_type
#   so tasks.py's stat_leaderboard_loop keeps it updated automatically.
#   use_image=True switches that board to a rendered PNG instead of a text
#   embed (heavier per tick - Roblox avatar fetches + a Pillow render - so
#   it's opt-in). Re-running setup on a disabled board re-enables it.
# /leaderboard_stats_disable, /leaderboard_stats_enable -> pause/resume a
#   board's auto-updates (staff). Deleting the posted message by hand does
#   NOT stop the loop - it just posts a fresh one next tick - disable is the
#   actual off switch.
# /leaderboard_stats_image -> on-demand PNG version of any stat leaderboard.

from typing import Optional

import discord
from discord import Embed, Interaction, Member, app_commands
from discord.ext import commands

from bot.leaderboard import build_stat_leaderboard_embed, build_stat_leaderboard_image
from bot.shared import *  # noqa: F401,F403

STAT_CHOICES = [
    app_commands.Choice(name=label, value=key)
    for key, (label, _unit) in STAT_TYPES.items()
]

# Pings whichever staff roles are configured (guild_data.staff_roles) so a new
# submission actually gets noticed instead of sitting silently in the review
# channel. Empty string (no content) if none are configured.
STAFF_PING_CONTENT = " ".join(f"<@&{role_id}>" for role_id in STAFF_ROLES)
STAFF_PING_ALLOWED_MENTIONS = discord.AllowedMentions(roles=True, users=False, everyone=False)


# Discord slash commands can't take a variable-length list of attachments -
# each has to be its own named, optional parameter. Three covers "a couple
# angles / before-and-after" without the command signature getting unwieldy;
# bump this (and add matching proof_N parameters below) if that's too few.
MAX_PROOF_ATTACHMENTS = 3


async def attachments_to_files(attachments: list[discord.Attachment]) -> list[discord.File]:
    """Re-downloads each attachment and wraps it as a discord.File so it can be
    re-posted in the review channel message, alongside the text embed."""
    files = []
    for attachment in attachments:
        try:
            files.append(await attachment.to_file())
        except discord.HTTPException as e:
            print(f"Could not re-attach proof {attachment.url}: {e}")
    return files


def get_or_create_player_id(discord_id: int, roblox_id: int = 0) -> int:
    """Same get-or-create pattern used in players.py's player_register."""
    cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
    player = cursor.fetchone()
    if player:
        return player[0]

    cursor.execute("INSERT INTO players (discord_id, roblox_id) VALUES (?, ?)", (discord_id, roblox_id))
    conn.commit()
    return cursor.lastrowid


def has_roblox_linked(discord_id: int) -> bool:
    """Whether this member has a nonzero roblox_id on file - used to nudge
    people toward /roblox_link (see players.py) after a stat/badge submission,
    since neither /stat_submit nor /badge_submit ever asks for a Roblox
    account, so a first-time submitter's avatar won't show up on /profile
    until they link it themselves."""
    cursor.execute("SELECT roblox_id FROM players WHERE discord_id = ?", (discord_id,))
    row = cursor.fetchone()
    return bool(row and row[0])


ROBLOX_LINK_HINT = "\n-# Tip: run `/roblox_link` to add your Roblox account so your avatar shows up on `/profile`."


def highest_held_role(member: Member, tiered_role_ids: list[int]) -> Optional[discord.Role]:
    """tiered_role_ids is ordered lowest -> highest tier; returns the member's
    highest-tier role from that list they actually hold, or None."""
    held_ids = {role.id for role in member.roles}
    for role_id in reversed(tiered_role_ids):
        if role_id in held_ids:
            return member.guild.get_role(role_id)
    return None


class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="stat_submit",
        description="Submit one or more stats for staff to verify (needs proof)"
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.describe(
        hadal_wins=f"{STAT_TYPES['hadal_wins'][0]} - leave blank if you're not submitting this one",
        endless_record=f"{STAT_TYPES['endless_record'][0]} - leave blank if you're not submitting this one",
        modifier_wins=f"{STAT_TYPES['modifier_wins'][0]} - leave blank if you're not submitting this one",
        death_count=f"{STAT_TYPES['death_count'][0]} - leave blank if you're not submitting this one",
        heartburn_score=f"{STAT_TYPES['heartburn_score'][0]} - leave blank if you're not submitting this one",
        heartburn_wins=f"{STAT_TYPES['heartburn_wins'][0]} - leave blank if you're not submitting this one",
        raveyard_wins=f"{STAT_TYPES['raveyard_wins'][0]} - leave blank if you're not submitting this one",
        hunted_wins=f"{STAT_TYPES['hunted_wins'][0]} - leave blank if you're not submitting this one",
        firewall_record=f"{STAT_TYPES['firewall_record'][0]} - leave blank if you're not submitting this one",
        robux_spent=f"{STAT_TYPES['robux_spent'][0]} - leave blank if you're not submitting this one",
        max_modifier_percentage=f"{STAT_TYPES['max_modifier_percentage'][0]} - leave blank if you're not submitting this one",
        modifier_wins_1star=f"{STAT_TYPES['modifier_wins_1star'][0]} - 1★ runs only - leave blank if you're not submitting this one",
        modifier_wins_2star=f"{STAT_TYPES['modifier_wins_2star'][0]} - 2★ runs only - leave blank if you're not submitting this one",
        modifier_wins_3star=f"{STAT_TYPES['modifier_wins_3star'][0]} - 3★ runs only - leave blank if you're not submitting this one",
        modifier_wins_4star=f"{STAT_TYPES['modifier_wins_4star'][0]} - 4★ runs only - leave blank if you're not submitting this one",
        proof="Screenshot(s) covering whichever stat(s) you filled in above",
    )
    @is_allowed()
    async def stat_submit(
        self,
        interaction: Interaction,
        proof: discord.Attachment,
        hadal_wins: Optional[int] = None,
        endless_record: Optional[int] = None,
        modifier_wins: Optional[int] = None,
        death_count: Optional[int] = None,
        heartburn_score: Optional[int] = None,
        heartburn_wins: Optional[int] = None,
        raveyard_wins: Optional[int] = None,
        hunted_wins: Optional[int] = None,
        firewall_record: Optional[int] = None,
        robux_spent: Optional[int] = None,
        max_modifier_percentage: Optional[int] = None,
        modifier_wins_1star: Optional[int] = None,
        modifier_wins_2star: Optional[int] = None,
        modifier_wins_3star: Optional[int] = None,
        modifier_wins_4star: Optional[int] = None,
        proof_2: Optional[discord.Attachment] = None,
        proof_3: Optional[discord.Attachment] = None,
        proof_4: Optional[discord.Attachment] = None,
        proof_5: Optional[discord.Attachment] = None,
        proof_6: Optional[discord.Attachment] = None,
        proof_7: Optional[discord.Attachment] = None,
        proof_8: Optional[discord.Attachment] = None,
        proof_9: Optional[discord.Attachment] = None,
        proof_10: Optional[discord.Attachment] = None,
    ):
        # Left blank (None) means "not submitting this one" - unlike a 0-means-
        # skip convention, this doesn't break stats where 0 is a real, valid
        # value to submit (e.g. a flawless death_count run).
        submitted = {
            "hadal_wins": hadal_wins,
            "endless_record": endless_record,
            "modifier_wins": modifier_wins,
            "death_count": death_count,
            "heartburn_score": heartburn_score,
            "heartburn_wins": heartburn_wins,
            "raveyard_wins": raveyard_wins,
            "hunted_wins": hunted_wins,
            "firewall_record": firewall_record,
            "robux_spent": robux_spent,
            "max_modifier_percentage": max_modifier_percentage,
            "modifier_wins_1star": modifier_wins_1star,
            "modifier_wins_2star": modifier_wins_2star,
            "modifier_wins_3star": modifier_wins_3star,
            "modifier_wins_4star": modifier_wins_4star,
        }
        provided = {stat_type: value for stat_type, value in submitted.items() if value is not None}

        if not provided:
            await interaction.response.send_message(
                "Fill in at least one stat to submit.", ephemeral=True
            )
            return

        negative = [stat_type for stat_type, value in provided.items() if value < 0]
        if negative:
            await interaction.response.send_message(
                f"{', '.join(STAT_TYPES[s][0] for s in negative)} can't be negative.", ephemeral=True
            )
            return

        if not STATS_REVIEW_CHANNEL_ID:
            await interaction.response.send_message(
                "Stat submissions aren't set up yet - ask staff to add stats_review_channel_id to the config.",
                ephemeral=True
            )
            return

        proofs = [a for a in (proof, proof_2, proof_3, proof_4, proof_5, proof_6, proof_7, proof_8, proof_9, proof_10) if a is not None]
        player_id = get_or_create_player_id(interaction.user.id)
        proof_url = "\n".join(a.url for a in proofs)

        try:
            submission_ids = []
            for stat_type, value in provided.items():
                cursor.execute(
                    "INSERT INTO stat_submissions (player_id, stat_type, value, proof_url) VALUES (?, ?, ?, ?)",
                    (player_id, stat_type, value, proof_url)
                )
                submission_ids.append(cursor.lastrowid)

            # Tag every row from this call with the same batch_id (the first
            # row's id) so they're reviewed together and so app.py can
            # re-group them into one view after a restart.
            batch_id = min(submission_ids)
            placeholders = ",".join("?" * len(submission_ids))
            cursor.execute(
                f"UPDATE stat_submissions SET batch_id = ? WHERE id IN ({placeholders})",
                (batch_id, *submission_ids)
            )
            conn.commit()
        except sqlite3.Error as e:
            await interaction.response.send_message(f"Could not record submission: {e}", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        stat_lines = "\n".join(f"**{STAT_TYPES[s][0]}:** {v:,}" for s, v in provided.items())
        review_embed = Embed(
            title="New stat submission",
            description=f"**Player:** {interaction.user.mention}\n{stat_lines}",
            color=discord.Color.blurple(),
        )
        review_embed.set_footer(text=f"Batch #{batch_id} - {len(proofs)} proof file(s) attached below")

        try:
            review_channel = self.bot.get_channel(STATS_REVIEW_CHANNEL_ID) or await self.bot.fetch_channel(STATS_REVIEW_CHANNEL_ID)
            files = await attachments_to_files(proofs)
            await review_channel.send(
                content=STAFF_PING_CONTENT or None,
                embed=review_embed,
                files=files,
                view=StatBatchSubmissionReviewView(submission_ids),
                allowed_mentions=STAFF_PING_ALLOWED_MENTIONS,
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            print(f"Could not post submission batch #{batch_id} for review: {e}")
            await interaction.followup.send(
                "Submission saved, but I couldn't post it to the review channel - let staff know.",
                ephemeral=True
            )
            return

        success_message = "Submitted for review ✅ - you'll get a DM once staff decide."
        if not has_roblox_linked(interaction.user.id):
            success_message += ROBLOX_LINK_HINT
        await interaction.followup.send(success_message, ephemeral=True)

    @app_commands.command(name="stat_add", description="Directly set a player's stat (trusted admin, no review needed)")
    @app_commands.guilds(GUILD_ID)
    @app_commands.choices(stat_type=STAT_CHOICES)
    @is_admin_or_staff()
    async def stat_add(self, interaction: Interaction, user: Member, stat_type: app_commands.Choice[str], value: int):
        if value < 0:
            await interaction.response.send_message("Value can't be negative.", ephemeral=True)
            return

        player_id = get_or_create_player_id(user.id)
        cursor.execute("""
            INSERT INTO player_stats (player_id, stat_type, value, source, verified_by, updated_at)
            VALUES (?, ?, ?, 'admin', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(player_id, stat_type) DO UPDATE SET
                value = excluded.value, source = 'admin',
                verified_by = excluded.verified_by, updated_at = CURRENT_TIMESTAMP
        """, (player_id, stat_type.value, value, interaction.user.id))
        conn.commit()

        await interaction.response.send_message(f"Set **{stat_type.name}** to **{value:,}** for {user.mention}.", ephemeral=True)

    @app_commands.command(name="badge_add", description="Award a badge to a player's profile (trusted admin)")
    @app_commands.guilds(GUILD_ID)
    @is_admin_or_staff()
    async def badge_add(self, interaction: Interaction, user: Member, badge_name: str):
        player_id = get_or_create_player_id(user.id)
        try:
            cursor.execute(
                "INSERT INTO player_badges (player_id, badge_name, awarded_by, source) VALUES (?, ?, ?, 'manual')",
                (player_id, badge_name, interaction.user.id)
            )
            conn.commit()
        except sqlite3.IntegrityError:
            await interaction.response.send_message(f"{user.mention} already has that badge.", ephemeral=True)
            return

        await interaction.response.send_message(f"Awarded **{badge_name}** to {user.mention}.", ephemeral=True)

    @app_commands.command(
        name="badge_submit",
        description="Claim a Roblox badge for your profile (auto-awarded if you hold the linked role)"
    )
    @app_commands.guilds(GUILD_ID)
    @is_allowed()
    async def badge_submit(
        self,
        interaction: Interaction,
        proof: discord.Attachment,
        badge_id: int,
        proof_2: Optional[discord.Attachment] = None,
        proof_3: Optional[discord.Attachment] = None,
    ):
        await interaction.response.defer(ephemeral=True)

        badge_name = get_badge_name(badge_id)
        if badge_name is None:
            await interaction.followup.send(f"Couldn't find a Roblox badge with id `{badge_id}`.", ephemeral=True)
            return

        player_id = get_or_create_player_id(interaction.user.id)

        # Role-based check first: if this badge is mapped to a Discord role
        # and the submitter already holds it, award immediately - no proof,
        # no queue. member.roles works here since this command is guild-only.
        linked_role_id = BADGE_ROLE_IDS.get(badge_id)
        if linked_role_id and any(role.id == linked_role_id for role in interaction.user.roles):
            try:
                cursor.execute(
                    "INSERT INTO player_badges (player_id, badge_name, awarded_by, source) VALUES (?, ?, ?, 'role')",
                    (player_id, badge_name, interaction.user.id)
                )
                conn.commit()
            except sqlite3.IntegrityError:
                await interaction.followup.send(f"You already have **{badge_name}** on your profile.", ephemeral=True)
                return

            verdict_message = f"✅ Verified via your linked role - **{badge_name}** is now on your profile."
            if not has_roblox_linked(interaction.user.id):
                verdict_message += ROBLOX_LINK_HINT
            await interaction.followup.send(verdict_message, ephemeral=True)
            return

        # No role match (either nothing's mapped for this badge, or the
        # submitter doesn't currently hold it) - fall back to a
        # screenshot-reviewed queue instead of rejecting outright, since the
        # role could just be stale (see /badge_role_sync).
        proofs = [a for a in (proof, proof_2, proof_3) if a is not None]
        if not proofs:
            reason = (
                "you don't currently hold the linked role - ask staff to run `/badge_role_sync` if that's out of date"
                if linked_role_id else
                "no role is linked to this badge"
            )
            await interaction.followup.send(
                f"Couldn't auto-verify **{badge_name}** ({reason}) - "
                "attach at least one screenshot as proof and run this again.",
                ephemeral=True
            )
            return

        if not STATS_REVIEW_CHANNEL_ID:
            await interaction.followup.send(
                "Badge submissions aren't set up yet - ask staff to add stats_review_channel_id to the config.",
                ephemeral=True
            )
            return

        cursor.execute(
            "INSERT INTO badge_submissions (player_id, badge_id, badge_name, proof_url) VALUES (?, ?, ?, ?)",
            (player_id, badge_id, badge_name, "\n".join(a.url for a in proofs))
        )
        conn.commit()
        submission_id = cursor.lastrowid

        review_embed = Embed(
            title=f"New badge submission - {badge_name}",
            description=f"**Player:** {interaction.user.mention}\n**Badge ID:** {badge_id}",
            color=discord.Color.blurple(),
        )
        review_embed.set_footer(text=f"Submission #{submission_id} - {len(proofs)} proof file(s) attached below")

        try:
            review_channel = self.bot.get_channel(STATS_REVIEW_CHANNEL_ID) or await self.bot.fetch_channel(STATS_REVIEW_CHANNEL_ID)
            files = await attachments_to_files(proofs)
            await review_channel.send(
                content=STAFF_PING_CONTENT or None,
                embed=review_embed,
                files=files,
                view=BadgeSubmissionReviewView(submission_id),
                allowed_mentions=STAFF_PING_ALLOWED_MENTIONS,
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            print(f"Could not post badge submission #{submission_id} for review: {e}")
            await interaction.followup.send(
                "Submission saved, but I couldn't post it to the review channel - let staff know.",
                ephemeral=True
            )
            return

        success_message = "Submitted for staff review ✅ - you'll get a DM once they decide."
        if not has_roblox_linked(interaction.user.id):
            success_message += ROBLOX_LINK_HINT
        await interaction.followup.send(success_message, ephemeral=True)

    @app_commands.command(
        name="badge_role_sync",
        description="Re-check a member's badge roles and award any matching badges (staff)"
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin_or_staff()
    async def badge_role_sync(self, interaction: Interaction, user: Member):
        if not BADGE_ROLE_IDS:
            await interaction.response.send_message(
                "No badge_role_ids configured - add one to stats_data in bot_data.json first.",
                ephemeral=True
            )
            return

        player_id = get_or_create_player_id(user.id)
        held_role_ids = {role.id for role in user.roles}

        awarded, already_had = [], []
        for badge_id, role_id in BADGE_ROLE_IDS.items():
            if role_id not in held_role_ids:
                continue

            badge_name = get_badge_name(badge_id) or f"Badge {badge_id}"
            try:
                cursor.execute(
                    "INSERT INTO player_badges (player_id, badge_name, awarded_by, source) VALUES (?, ?, ?, 'role')",
                    (player_id, badge_name, interaction.user.id)
                )
                conn.commit()
                awarded.append(badge_name)
            except sqlite3.IntegrityError:
                already_had.append(badge_name)

        if not awarded and not already_had:
            await interaction.response.send_message(
                f"{user.mention} doesn't hold any role linked to a badge - nothing to sync.",
                ephemeral=True
            )
            return

        lines = []
        if awarded:
            lines.append(f"Awarded: {', '.join(awarded)}")
        if already_had:
            lines.append(f"Already had: {', '.join(already_had)}")
        await interaction.response.send_message(f"Synced badge roles for {user.mention}.\n" + "\n".join(lines), ephemeral=True)

    @app_commands.command(name="badge_remove", description="Remove a badge from a player's profile (trusted admin)")
    @app_commands.guilds(GUILD_ID)
    @is_admin_or_staff()
    async def badge_remove(self, interaction: Interaction, user: Member, badge_name: str):
        player_id = get_or_create_player_id(user.id)
        cursor.execute(
            "DELETE FROM player_badges WHERE player_id = ? AND badge_name = ?",
            (player_id, badge_name)
        )
        conn.commit()

        if cursor.rowcount == 0:
            await interaction.response.send_message(f"{user.mention} doesn't have that badge.", ephemeral=True)
            return

        await interaction.response.send_message(f"Removed **{badge_name}** from {user.mention}.", ephemeral=True)

    @app_commands.command(name="profile", description="Show a division member's Pressure stats card")
    @app_commands.guilds(GUILD_ID)
    @is_allowed()
    async def profile(self, interaction: Interaction, user: Optional[Member] = None):
        member = user or interaction.user

        cursor.execute("SELECT id, roblox_id FROM players WHERE discord_id = ?", (member.id,))
        player = cursor.fetchone()
        if not player:
            await interaction.response.send_message(f"{member.mention} has no stats on file yet.", ephemeral=True)
            return
        player_id, roblox_id = player

        cursor.execute("SELECT stat_type, value FROM player_stats WHERE player_id = ?", (player_id,))
        stats = dict(cursor.fetchall())

        cursor.execute("SELECT badge_name FROM player_badges WHERE player_id = ? ORDER BY awarded_at", (player_id,))
        badges = [row[0] for row in cursor.fetchall()]

        embed = Embed(title=f"{member.display_name}'s Pressure Profile", color=discord.Color.dark_gold())

        if stats:
            embed.add_field(
                name="Stats",
                value="\n".join(f"**{STAT_TYPES[key][0]}:** {value:,}" for key, value in stats.items() if key in STAT_TYPES),
                inline=False
            )
        else:
            embed.add_field(name="Stats", value="No verified stats yet - `/stat_submit` to add one!", inline=False)

        if badges:
            embed.add_field(name="Badges", value=" • ".join(badges), inline=False)

        role_lines = []
        if ENDLESS_RECORD_ROLE_IDS:
            role = highest_held_role(member, ENDLESS_RECORD_ROLE_IDS)
            if role:
                role_lines.append(f"Endless Record tier: {role.mention}")
        if WIN_ROLE_IDS:
            role = highest_held_role(member, WIN_ROLE_IDS)
            if role:
                role_lines.append(f"Wins tier: {role.mention}")
        if role_lines:
            embed.add_field(name="Division Roles", value="\n".join(role_lines), inline=False)

        if roblox_id:
            avatar_url = get_full_avatar_url(roblox_id)
            if avatar_url:
                embed.set_image(url=avatar_url)
        elif member.id == interaction.user.id:
            embed.set_footer(text="No Roblox account linked yet - run /roblox_link to add your avatar here.")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard_stats_setup", description="Set an auto-updating leaderboard channel for a stat (staff)")
    @app_commands.guilds(GUILD_ID)
    @app_commands.choices(stat_type=STAT_CHOICES)
    @app_commands.describe(
        use_image="Post a rendered image instead of a text embed - heavier to generate every tick, so opt in deliberately"
    )
    @is_admin_or_staff()
    async def leaderboard_stats_setup(
        self,
        interaction: Interaction,
        stat_type: app_commands.Choice[str],
        channel: discord.TextChannel,
        interval_minutes: int = 60,
        use_image: bool = False,
    ):
        if interval_minutes < 5:
            await interaction.response.send_message("interval_minutes must be at least 5.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # enabled = 1 unconditionally, so re-running setup on a previously
        # /leaderboard_stats_disable'd board re-enables it too.
        cursor.execute("""
            INSERT INTO stat_leaderboards (stat_type, channel_id, update_interval_minutes, use_image, enabled)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(stat_type) DO UPDATE SET
                channel_id = excluded.channel_id, update_interval_minutes = excluded.update_interval_minutes,
                use_image = excluded.use_image, enabled = 1
        """, (stat_type.value, channel.id, interval_minutes, int(use_image)))
        conn.commit()

        if use_image:
            file = await build_stat_leaderboard_image(interaction.guild, stat_type.value)
            message = await channel.send(file=file)
        else:
            embed = await build_stat_leaderboard_embed(interaction.guild, stat_type.value)
            message = await channel.send(embed=embed)

        cursor.execute(
            "UPDATE stat_leaderboards SET message_id = ?, last_updated_at = CURRENT_TIMESTAMP WHERE stat_type = ?",
            (message.id, stat_type.value)
        )
        conn.commit()

        mode_note = " as an image" if use_image else ""
        await interaction.followup.send(
            f"**{stat_type.name}** leaderboard will now auto-update in {channel.mention}{mode_note} every {interval_minutes} minutes.",
            ephemeral=True
        )

    @app_commands.command(
        name="leaderboard_stats_disable",
        description="Stop an auto-updating leaderboard from refreshing (staff). Deleting its message alone won't stop it.",
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.choices(stat_type=STAT_CHOICES)
    @is_admin_or_staff()
    async def leaderboard_stats_disable(self, interaction: Interaction, stat_type: app_commands.Choice[str]):
        cursor.execute("SELECT enabled FROM stat_leaderboards WHERE stat_type = ?", (stat_type.value,))
        row = cursor.fetchone()
        if not row:
            await interaction.response.send_message(f"**{stat_type.name}** was never set up with `/leaderboard_stats_setup`.", ephemeral=True)
            return
        if not row[0]:
            await interaction.response.send_message(f"**{stat_type.name}** is already disabled.", ephemeral=True)
            return

        cursor.execute("UPDATE stat_leaderboards SET enabled = 0 WHERE stat_type = ?", (stat_type.value,))
        conn.commit()

        await interaction.response.send_message(
            f"**{stat_type.name}** will stop auto-updating. Its last posted message is left as-is - "
            f"delete it yourself if you don't want it lingering. Run `/leaderboard_stats_setup` again to resume.",
            ephemeral=True
        )

    @app_commands.command(
        name="leaderboard_stats_enable",
        description="Resume an auto-updating leaderboard previously stopped with /leaderboard_stats_disable (staff)",
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.choices(stat_type=STAT_CHOICES)
    @is_admin_or_staff()
    async def leaderboard_stats_enable(self, interaction: Interaction, stat_type: app_commands.Choice[str]):
        cursor.execute("SELECT enabled, channel_id FROM stat_leaderboards WHERE stat_type = ?", (stat_type.value,))
        row = cursor.fetchone()
        if not row:
            await interaction.response.send_message(f"**{stat_type.name}** was never set up - use `/leaderboard_stats_setup` instead.", ephemeral=True)
            return
        if row[0]:
            await interaction.response.send_message(f"**{stat_type.name}** is already enabled.", ephemeral=True)
            return

        cursor.execute("UPDATE stat_leaderboards SET enabled = 1 WHERE stat_type = ?", (stat_type.value,))
        conn.commit()

        channel_mention = f"<#{row[1]}>"
        await interaction.response.send_message(f"**{stat_type.name}** will resume auto-updating in {channel_mention}.", ephemeral=True)

    @app_commands.command(name="leaderboard_stats_image", description="Render a stat leaderboard as an image, on demand")
    @app_commands.guilds(GUILD_ID)
    @app_commands.choices(stat_type=STAT_CHOICES)
    @is_admin_or_staff()
    async def leaderboard_stats_image(self, interaction: Interaction, stat_type: app_commands.Choice[str]):
        await interaction.response.defer()
        file = await build_stat_leaderboard_image(interaction.guild, stat_type.value)
        if file is None:
            await interaction.followup.send("Unknown stat type.")
            return
        await interaction.followup.send(file=file)


async def setup(bot: commands.Bot):
    await bot.add_cog(StatsCog(bot))