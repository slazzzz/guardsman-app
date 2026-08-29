### DRILL COMMANDS ###

from typing import Union

import discord
from discord import Embed, Interaction, app_commands
from discord.ext import commands

from bot.shared import *  # noqa: F401,F403


def _can_manage_drill(interaction: Interaction, host_discord_id: int) -> bool:
    """A drill can be managed by its host, or by anyone staff/admin/helper -
    same tiers as is_admin_or_staff_or_helper(), plus the host themselves.
    Not a decorator since drill_start/end/cancel need to know the specific
    drill's host before they can decide, which a decorator can't see."""
    if interaction.user.id == host_discord_id:
        return True
    if interaction.user.id in ADMIN_USERS:
        return True
    user_roles = [role.id for role in interaction.user.roles]
    return any(r in STAFF_ROLES for r in user_roles) or any(r in HELPER_ROLES for r in user_roles)


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
    @is_allowed()
    @cooldown(DRILL_CREATE_COOLDOWN_SECONDS)
    async def drill_create(self, interaction: Interaction, name: str, objective: str, size: str, max_participants: int = 0):
        if max_participants < 0:
            await interaction.response.send_message("max_participants must be greater than 0.", ephemeral=True)
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
    @is_allowed()
    async def drill_start(self, interaction: Interaction, drill_number: int = 0):
        drill = await require_drill(interaction, drill_number)
        if drill is None:
            return

        d = drill_as_dict(drill)

        if not _can_manage_drill(interaction, d["host_discord_id"]):
            await interaction.response.send_message("Only the host or staff can start this drill.", ephemeral=True)
            return

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

        cursor.execute("UPDATE drills SET status = 'in_progress', vc_channel_id = ? WHERE id = ?", (vc.id, d["id"]))
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
        description="End a drill and record how many participants completed the objective.",
    )
    @app_commands.guilds(GUILD_ID)
    @app_commands.describe(
        completed_count="How many participants completed the objective. Leave at -1 to count everyone as completed.",
    )
    @is_allowed()
    async def drill_end(self, interaction: Interaction, completed_count: int = -1, drill_number: int = 0):
        drill = await require_drill(interaction, drill_number)
        if drill is None:
            return

        d = drill_as_dict(drill)

        if not _can_manage_drill(interaction, d["host_discord_id"]):
            await interaction.response.send_message("Only the host or staff can end this drill.", ephemeral=True)
            return

        if d["status"] not in ("recruiting", "ready", "in_progress"):
            await interaction.response.send_message(f"This drill is already {d['status']}.", ephemeral=True)
            return

        participant_count = get_active_participant_count(d["id"])

        if completed_count < 0:
            completed_count = participant_count
        elif completed_count > participant_count:
            await interaction.response.send_message("completed_count can't be greater than the roster size.", ephemeral=True)
            return

        cursor.execute("UPDATE drills SET status = 'completed', ended_at = CURRENT_TIMESTAMP WHERE id = ?", (d["id"],))
        conn.commit()

        if d["vc_channel_id"]:
            vc = interaction.guild.get_channel(d["vc_channel_id"])
            if vc:
                try:
                    await vc.delete(reason=f"Drill #{d['id']} ended")
                except (discord.Forbidden, discord.HTTPException) as e:
                    print(f"Could not delete VC for drill {d['id']}: {e}")

        await refresh_drill_message(d["id"])

        failed_count = participant_count - completed_count
        await interaction.response.send_message(
            f"**Drill #{d['id']} complete - {d['drill_name']}**\n"
            f"Participants: {participant_count}\n"
            f"Completed: {completed_count}\n"
            f"Failed: {failed_count}\n\n"
            f"Results have been recorded ✅"
        )


    @app_commands.command(
        name="drill_cancel",
        description="Cancel a drill before it happens.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_allowed()
    async def drill_cancel(self, interaction: Interaction, drill_number: int = 0):
        drill = await require_drill(interaction, drill_number)
        if drill is None:
            return

        d = drill_as_dict(drill)

        if not _can_manage_drill(interaction, d["host_discord_id"]):
            await interaction.response.send_message("Only the host or staff can cancel this drill.", ephemeral=True)
            return

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
    @is_allowed()
    async def drill_vc_mode(self, interaction: Interaction, mode: str, drill_number: int = 0):
        drill = await require_drill(interaction, drill_number)
        if drill is None:
            return

        d = drill_as_dict(drill)
        if not _can_manage_drill(interaction, d["host_discord_id"]):
            await interaction.response.send_message("Only the host or staff can change this drill's VC mode.", ephemeral=True)
            return

        cursor.execute("UPDATE drills SET vc_mode = ? WHERE id = ?", (mode, d["id"]))
        conn.commit()

        await sync_drill_vc_permissions(interaction.guild, d["id"])
        await interaction.response.send_message(f"Drill #{d['id']}'s voice channel is now **{mode}** ✅", ephemeral=True)


    @app_commands.command(
        name="drill_vc_lock",
        description="Stop new people from joining a drill's voice channel, without kicking anyone already in it.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_allowed()
    async def drill_vc_lock(self, interaction: Interaction, drill_number: int = 0):
        drill = await require_drill(interaction, drill_number)
        if drill is None:
            return

        d = drill_as_dict(drill)
        if not _can_manage_drill(interaction, d["host_discord_id"]):
            await interaction.response.send_message("Only the host or staff can lock this drill's VC.", ephemeral=True)
            return

        cursor.execute("UPDATE drills SET vc_locked = 1 WHERE id = ?", (d["id"],))
        conn.commit()

        await sync_drill_vc_permissions(interaction.guild, d["id"])
        await interaction.response.send_message(f"Drill #{d['id']}'s voice channel is now locked 🔒", ephemeral=True)


    @app_commands.command(
        name="drill_vc_unlock",
        description="Re-open a locked drill voice channel to new joins.",
    )
    @app_commands.guilds(GUILD_ID)
    @is_allowed()
    async def drill_vc_unlock(self, interaction: Interaction, drill_number: int = 0):
        drill = await require_drill(interaction, drill_number)
        if drill is None:
            return

        d = drill_as_dict(drill)
        if not _can_manage_drill(interaction, d["host_discord_id"]):
            await interaction.response.send_message("Only the host or staff can unlock this drill's VC.", ephemeral=True)
            return

        cursor.execute("UPDATE drills SET vc_locked = 0 WHERE id = ?", (d["id"],))
        conn.commit()

        await sync_drill_vc_permissions(interaction.guild, d["id"])
        await interaction.response.send_message(f"Drill #{d['id']}'s voice channel is unlocked 🔓", ephemeral=True)


    @app_commands.command(
        name="drill_vc_block",
        description="Block a member or role from this drill's voice channel (and roster).",
    )
    @app_commands.guilds(GUILD_ID)
    @is_allowed()
    async def drill_vc_block(self, interaction: Interaction, target: discord.Member, reason: str = "", drill_number: int = 0):
        drill = await require_drill(interaction, drill_number)
        if drill is None:
            return

        d = drill_as_dict(drill)
        if not _can_manage_drill(interaction, d["host_discord_id"]):
            await interaction.response.send_message("Only the host or staff can manage this drill's VC access.", ephemeral=True)
            return

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
    @is_allowed()
    async def drill_vc_allow(self, interaction: Interaction, target: discord.Member, drill_number: int = 0):
        drill = await require_drill(interaction, drill_number)
        if drill is None:
            return

        d = drill_as_dict(drill)
        if not _can_manage_drill(interaction, d["host_discord_id"]):
            await interaction.response.send_message("Only the host or staff can manage this drill's VC access.", ephemeral=True)
            return

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
    @is_allowed()
    async def drill_vc_clear(self, interaction: Interaction, target: discord.Member, drill_number: int = 0):
        drill = await require_drill(interaction, drill_number)
        if drill is None:
            return

        d = drill_as_dict(drill)
        if not _can_manage_drill(interaction, d["host_discord_id"]):
            await interaction.response.send_message("Only the host or staff can manage this drill's VC access.", ephemeral=True)
            return

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
    @is_allowed()
    async def drill_vc_permissions(self, interaction: Interaction, drill_number: int = 0):
        drill = await require_drill(interaction, drill_number)
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
    @is_allowed()
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
    @is_allowed()
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
    @is_allowed()
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
    @is_allowed()
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


async def setup(bot: commands.Bot):
    await bot.add_cog(DrillsCog(bot))