### DRILL COMMANDS ###

from datetime import datetime
from typing import Optional, Union

import discord
from discord import Embed, Interaction, app_commands
from discord.ext import commands

from bot.shared import *  # noqa: F401,F403


class DrillsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="drill_create",
        description="Create a new Guardsman Drill and post it in the drills channel.",
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.choices(
        size=[app_commands.Choice(name=size_name.capitalize(), value=size_name) for size_name in DRILL_SIZES]
    )
    @app_commands.describe(
        name="What to call this drill (e.g. 'No-Death Drill').",
        objective="What participants are trying to do.",
        size="Drill size tier - sets the default roster cap.",
        max_participants="Override the roster cap. Leave at 0 to use the size's default (uncapped for Mega).",
    )
    @is_host()
    @cooldown(DRILL_CREATE_COOLDOWN_SECONDS)
    async def drill_create(self, interaction: Interaction, name: str, objective: str, size: str, max_participants: int = 0):
        if max_participants < 0:
            await interaction.response.send_message("max_participants must be greater than 0.", ephemeral=True)
            return

        # One active drill per host at a time. Doesn't touch the cooldown
        # above - that stays in place specifically to stop the loophole this
        # alone wouldn't cover: cancel your one active drill, then spam
        # /drill_create again immediately. With this check, a host is only
        # ever blocked from creating a SECOND simultaneous drill; the
        # cooldown is what stops rapid cancel-then-recreate cycles.
        cursor.execute(
            "SELECT id FROM drills WHERE host_discord_id = ? AND status IN ('recruiting', 'ready', 'in_progress')",
            (interaction.user.id,)
        )
        existing_drill = cursor.fetchone()
        if existing_drill:
            await interaction.response.send_message(
                f"You already have an active drill (#{existing_drill[0]}) - "
                f"/drill_end or /drill_cancel it before creating another.",
                ephemeral=True
            )
            return

        if DRILLS_CHANNEL_ID is None:
            await interaction.response.send_message(
                "Drills channel not configured - add drill_data.drills_channel_id to bot_data.json.", ephemeral=True
            )
            return

        drills_channel = bot.get_channel(DRILLS_CHANNEL_ID)
        if drills_channel is None:
            await interaction.response.send_message(
                "Drills channel not found - check drill_data.drills_channel_id.", ephemeral=True
            )
            return

        resolved_max = max_participants or DRILL_SIZES[size][1]

        cursor.execute(
            "INSERT INTO drills (season_id, host_discord_id, drill_name, drill_size, objective, max_participants) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (get_active_season_id(), interaction.user.id, name, size, objective, resolved_max)
        )
        conn.commit()
        drill_id = cursor.lastrowid

        # Copy the host's standing block/allow list (if any) into this
        # drill's own overrides - a COPY, not a live link, so editing either
        # one afterward doesn't touch the other. See /drill_default_block.
        cursor.execute(
            "SELECT target_type, target_id, permission FROM drill_host_defaults WHERE host_discord_id = ?",
            (interaction.user.id,)
        )
        for target_type, target_id, permission in cursor.fetchall():
            cursor.execute(
                "INSERT INTO drill_vc_overrides (drill_id, target_type, target_id, permission, set_by) VALUES (?, ?, ?, ?, ?)",
                (drill_id, target_type, target_id, permission, interaction.user.id)
            )
        conn.commit()

        cursor.execute("SELECT * FROM drills WHERE id = ?", (drill_id,))
        drill = cursor.fetchone()

        embed = build_drill_embed(drill, 0)
        view = DrillRosterView(drill_id)

        try:
            message = await drills_channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to post in the drills channel.", ephemeral=True
            )
            return

        cursor.execute(
            "UPDATE drills SET roster_message_id = ?, roster_channel_id = ? WHERE id = ?",
            (message.id, message.channel.id, drill_id)
        )
        conn.commit()

        await interaction.response.send_message(f"Drill #{drill_id} created in {drills_channel.mention} ✅", ephemeral=True)


    @app_commands.command(
        name="drill_start",
        description="Start a drill: creates a voice channel and marks it in progress.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_host()
    async def drill_start(self, interaction: Interaction):
        drill = await require_active_drill(interaction)
        if drill is None:
            return

        d = drill_as_dict(drill)

        if d["status"] not in ("recruiting", "ready"):
            await interaction.response.send_message(f"This drill is already {d['status']}.", ephemeral=True)
            return

        category = interaction.guild.get_channel(DRILL_VC_CATEGORY_ID) if DRILL_VC_CATEGORY_ID else None
        # Discord caps user_limit at 99 - a max_participants above that just
        # means "no real cap enforced by Discord itself", the roster count in
        # the embed is still accurate either way.
        user_limit = min(d["max_participants"], 99) if d["max_participants"] else 0
        try:
            vc = await interaction.guild.create_voice_channel(
                f"Guardsman Drill #{d['id']}", category=category, user_limit=user_limit
            )
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to create voice channels.", ephemeral=True)
            return

        cursor.execute(
            "UPDATE drills SET status = 'in_progress', vc_channel_id = ?, started_at = CURRENT_TIMESTAMP WHERE id = ?",
            (vc.id, d["id"])
        )
        conn.commit()

        # Applies whatever the host/staff already set up during recruiting
        # (blocks, allows, private mode) now that there's an actual channel
        # to apply them to.
        await sync_drill_vc_permissions(interaction.guild, d["id"])

        await refresh_drill_message(d["id"])
        await interaction.response.send_message(f"Drill #{d['id']} started - jump into {vc.mention} ✅")

        for discord_id in get_active_participant_discord_ids(d["id"]):
            try:
                user = await bot.fetch_user(discord_id)
                await user.send(f"🛡️ **{d['drill_name']}** is starting now - join {vc.mention}!")
            except (discord.Forbidden, discord.NotFound):
                pass


    @app_commands.command(
        name="drill_end",
        description="End a drill, logging proof it happened and how many participants completed the objective.",
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.describe(
        proof_message_link=(
            "Link to your win/proof message (with screenshot(s)) - right-click it in "
            "the proof channel and choose Copy Message Link."
        ),
        completed_count="How many participants completed the objective. Leave at -1 to count everyone as completed.",
    )
    @is_host()
    # Fact-checking for /drill_end, modeled on how the Leader Division logs TBS
    # wins: the host posts a result message with screenshots (and whatever
    # else they want to include, e.g. a note on who showed up) in a dedicated
    # channel, then links that exact message here rather than the bot just
    # taking a host's word for a number. It's still fundamentally an honor
    # system - nobody's verifying the screenshot actually shows a completed
    # objective - but it forces every completed drill to leave a durable,
    # public, timestamped paper trail that staff (or anyone) can go check
    # after the fact, which a bare completed_count integer doesn't.
    #
    # _resolve_proof_message() below is what actually enforces this - see its
    # docstring for the specific things it checks and why. Two things this
    # DOESN'T need to duplicate from the Leader Division pattern: "who
    # participated" doesn't need to be manually written into the proof
    # message, since the roster is already tracked automatically via the
    # Join/Leave buttons (drill_participants) - the proof message only needs
    # to carry what the bot can't already see for itself (the screenshot).
    async def drill_end(
        self, interaction: Interaction, proof_message_link: str, completed_count: int = -1
    ):
        drill = await require_active_drill(interaction)
        if drill is None:
            return

        d = drill_as_dict(drill)

        if d["status"] not in ("recruiting", "ready", "in_progress"):
            await interaction.response.send_message(f"This drill is already {d['status']}.", ephemeral=True)
            return

        participant_count = get_active_participant_count(d["id"])

        if completed_count < 0:
            completed_count = participant_count
        elif completed_count > participant_count:
            await interaction.response.send_message("completed_count can't be greater than the roster size.", ephemeral=True)
            return

        proof_message = await self._resolve_proof_message(interaction, d, proof_message_link)
        if proof_message is None:
            # _resolve_proof_message already sent the ephemeral explanation.
            return

        cursor.execute(
            "UPDATE drills SET status = 'completed', ended_at = CURRENT_TIMESTAMP, "
            "proof_channel_id = ?, proof_message_id = ? WHERE id = ?",
            (proof_message.channel.id, proof_message.id, d["id"])
        )
        conn.commit()

        if d["vc_channel_id"]:
            vc = interaction.guild.get_channel(d["vc_channel_id"])
            if vc:
                try:
                    await vc.delete(reason=f"Drill #{d['id']} ended")
                except (discord.Forbidden, discord.HTTPException) as e:
                    print(f"Could not delete VC for drill {d['id']}: {e}")

        await refresh_drill_message(d["id"])
        await post_drill_completion_log(interaction.guild, d["id"], participant_count, completed_count)

        failed_count = participant_count - completed_count
        await interaction.response.send_message(
            f"**Drill #{d['id']} complete - {d['drill_name']}**\n"
            f"Participants: {participant_count}\n"
            f"Completed: {completed_count}\n"
            f"Failed: {failed_count}\n"
            f"Proof: {proof_message.jump_url}\n\n"
            f"Results have been recorded ✅"
        )


    async def _resolve_proof_message(
        self, interaction: Interaction, d: dict, proof_message_link: str
    ) -> Optional[discord.Message]:
        """Fetches and validates the message a host/staff linked as proof a
        drill's objective was completed. On any failure, sends the ephemeral
        explanation itself and returns None - callers just need to bail out
        when they get None back, not send their own error.

        Checks, in order:
          1. It's actually a Discord message link, for this server.
          2. It's in the configured proof channel (bot.config.DRILL_PROOF_CHANNEL_ID)
             - skipped entirely if that isn't configured, though setting one
             up is what makes this whole thing worth having: a fixed,
             predictable place staff can go audit after the fact, rather
             than proof potentially scattered across any channel the bot can
             read.
          3. The message is actually fetchable (still exists, bot can see it).
          4. It has at least one attachment - a bare text message isn't proof
             of anything.
          5. It was posted after the drill actually started (drills.started_at,
             set by /drill_start) - stops an old screenshot from some earlier,
             unrelated run being recycled as "proof" for this drill. Skipped
             if the drill was somehow ended without ever being started.
          6. It isn't already logged as proof on a different drill - stops
             one screenshot backing two separate completions.
        """
        parsed = parse_message_link(proof_message_link)
        if parsed is None:
            await interaction.response.send_message(
                "That doesn't look like a message link - right-click the proof message "
                "and choose **Copy Message Link**, then paste the whole thing here.",
                ephemeral=True
            )
            return None

        link_guild_id, channel_id, message_id = parsed
        if link_guild_id != interaction.guild.id:
            await interaction.response.send_message("That message link is from a different server.", ephemeral=True)
            return None

        if DRILL_PROOF_CHANNEL_ID and channel_id != DRILL_PROOF_CHANNEL_ID:
            await interaction.response.send_message(
                f"Proof has to be posted in <#{DRILL_PROOF_CHANNEL_ID}> - post it there, then paste its link here.",
                ephemeral=True
            )
            return None

        try:
            channel = interaction.guild.get_channel(channel_id) or await interaction.guild.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                "Couldn't find that message - double check the link and try again.", ephemeral=True
            )
            return None

        if not message.attachments:
            await interaction.response.send_message(
                "That message doesn't have any screenshots attached - proof needs at least one.",
                ephemeral=True
            )
            return None

        if d["started_at"]:
            started_at = datetime.strptime(d["started_at"], "%Y-%m-%d %H:%M:%S")
            if message.created_at.replace(tzinfo=None) < started_at:
                await interaction.response.send_message(
                    "That message was posted before this drill started, so it can't be proof for it - "
                    "post a fresh result message and link that one instead.",
                    ephemeral=True
                )
                return None

        conflicting_drill_id = find_drill_using_proof(channel_id, message_id, exclude_drill_id=d["id"])
        if conflicting_drill_id is not None:
            await interaction.response.send_message(
                f"That message is already logged as proof for drill #{conflicting_drill_id} - "
                f"each proof message can only back one drill.",
                ephemeral=True
            )
            return None

        return message


    @app_commands.command(
        name="drill_cancel",
        description="Cancel a drill before it happens.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_host()
    async def drill_cancel(self, interaction: Interaction):
        drill = await require_active_drill(interaction)
        if drill is None:
            return

        d = drill_as_dict(drill)

        if d["status"] in ("completed", "cancelled"):
            await interaction.response.send_message(f"This drill is already {d['status']}.", ephemeral=True)
            return

        cursor.execute("UPDATE drills SET status = 'cancelled', ended_at = CURRENT_TIMESTAMP WHERE id = ?", (d["id"],))
        conn.commit()

        if d["vc_channel_id"]:
            vc = interaction.guild.get_channel(d["vc_channel_id"])
            if vc:
                try:
                    await vc.delete(reason=f"Drill #{d['id']} cancelled")
                except (discord.Forbidden, discord.HTTPException) as e:
                    print(f"Could not delete VC for drill {d['id']}: {e}")

        await refresh_drill_message(d["id"])
        await interaction.response.send_message(f"Drill #{d['id']} ({d['drill_name']}) cancelled.", ephemeral=True)


    @app_commands.command(
        name="drill_list",
        description="List Guardsman Drills.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_allowed()
    async def drill_list(self, interaction: Interaction):
        cursor.execute("SELECT id, drill_name, drill_size, status, max_participants FROM drills ORDER BY id DESC")
        drills = cursor.fetchall()

        if not drills:
            await interaction.response.send_message("No drills yet.", ephemeral=True)
            return

        lines = []
        for drill_id, drill_name, drill_size, status, max_participants in drills:
            count = get_active_participant_count(drill_id)
            roster = f"{count}/{max_participants}" if max_participants else str(count)
            lines.append(
                f"**#{drill_id}.** {drill_name} - {drill_size.capitalize()} - "
                f"{roster} - {DRILL_STATUS_LABELS.get(status, status)}"
            )

        pages = [lines[i:i + EVENTS_PER_PAGE] for i in range(0, len(lines), EVENTS_PER_PAGE)]
        embeds = [
            Embed(title="Guardsman Drills", description="\n".join(page))
            .set_footer(text=f"Page {i + 1}/{len(pages)}")
            for i, page in enumerate(pages)
        ]

        if len(embeds) == 1:
            await interaction.response.send_message(embed=embeds[0])
        else:
            view = PaginatorView(embeds, interaction.user.id)
            await interaction.response.send_message(embed=embeds[0], view=view)


    ### VOICE CHANNEL ACCESS CONTROL ###
    # These manage bot.database's drill_vc_overrides (per-drill) and
    # drill_banned_users (division-wide) tables - see sync_drill_vc_permissions()
    # in bot/drills.py for exactly how the two combine into a channel's actual
    # permission overwrites, and the precedence between them.

    @app_commands.command(
        name="drill_vc_mode",
        description="Set whether a drill's voice channel is open to everyone or roster-only.",
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.choices(mode=[
        app_commands.Choice(name="Open - anyone can connect", value="open"),
        app_commands.Choice(name="Private - only roster members (+ explicit allows) can connect", value="private"),
    ])
    @is_host()
    async def drill_vc_mode(self, interaction: Interaction, mode: str):
        drill = await require_active_drill(interaction)
        if drill is None:
            return

        d = drill_as_dict(drill)

        cursor.execute("UPDATE drills SET vc_mode = ? WHERE id = ?", (mode, d["id"]))
        conn.commit()

        await sync_drill_vc_permissions(interaction.guild, d["id"])
        await interaction.response.send_message(f"Drill #{d['id']}'s voice channel is now **{mode}** ✅", ephemeral=True)


    @app_commands.command(
        name="drill_vc_lock",
        description="Stop new people from joining a drill's voice channel, without kicking anyone already in it.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_host()
    async def drill_vc_lock(self, interaction: Interaction):
        drill = await require_active_drill(interaction)
        if drill is None:
            return

        d = drill_as_dict(drill)

        cursor.execute("UPDATE drills SET vc_locked = 1 WHERE id = ?", (d["id"],))
        conn.commit()

        await sync_drill_vc_permissions(interaction.guild, d["id"])
        await interaction.response.send_message(f"Drill #{d['id']}'s voice channel is now locked 🔒", ephemeral=True)


    @app_commands.command(
        name="drill_vc_unlock",
        description="Re-open a locked drill voice channel to new joins.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_host()
    async def drill_vc_unlock(self, interaction: Interaction):
        drill = await require_active_drill(interaction)
        if drill is None:
            return

        d = drill_as_dict(drill)

        cursor.execute("UPDATE drills SET vc_locked = 0 WHERE id = ?", (d["id"],))
        conn.commit()

        await sync_drill_vc_permissions(interaction.guild, d["id"])
        await interaction.response.send_message(f"Drill #{d['id']}'s voice channel is unlocked 🔓", ephemeral=True)


    @app_commands.command(
        name="drill_vc_block",
        description="Block a member or role from this drill's voice channel (and roster).",
    )
    @app_commands.guilds(GUILD_ID)
    @is_host()
    async def drill_vc_block(self, interaction: Interaction, target: discord.Member, reason: str = ""):
        drill = await require_active_drill(interaction)
        if drill is None:
            return

        d = drill_as_dict(drill)

        target_type = "role" if isinstance(target, discord.Role) else "user"
        cursor.execute("""
            INSERT INTO drill_vc_overrides (drill_id, target_type, target_id, permission, set_by)
            VALUES (?, ?, ?, 'blocked', ?)
            ON CONFLICT(drill_id, target_type, target_id) DO UPDATE SET
                permission = 'blocked', set_by = excluded.set_by, set_at = CURRENT_TIMESTAMP
        """, (d["id"], target_type, target.id, interaction.user.id))
        conn.commit()

        await sync_drill_vc_permissions(interaction.guild, d["id"])

        if d["vc_channel_id"]:
            if target_type == "user":
                member_ids = {target.id}
            else:
                member_ids = {member.id for member in target.members}
            await disconnect_members(interaction.guild, d["vc_channel_id"], member_ids)

        reason_note = f" ({reason})" if reason else ""
        await interaction.response.send_message(f"Blocked {target.mention} from drill #{d['id']}'s VC{reason_note} ✅", ephemeral=True)


    @app_commands.command(
        name="drill_vc_allow",
        description="Explicitly allow a member or role into this drill's voice channel.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_host()
    async def drill_vc_allow(self, interaction: Interaction, target: discord.Member):
        drill = await require_active_drill(interaction)
        if drill is None:
            return

        d = drill_as_dict(drill)

        target_type = "role" if isinstance(target, discord.Role) else "user"
        cursor.execute("""
            INSERT INTO drill_vc_overrides (drill_id, target_type, target_id, permission, set_by)
            VALUES (?, ?, ?, 'allowed', ?)
            ON CONFLICT(drill_id, target_type, target_id) DO UPDATE SET
                permission = 'allowed', set_by = excluded.set_by, set_at = CURRENT_TIMESTAMP
        """, (d["id"], target_type, target.id, interaction.user.id))
        conn.commit()

        await sync_drill_vc_permissions(interaction.guild, d["id"])
        await interaction.response.send_message(f"Allowed {target.mention} into drill #{d['id']}'s VC ✅", ephemeral=True)


    @app_commands.command(
        name="drill_vc_clear",
        description="Remove a block/allow override for a member or role on this drill.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_host()
    async def drill_vc_clear(self, interaction: Interaction, target: discord.Member):
        drill = await require_active_drill(interaction)
        if drill is None:
            return

        d = drill_as_dict(drill)

        target_type = "role" if isinstance(target, discord.Role) else "user"
        cursor.execute(
            "DELETE FROM drill_vc_overrides WHERE drill_id = ? AND target_type = ? AND target_id = ?",
            (d["id"], target_type, target.id)
        )
        conn.commit()

        await sync_drill_vc_permissions(interaction.guild, d["id"])
        await interaction.response.send_message(f"Cleared any override for {target.mention} on drill #{d['id']} ✅", ephemeral=True)


    @app_commands.command(
        name="drill_vc_permissions",
        description="Show the current VC access rules for a drill.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_host()
    async def drill_vc_permissions(self, interaction: Interaction):
        drill = await require_active_drill(interaction)
        if drill is None:
            return

        d = drill_as_dict(drill)

        cursor.execute(
            "SELECT target_type, target_id, permission FROM drill_vc_overrides WHERE drill_id = ? ORDER BY set_at",
            (d["id"],)
        )
        overrides = cursor.fetchall()

        cursor.execute("SELECT COUNT(*) FROM drill_banned_users")
        banned_count = cursor.fetchone()[0]

        lines = [
            f"**Mode:** {d['vc_mode']}",
            f"**Locked:** {'yes 🔒' if d['vc_locked'] else 'no'}",
        ]
        if overrides:
            lines.append("**Per-drill overrides:**")
            for target_type, target_id, permission in overrides:
                mention = f"<@&{target_id}>" if target_type == "role" else f"<@{target_id}>"
                lines.append(f"- {mention}: {permission}")
        else:
            lines.append("**Per-drill overrides:** none")
        lines.append(f"*({banned_count} member(s) division-wide banned from all drill VCs - see /drill_ban_list)*")

        await interaction.response.send_message(
            embed=Embed(title=f"Drill #{d['id']} VC Access - {d['drill_name']}", description="\n".join(lines)),
            ephemeral=True
        )


    ### DIVISION-WIDE DRILL BANS ###
    # Separate from the per-drill overrides above - staff-only, and applies
    # to every drill (current and future), not just one.

    @app_commands.command(
        name="drill_ban",
        description="Ban a member from every drill voice channel division-wide.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin_or_staff()
    async def drill_ban(self, interaction: Interaction, member: discord.Member, reason: str = ""):
        cursor.execute("""
            INSERT INTO drill_banned_users (discord_id, banned_by, reason) VALUES (?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET banned_by = excluded.banned_by, reason = excluded.reason, banned_at = CURRENT_TIMESTAMP
        """, (member.id, interaction.user.id, reason))
        conn.commit()

        # Take effect immediately on any drill currently underway, not just
        # future ones - sync every in-progress drill's VC and disconnect the
        # member from any of them they're currently sitting in.
        cursor.execute("SELECT id, vc_channel_id FROM drills WHERE status = 'in_progress' AND vc_channel_id IS NOT NULL")
        for drill_id, vc_channel_id in cursor.fetchall():
            await sync_drill_vc_permissions(interaction.guild, drill_id)
            await disconnect_members(interaction.guild, vc_channel_id, {member.id})

        reason_note = f" ({reason})" if reason else ""
        await interaction.response.send_message(f"{member.mention} is now banned from all drill voice channels{reason_note} ✅", ephemeral=True)


    @app_commands.command(
        name="drill_unban",
        description="Remove a member's division-wide drill VC ban.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin_or_staff()
    async def drill_unban(self, interaction: Interaction, member: discord.Member):
        cursor.execute("SELECT 1 FROM drill_banned_users WHERE discord_id = ?", (member.id,))
        if not cursor.fetchone():
            await interaction.response.send_message(f"{member.mention} isn't drill-banned.", ephemeral=True)
            return

        cursor.execute("DELETE FROM drill_banned_users WHERE discord_id = ?", (member.id,))
        conn.commit()

        cursor.execute("SELECT id FROM drills WHERE status = 'in_progress' AND vc_channel_id IS NOT NULL")
        for (drill_id,) in cursor.fetchall():
            await sync_drill_vc_permissions(interaction.guild, drill_id)

        await interaction.response.send_message(f"{member.mention}'s drill VC ban has been lifted ✅", ephemeral=True)


    @app_commands.command(
        name="drill_ban_list",
        description="List everyone currently banned from drill voice channels.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_admin_or_staff()
    async def drill_ban_list(self, interaction: Interaction):
        cursor.execute("SELECT discord_id, reason, banned_by, banned_at FROM drill_banned_users ORDER BY banned_at DESC")
        rows = cursor.fetchall()

        if not rows:
            await interaction.response.send_message("No one is currently drill-banned.", ephemeral=True)
            return

        lines = [
            f"<@{discord_id}> - {reason or 'no reason given'} (by <@{banned_by}>, {banned_at})"
            for discord_id, reason, banned_by, banned_at in rows
        ]
        await interaction.response.send_message(
            embed=Embed(title="Drill VC Bans", description="\n".join(lines)), ephemeral=True
        )


    ### PERSONAL HOST DEFAULTS ###
    # A host's own standing block/allow list, copied into every new drill
    # they create (see /drill_create above) - so a repeat host doesn't have
    # to re-run /drill_vc_block on the same person every time. Any division
    # member can maintain their own list, since anyone can host a drill.

    @app_commands.command(
        name="drill_default_block",
        description="Add a member or role to your standing block list, applied automatically to every drill you create.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_host()
    async def drill_default_block(self, interaction: Interaction, target: discord.Member, reason: str = ""):
        target_type = "role" if isinstance(target, discord.Role) else "user"
        cursor.execute("""
            INSERT INTO drill_host_defaults (host_discord_id, target_type, target_id, permission) VALUES (?, ?, ?, 'blocked')
            ON CONFLICT(host_discord_id, target_type, target_id) DO UPDATE SET permission = 'blocked', set_at = CURRENT_TIMESTAMP
        """, (interaction.user.id, target_type, target.id))
        conn.commit()

        reason_note = f" ({reason})" if reason else ""
        await interaction.response.send_message(
            f"{target.mention} will now be blocked from every drill you create{reason_note} ✅\n"
            f"-# This doesn't affect drills you've already created - use /drill_vc_block for those.",
            ephemeral=True
        )


    @app_commands.command(
        name="drill_default_allow",
        description="Add a member or role to your standing allow list, applied automatically to every drill you create.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_host()
    async def drill_default_allow(self, interaction: Interaction, target: discord.Member):
        target_type = "role" if isinstance(target, discord.Role) else "user"
        cursor.execute("""
            INSERT INTO drill_host_defaults (host_discord_id, target_type, target_id, permission) VALUES (?, ?, ?, 'allowed')
            ON CONFLICT(host_discord_id, target_type, target_id) DO UPDATE SET permission = 'allowed', set_at = CURRENT_TIMESTAMP
        """, (interaction.user.id, target_type, target.id))
        conn.commit()

        await interaction.response.send_message(
            f"{target.mention} will now be allowed into every drill you create (useful for private-mode drills) ✅\n"
            f"-# This doesn't affect drills you've already created - use /drill_vc_allow for those.",
            ephemeral=True
        )


    @app_commands.command(
        name="drill_default_clear",
        description="Remove a member or role from your standing block/allow list.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_host()
    async def drill_default_clear(self, interaction: Interaction, target: Union[discord.Member, discord.Role]):
        target_type = "role" if isinstance(target, discord.Role) else "user"
        cursor.execute(
            "DELETE FROM drill_host_defaults WHERE host_discord_id = ? AND target_type = ? AND target_id = ?",
            (interaction.user.id, target_type, target.id)
        )
        conn.commit()
        await interaction.response.send_message(f"Removed {target.mention} from your standing drill list ✅", ephemeral=True)


    @app_commands.command(
        name="drill_default_list",
        description="Show your own standing block/allow list for drills you host.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_host()
    async def drill_default_list(self, interaction: Interaction):
        cursor.execute(
            "SELECT target_type, target_id, permission FROM drill_host_defaults WHERE host_discord_id = ? ORDER BY set_at",
            (interaction.user.id,)
        )
        rows = cursor.fetchall()

        if not rows:
            await interaction.response.send_message("You don't have any standing block/allow entries.", ephemeral=True)
            return

        lines = []
        for target_type, target_id, permission in rows:
            mention = f"<@&{target_id}>" if target_type == "role" else f"<@{target_id}>"
            lines.append(f"- {mention}: {permission}")

        await interaction.response.send_message(
            embed=Embed(title="Your Standing Drill List", description="\n".join(lines)), ephemeral=True
        )


    ### STAFF OVERRIDES ###
    # Everything above (drill_start/end/cancel, roster join/leave, etc.)
    # is host-only and always acts on the caller's own active drill (see
    # require_active_drill in bot/lookups.py) - a host can only ever have
    # one, so there's nothing to disambiguate. Everything below is
    # staff/admin only (is_admin_or_staff, no host exception) and takes an
    # explicit drill_number, since staff may need to reach a drill that
    # isn't theirs, isn't the most recent, or has already ended - either to
    # force the record straight after something's gone wrong (bot crashed
    # mid-command, a host fat-fingered a field, someone needs adding/
    # removing by hand), or to manage a drill on a host's behalf.

    @app_commands.command(
        name="drill_force_status",
        description="[Staff] Force a drill's status directly, bypassing normal lifecycle checks.",
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.choices(status=[
        app_commands.Choice(name=label, value=value)
        for value, label in DRILL_STATUS_LABELS.items()
    ])
    @app_commands.describe(
        status="The status to force the drill into.",
        drill_number="1-indexed, most recent first. 0 (default) = most recent drill.",
    )
    @is_admin_or_staff()
    async def drill_force_status(self, interaction: Interaction, status: str, drill_number: int = 0):
        drill = await require_drill(interaction, drill_number)
        if drill is None:
            return

        d = drill_as_dict(drill)

        cursor.execute("UPDATE drills SET status = ? WHERE id = ?", (status, d["id"]))
        conn.commit()

        await refresh_drill_message(d["id"])
        await interaction.response.send_message(
            f"Drill #{d['id']} force-set to **{DRILL_STATUS_LABELS.get(status, status)}** ✅\n"
            f"-# This only changes the status field - it doesn't touch the voice channel, so "
            f"clean that up separately (e.g. /drill_cancel or /drill_end) if the drill's actually over.",
            ephemeral=True
        )


    @app_commands.command(
        name="drill_edit",
        description="[Staff] Edit a drill's name, objective, size, or roster cap after the fact.",
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.choices(
        size=[app_commands.Choice(name=size_name.capitalize(), value=size_name) for size_name in DRILL_SIZES]
    )
    @app_commands.describe(
        name="New name. Leave blank to keep the current one.",
        objective="New objective. Leave blank to keep the current one.",
        size="New size tier. Leave blank to keep the current one - doesn't change max_participants by itself.",
        max_participants="New roster cap. Leave blank to keep the current one; 0 = uncapped.",
        drill_number="1-indexed, most recent first. 0 (default) = most recent drill.",
    )
    @is_admin_or_staff()
    async def drill_edit(
        self,
        interaction: Interaction,
        name: Optional[str] = None,
        objective: Optional[str] = None,
        size: Optional[str] = None,
        max_participants: Optional[int] = None,
        drill_number: int = 0,
    ):
        if name is None and objective is None and size is None and max_participants is None:
            await interaction.response.send_message("Provide at least one field to change.", ephemeral=True)
            return

        if max_participants is not None and max_participants < 0:
            await interaction.response.send_message("max_participants must be 0 or greater.", ephemeral=True)
            return

        drill = await require_drill(interaction, drill_number)
        if drill is None:
            return

        d = drill_as_dict(drill)

        updates = {}
        if name is not None:
            updates["drill_name"] = name
        if objective is not None:
            updates["objective"] = objective
        if size is not None:
            updates["drill_size"] = size
        if max_participants is not None:
            updates["max_participants"] = max_participants

        set_clause = ", ".join(f"{column} = ?" for column in updates)
        cursor.execute(f"UPDATE drills SET {set_clause} WHERE id = ?", (*updates.values(), d["id"]))
        conn.commit()

        await refresh_drill_message(d["id"])
        changed = ", ".join(updates)
        await interaction.response.send_message(f"Drill #{d['id']} updated ({changed}) ✅", ephemeral=True)


    @app_commands.command(
        name="drill_reassign_host",
        description="[Staff] Change a drill's host (e.g. the original host left or was picked by mistake).",
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.describe(drill_number="1-indexed, most recent first. 0 (default) = most recent drill.")
    @is_admin_or_staff()
    async def drill_reassign_host(self, interaction: Interaction, new_host: discord.Member, drill_number: int = 0):
        drill = await require_drill(interaction, drill_number)
        if drill is None:
            return

        d = drill_as_dict(drill)

        cursor.execute("UPDATE drills SET host_discord_id = ? WHERE id = ?", (new_host.id, d["id"]))
        conn.commit()

        # The host gets a standing connect/mute/move overwrite on the VC
        # (see sync_drill_vc_permissions in bot/drills.py) - re-syncing moves
        # that overwrite from the old host to the new one immediately.
        await sync_drill_vc_permissions(interaction.guild, d["id"])
        await refresh_drill_message(d["id"])

        await interaction.response.send_message(
            f"Drill #{d['id']}'s host is now {new_host.mention} ✅", ephemeral=True
        )


    @app_commands.command(
        name="drill_kick",
        description="[Staff] Force-remove a member from a drill's roster.",
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.describe(
        member="Who to remove from the roster.",
        drill_number="1-indexed, most recent first. 0 (default) = most recent drill.",
    )
    @is_admin_or_staff()
    async def drill_kick(self, interaction: Interaction, member: discord.Member, reason: str = "", drill_number: int = 0):
        drill = await require_drill(interaction, drill_number)
        if drill is None:
            return

        d = drill_as_dict(drill)

        cursor.execute("SELECT id FROM players WHERE discord_id = ?", (member.id,))
        player_row = cursor.fetchone()

        cursor.execute(
            "SELECT left_at FROM drill_participants WHERE drill_id = ? AND player_id = ?",
            (d["id"], player_row[0] if player_row else -1)
        )
        existing = cursor.fetchone()
        if not player_row or not existing or existing[0] is not None:
            await interaction.response.send_message(f"{member.mention} isn't on drill #{d['id']}'s roster.", ephemeral=True)
            return

        cursor.execute(
            "UPDATE drill_participants SET left_at = CURRENT_TIMESTAMP WHERE drill_id = ? AND player_id = ?",
            (d["id"], player_row[0])
        )

        # Same "ready" -> "recruiting" drop-back as the Leave button (see
        # DrillRosterView.leave in bot/ui.py) - a spot opening up should
        # reopen recruiting either way, regardless of who freed it. The
        # count below already reflects the removal above, since it runs on
        # the same uncommitted transaction.
        if d["status"] == "ready" and (not d["max_participants"] or get_active_participant_count(d["id"]) < d["max_participants"]):
            cursor.execute("UPDATE drills SET status = 'recruiting' WHERE id = ?", (d["id"],))
        conn.commit()

        if d["vc_channel_id"]:
            await disconnect_members(interaction.guild, d["vc_channel_id"], {member.id})

        await refresh_drill_message(d["id"])

        reason_note = f" ({reason})" if reason else ""
        await interaction.response.send_message(
            f"Removed {member.mention} from drill #{d['id']}'s roster{reason_note} ✅", ephemeral=True
        )


    @app_commands.command(
        name="drill_force_join",
        description="[Staff] Manually add a member to a drill's roster, bypassing the roster cap and any blocks.",
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.describe(
        member="Who to add to the roster.",
        drill_number="1-indexed, most recent first. 0 (default) = most recent drill.",
    )
    @is_admin_or_staff()
    async def drill_force_join(self, interaction: Interaction, member: discord.Member, drill_number: int = 0):
        drill = await require_drill(interaction, drill_number)
        if drill is None:
            return

        d = drill_as_dict(drill)

        player_id = get_or_create_player_id(member.id)

        cursor.execute(
            "SELECT left_at FROM drill_participants WHERE drill_id = ? AND player_id = ?",
            (d["id"], player_id)
        )
        existing = cursor.fetchone()
        if existing and existing[0] is None:
            await interaction.response.send_message(f"{member.mention} is already on drill #{d['id']}'s roster.", ephemeral=True)
            return

        cursor.execute("""
            INSERT INTO drill_participants (drill_id, player_id) VALUES (?, ?)
            ON CONFLICT(drill_id, player_id) DO UPDATE SET left_at = NULL, joined_at = CURRENT_TIMESTAMP
        """, (d["id"], player_id))
        conn.commit()

        if d["status"] == "recruiting" and d["max_participants"] and get_active_participant_count(d["id"]) >= d["max_participants"]:
            cursor.execute("UPDATE drills SET status = 'ready' WHERE id = ?", (d["id"],))
            conn.commit()

        await refresh_drill_message(d["id"])
        await interaction.response.send_message(
            f"Added {member.mention} to drill #{d['id']}'s roster ✅\n"
            f"-# This bypasses the roster cap and any block/ban, so double check they're actually meant to be in.",
            ephemeral=True
        )


    @app_commands.command(
        name="drill_relink_message",
        description="[Staff] Re-post a drill's roster message if the original one was deleted.",
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.describe(drill_number="1-indexed, most recent first. 0 (default) = most recent drill.")
    @is_admin_or_staff()
    async def drill_relink_message(self, interaction: Interaction, drill_number: int = 0):
        drill = await require_drill(interaction, drill_number)
        if drill is None:
            return

        d = drill_as_dict(drill)

        if DRILLS_CHANNEL_ID is None:
            await interaction.response.send_message(
                "Drills channel not configured - add drill_data.drills_channel_id to bot_data.json.", ephemeral=True
            )
            return

        drills_channel = bot.get_channel(DRILLS_CHANNEL_ID)
        if drills_channel is None:
            await interaction.response.send_message(
                "Drills channel not found - check drill_data.drills_channel_id.", ephemeral=True
            )
            return

        participant_count = get_active_participant_count(d["id"])
        embed = build_drill_embed(drill, participant_count)
        view = DrillRosterView(d["id"]) if d["status"] in OPEN_STATUSES else None

        try:
            message = await drills_channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to post in the drills channel.", ephemeral=True)
            return

        cursor.execute(
            "UPDATE drills SET roster_message_id = ?, roster_channel_id = ? WHERE id = ?",
            (message.id, message.channel.id, d["id"])
        )
        conn.commit()

        await interaction.response.send_message(f"Drill #{d['id']}'s roster message has been re-posted ✅", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(DrillsCog(bot))