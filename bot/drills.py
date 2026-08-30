### GUARDSMAN DRILLS - RENDERING & STATE HELPERS ###
# Shared building blocks for the Drill system. Slash commands live in
# bot/cogs/drills.py; the Join/Leave/View Roster buttons live in
# bot/ui.py's DrillRosterView. This module is the thing both of those
# import from, the same role bot/leaderboard.py plays for events/stats.

import re
from typing import Optional

import discord
from discord import Embed

from bot.client import bot
from bot.config import DRILL_LOG_CHANNEL_ID, GUILD_ID
from bot.database import conn, cursor

# drills table column order, for readable tuple-unpacking below - keep this
# in sync with the CREATE TABLE (+ ensure_column calls) in bot/database.py.
# vc_mode/vc_locked/started_at/proof_channel_id/proof_message_id were all
# added via ensure_column, which always appends new columns at the end -
# hence they're last here too, in the order they were added, not grouped
# with the other columns they're thematically related to.
DRILL_COLUMNS = (
    "id", "season_id", "host_discord_id", "drill_name", "drill_size", "objective",
    "max_participants", "status", "created_at", "start_time", "ended_at",
    "vc_channel_id", "roster_message_id", "roster_channel_id",
    "vc_mode", "vc_locked", "started_at", "proof_channel_id", "proof_message_id",
    "stale_warned", "log_channel_id", "log_message_id",
)

DRILL_STATUS_LABELS = {
    "recruiting": "🟢 RECRUITING",
    "ready": "🔵 READY",
    "in_progress": "🟡 IN PROGRESS",
    "completed": "⚪ COMPLETED",
    "cancelled": "🔴 CANCELLED",
}

# Statuses for which the roster is still open - used both to decide whether
# to attach a live DrillRosterView to the posted message, and by the buttons
# themselves to reject a join/leave on a drill that's moved on.
OPEN_STATUSES = ("recruiting", "ready")

# Matches a Discord "Copy Message Link" URL, e.g.
# https://discord.com/channels/111/222/333 (also canary/ptb, and the legacy
# discordapp.com domain) - captures (guild_id, channel_id, message_id).
MESSAGE_LINK_RE = re.compile(
    r"(?:https?://)?(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
)


def drill_as_dict(drill: tuple) -> dict:
    """Turns a raw `SELECT * FROM drills` row into a name-keyed dict, so
    call sites don't need to remember column positions."""
    return dict(zip(DRILL_COLUMNS, drill))


def get_or_create_player_id(discord_id: int) -> int:
    """Returns the player row id for discord_id, creating one on the fly if
    it doesn't exist yet - a drill roster doesn't require a linked Roblox
    account. Mirrors DrillRosterView._get_or_create_player_id in bot/ui.py
    (that one can't import this one without a circular import, since this
    module is what bot/ui.py itself imports from), kept here too so staff
    override commands like /drill_force_join (bot/cogs/drills.py) can reuse
    the same logic instead of re-deriving it."""
    cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
    row = cursor.fetchone()
    if row:
        return row[0]
    cursor.execute("INSERT INTO players (discord_id) VALUES (?)", (discord_id,))
    conn.commit()
    return cursor.lastrowid


def parse_message_link(link: str) -> Optional[tuple[int, int, int]]:
    """Pulls (guild_id, channel_id, message_id) out of a pasted "Copy Message
    Link" URL, or None if the text doesn't look like one. Used by /drill_end
    to turn the proof link a host pastes in into something fetchable - see
    the comment above /drill_end in bot/cogs/drills.py."""
    match = MESSAGE_LINK_RE.search(link.strip())
    if not match:
        return None
    guild_id, channel_id, message_id = (int(g) for g in match.groups())
    return guild_id, channel_id, message_id


def find_drill_using_proof(proof_channel_id: int, proof_message_id: int, exclude_drill_id: int = 0) -> Optional[int]:
    """Whether some OTHER drill has already been completed with this exact
    proof message - stops the same screenshot being reused to log two
    different drills as complete. Returns that drill's id, or None if the
    message is unused."""
    cursor.execute(
        "SELECT id FROM drills WHERE proof_channel_id = ? AND proof_message_id = ? AND id != ?",
        (proof_channel_id, proof_message_id, exclude_drill_id)
    )
    row = cursor.fetchone()
    return row[0] if row else None


def get_active_participant_count(drill_id: int) -> int:
    cursor.execute(
        "SELECT COUNT(*) FROM drill_participants WHERE drill_id = ? AND left_at IS NULL",
        (drill_id,)
    )
    return cursor.fetchone()[0]


def get_active_participant_discord_ids(drill_id: int) -> list[int]:
    cursor.execute("""
        SELECT players.discord_id FROM drill_participants
        JOIN players ON players.id = drill_participants.player_id
        WHERE drill_participants.drill_id = ? AND drill_participants.left_at IS NULL
        ORDER BY drill_participants.joined_at
    """, (drill_id,))
    return [row[0] for row in cursor.fetchall()]


def get_host_stats(host_discord_id: int) -> dict:
    """Aggregates a host's hosting record across every drill they've ever
    created - drills hosted, how many finished (completed vs cancelled), and
    total participants mobilized, all pulled live from drills/drill_participants
    rather than a stored/cached figure. Used by /guardsman_profile's Drill
    Hosting section (see build_profile_embed in bot/cogs/stats.py) and by
    the drills_hosted/drills_completed/participants_mobilized leaderboard
    categories (bot/leaderboard.py, config.DRILL_LEADERBOARD_TYPES).

    completion_rate is out of DECIDED drills (completed + cancelled) rather
    than every drill ever created, so a host with several drills still
    recruiting/in_progress isn't penalized for outcomes that haven't
    happened yet - it's None (not 0) when there's nothing decided yet, so
    callers can render "-" instead of a misleading 0%.
    """
    cursor.execute(
        "SELECT status, COUNT(*) FROM drills WHERE host_discord_id = ? GROUP BY status",
        (host_discord_id,)
    )
    status_counts = dict(cursor.fetchall())
    hosted = sum(status_counts.values())
    completed = status_counts.get("completed", 0)
    cancelled = status_counts.get("cancelled", 0)
    decided = completed + cancelled

    cursor.execute(
        "SELECT COUNT(*) FROM drill_participants dp JOIN drills d ON d.id = dp.drill_id WHERE d.host_discord_id = ?",
        (host_discord_id,)
    )
    participants_mobilized = cursor.fetchone()[0]

    return {
        "hosted": hosted,
        "completed": completed,
        "cancelled": cancelled,
        "participants_mobilized": participants_mobilized,
        "average_participation": round(participants_mobilized / hosted, 1) if hosted else 0.0,
        "completion_rate": round(completed / decided * 100) if decided else None,
    }


def build_drill_embed(drill: tuple, participant_count: int) -> Embed:
    d = drill_as_dict(drill)

    roster_text = (
        f"{participant_count} / {d['max_participants']}"
        if d["max_participants"] else str(participant_count)
    )

    lines = [
        f"**Type:** {d['drill_size'].capitalize()} Drill",
        f"**Objective:** {d['objective']}",
        f"**Roster:** {roster_text}",
        f"**Host:** <@{d['host_discord_id']}>",
    ]
    if d["start_time"]:
        lines.append(f"**Scheduled:** {d['start_time']}")
    if d["vc_channel_id"]:
        lines.append(f"**Voice Channel:** <#{d['vc_channel_id']}>")
    lines.append(f"**Status:** {DRILL_STATUS_LABELS.get(d['status'], d['status'])}")
    if d["proof_channel_id"] and d["proof_message_id"]:
        proof_url = f"https://discord.com/channels/{GUILD_ID}/{d['proof_channel_id']}/{d['proof_message_id']}"
        lines.append(f"**Proof:** [jump to message]({proof_url})")
    if d["status"] in OPEN_STATUSES and d["stale_warned"]:
        lines.append(
            "⚠️ *This drill hasn't been started yet and will be auto-cancelled if it stays inactive "
            "much longer - run /drill_start or /drill_cancel.*"
        )

    return Embed(
        title=f"🛡️ Guardsman Drill #{d['id']} - {d['drill_name']}",
        description="\n".join(lines)
    )


async def refresh_drill_message(drill_id: int):
    """Re-renders and edits the roster message in place after any state
    change (join, leave, start, end, cancel) so the posted embed always
    reflects the live roster/status without spamming a new message each
    time - same idea as post_or_update_leaderboard_message() in
    bot/leaderboard.py, but always edits rather than falling back to a
    fresh post, since a drill always has exactly one roster message."""
    cursor.execute("SELECT * FROM drills WHERE id = ?", (drill_id,))
    drill = cursor.fetchone()
    if not drill:
        return

    d = drill_as_dict(drill)
    if not (d["roster_channel_id"] and d["roster_message_id"]):
        return

    try:
        channel = bot.get_channel(d["roster_channel_id"]) or await bot.fetch_channel(d["roster_channel_id"])
        message = await channel.fetch_message(d["roster_message_id"])
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        print(f"refresh_drill_message: could not fetch drill {drill_id}'s roster message: {e}")
        return

    participant_count = get_active_participant_count(drill_id)
    embed = build_drill_embed(drill, participant_count)

    # Deferred import: bot/ui.py imports helpers from this module at the top
    # level, so importing DrillRosterView from bot.ui up there would be a
    # circular import. Importing it here, only when actually needed, avoids
    # that without either module having to give up its natural home.
    from bot.ui import DrillRosterView
    view = DrillRosterView(drill_id) if d["status"] in OPEN_STATUSES else None

    try:
        await message.edit(embed=embed, view=view)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        print(f"refresh_drill_message: could not edit drill {drill_id}'s roster message: {e}")


async def post_drill_completion_log(guild: discord.Guild, drill_id: int, participant_count: int, completed_count: int):
    """Posts a bot-authored summary of a just-completed drill to
    bot.config.DRILL_LOG_CHANNEL_ID (host, roster size, completed/failed
    counts, a link to the proof message) and saves the resulting message's
    channel_id/message_id on the drill row - same "store a pointer, not a
    copy" pattern as roster_message_id/proof_message_id above. No-ops
    silently if that channel isn't configured, same as
    DRILL_VC_CATEGORY_ID/DRILL_PROOF_CHANNEL_ID being optional elsewhere.

    Called once, from /drill_end (bot/cogs/drills.py) right after a drill is
    marked completed. Deliberately doesn't get called again later if the
    drill's record changes (e.g. a staff /drill_force_status correction) -
    this is meant to be an immutable log of what was recorded at completion
    time, not a live mirror of the row.
    """
    if not DRILL_LOG_CHANNEL_ID:
        return

    cursor.execute("SELECT * FROM drills WHERE id = ?", (drill_id,))
    drill = cursor.fetchone()
    if not drill:
        return
    d = drill_as_dict(drill)

    try:
        channel = bot.get_channel(DRILL_LOG_CHANNEL_ID) or await bot.fetch_channel(DRILL_LOG_CHANNEL_ID)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        print(f"post_drill_completion_log: could not fetch log channel: {e}")
        return

    failed_count = participant_count - completed_count
    lines = [
        f"**Host:** <@{d['host_discord_id']}>",
        f"**Size:** {d['drill_size'].capitalize()}",
        f"**Roster:** {participant_count}",
        f"**Completed:** {completed_count}",
        f"**Failed:** {failed_count}",
    ]
    if d["proof_channel_id"] and d["proof_message_id"]:
        proof_url = f"https://discord.com/channels/{GUILD_ID}/{d['proof_channel_id']}/{d['proof_message_id']}"
        lines.append(f"**Proof:** [jump to message]({proof_url})")

    embed = Embed(title=f"✅ Drill #{d['id']} Completed - {d['drill_name']}", description="\n".join(lines))

    try:
        message = await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"post_drill_completion_log: could not post to log channel: {e}")
        return

    cursor.execute(
        "UPDATE drills SET log_channel_id = ?, log_message_id = ? WHERE id = ?",
        (message.channel.id, message.id, drill_id)
    )
    conn.commit()


def is_user_blocked_from_drill(drill_id: int, member: discord.Member) -> bool:
    """Used by DrillRosterView.join() to keep a banned/blocked member from
    even registering on the roster, not just from connecting to the VC -
    someone who can't get into the drill shouldn't show up in its
    participant list either. Checks the same two sources
    sync_drill_vc_permissions() does, in the same priority order (a global
    ban is checked first and short-circuits the per-drill check)."""
    cursor.execute("SELECT 1 FROM drill_banned_users WHERE discord_id = ?", (member.id,))
    if cursor.fetchone():
        return True

    role_ids = {role.id for role in member.roles}
    cursor.execute(
        "SELECT target_type, target_id, permission FROM drill_vc_overrides WHERE drill_id = ?",
        (drill_id,)
    )
    for target_type, target_id, permission in cursor.fetchall():
        if permission != "blocked":
            continue
        if target_type == "user" and target_id == member.id:
            return True
        if target_type == "role" and target_id in role_ids:
            return True
    return False


async def sync_drill_vc_permissions(guild: discord.Guild, drill_id: int):
    """Recomputes and applies the full set of voice permission overwrites for
    a drill's VC from scratch. Layers are applied in this order - for any
    given member/role that more than one layer targets, the later layer
    wins (earlier layers can still stand for everyone else that layer
    covers, so a host being exempted from a lock doesn't remove the lock for
    anyone else):

      1. Base: @everyone denied connect, if the drill is 'private' or locked -
         otherwise @everyone is left alone (inherits from the category).
      2. Roster allow: if 'private' and NOT locked, every currently-active
         participant is explicitly allowed - this is what makes "private"
         mean "roster only" rather than "nobody". Locking a private drill
         drops this layer entirely, so locking always means "no new joins",
         even for roster members who haven't connected yet.
      3. Host permissions: the drill's host always gets connect + the power
         to mute/deafen/move members, scoped to just this one VC via a
         channel-specific overwrite (not a server-wide role) - so a host
         can moderate their own drill without needing a standing staff role,
         and can't touch any other channel with it. This also means a lock
         or a private mode never accidentally locks the host out of their
         own drill.
      4. Per-drill overrides (drill_vc_overrides) - a host/staff explicitly
         blocking or allowing a specific member or role for this drill.
         Wins over all three layers above - e.g. to admit one guest into an
         otherwise-private drill, keep one specific roster member out of an
         otherwise-open one, or have staff strip a host's own access for
         cause.
      5. Global bans (drill_banned_users) - always denied, regardless of
         anything above, including the host layer - the one thing nobody
         can override for their own drill.

    Recomputing from scratch (rather than patching individual overwrites) on
    every change keeps this the single source of truth - no risk of a stale
    overwrite lingering from an override that was later cleared.

    No-ops if the drill has no VC yet - overrides set before /drill_start
    just sit in the DB until the VC exists, then get applied the first time
    sync runs after creation.
    """
    cursor.execute("SELECT * FROM drills WHERE id = ?", (drill_id,))
    drill = cursor.fetchone()
    if not drill:
        return

    d = drill_as_dict(drill)
    if not d["vc_channel_id"]:
        return

    vc = guild.get_channel(d["vc_channel_id"])
    if vc is None:
        return

    overwrites: dict = {}

    if d["vc_mode"] == "private" or d["vc_locked"]:
        overwrites[guild.default_role] = discord.PermissionOverwrite(connect=False)

    if d["vc_mode"] == "private" and not d["vc_locked"]:
        for discord_id in get_active_participant_discord_ids(drill_id):
            member = guild.get_member(discord_id)
            if member:
                overwrites[member] = discord.PermissionOverwrite(connect=True)

    host_member = guild.get_member(d["host_discord_id"])
    if host_member:
        overwrites[host_member] = discord.PermissionOverwrite(
            connect=True,
            mute_members=True,
            deafen_members=True,
            move_members=True,
        )

    cursor.execute(
        "SELECT target_type, target_id, permission FROM drill_vc_overrides WHERE drill_id = ?",
        (drill_id,)
    )
    for target_type, target_id, permission in cursor.fetchall():
        target = guild.get_role(target_id) if target_type == "role" else guild.get_member(target_id)
        if target:
            overwrites[target] = discord.PermissionOverwrite(connect=(permission == "allowed"))

    cursor.execute("SELECT discord_id FROM drill_banned_users")
    for (banned_discord_id,) in cursor.fetchall():
        member = guild.get_member(banned_discord_id)
        if member:
            overwrites[member] = discord.PermissionOverwrite(connect=False)

    try:
        await vc.edit(overwrites=overwrites)
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"sync_drill_vc_permissions: could not update overwrites for drill {drill_id}: {e}")


async def disconnect_members(guild: discord.Guild, vc_channel_id: int, member_ids: set[int]):
    """Best-effort kick of anyone in member_ids who's currently sitting in
    vc_channel_id. A permission overwrite change alone only stops the NEXT
    join attempt - this is what makes a fresh block/ban take effect on
    someone already connected, right away instead of at their next reconnect."""
    if not vc_channel_id:
        return
    vc = guild.get_channel(vc_channel_id)
    if vc is None:
        return
    for member in list(vc.members):
        if member.id in member_ids:
            try:
                await member.move_to(None, reason="Removed from drill voice channel")
            except (discord.Forbidden, discord.HTTPException) as e:
                print(f"disconnect_members: could not disconnect {member.id}: {e}")