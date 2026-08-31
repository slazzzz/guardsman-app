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
from bot.config import DRILL_LOG_CHANNEL_ID, EXPENDABLE_ROLE_ID, GUILD_ID, UNDER_REVIEW_ROLE_ID
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
    account. Shared by DrillRosterView.join (bot/ui.py), /drill_force_join
    and drill_create's host auto-enroll (both bot/cogs/drills.py, via
    add_drill_participant below), so none of them re-derive the same
    upsert-or-create logic independently."""
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


def add_drill_participant(drill_id: int, discord_id: int) -> None:
    """Enrolls discord_id as an active participant of drill_id. Upserts
    rather than a plain INSERT so this is safe to call even if the person
    (e.g. a host who already clicked Join Drill themselves) is somehow
    already in the roster, or previously left - mirrors the join button's
    own upsert in DrillRosterView.join."""
    player_id = get_or_create_player_id(discord_id)
    cursor.execute("""
        INSERT INTO drill_participants (drill_id, player_id) VALUES (?, ?)
        ON CONFLICT(drill_id, player_id) DO UPDATE SET left_at = NULL, joined_at = CURRENT_TIMESTAMP
    """, (drill_id, player_id))
    conn.commit()


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
    given member/role that more than one layer targets, later layers only
    override the SPECIFIC permission flags they set (via the ow() helper
    below), not the whole overwrite - so e.g. the host's view_channel grant
    doesn't accidentally erase their connect grant, and a per-drill override
    on a role doesn't erase that role's other denied permissions unless it
    explicitly sets them too:

      0. Access-tier roles: UNDER_REVIEW_ROLE_ID (if configured) is denied
         every relevant permission on every drill VC, no exceptions -
         mirrors the server-wide convention of locking this role out of
         voice entirely. EXPENDABLE_ROLE_ID (if configured) is denied just
         view_channel, so members with it can't see a drill VC exists
         unless they're the host or an active participant (layers 2b/3
         grant those two an explicit view_channel=True, which - being
         member-specific - wins over this role-level deny). This layer is
         unconditional (applies regardless of the drill's public/private
         mode) since it's a standing access-tier rule, not a per-drill
         setting. NOTE: this layer exists because vc.edit(overwrites=...)
         below replaces the channel's ENTIRE overwrite set every time this
         function runs - anything inherited from the category (like a
         server-wide UNDER REVIEW deny-all) would otherwise get silently
         wiped the moment a drill VC is first synced.
      1. Base: @everyone is denied view_channel unconditionally (drill VCs
         are never visible by default - only the host, active
         participants, and anyone granted explicit access via layer 4
         should ever see one), plus denied connect on top of that if the
         drill is 'private' or locked.
      2. Roster allow: if 'private' and NOT locked, every currently-active
         participant is explicitly allowed to connect - this is what makes
         "private" mean "roster only" rather than "nobody". Locking a
         private drill drops the connect grant entirely, so locking always
         means "no new joins", even for roster members who haven't
         connected yet.
      2b. Roster visibility: regardless of vc_mode/lock, every currently-
         active participant also gets view_channel=True, so an Expendable
         participant can still see and join their own drill's VC.
      3. Host permissions: the drill's host always gets view_channel +
         connect + the power to mute/deafen/move members, scoped to just
         this one VC via a channel-specific overwrite (not a server-wide
         role) - so a host can moderate their own drill without needing a
         standing staff role, and can't touch any other channel with it.
         This also means a lock, a private mode, or the Expendable/Under
         Review layer above never accidentally locks the host out of their
         own drill.
      4. Per-drill overrides (drill_vc_overrides) - a host/staff explicitly
         blocking or allowing a specific member or role's connect
         permission for this drill. Wins over all layers above for connect
         specifically - e.g. to admit one guest into an otherwise-private
         drill, keep one specific roster member out of an otherwise-open
         one, or have staff strip a host's own access for cause.
      5. Global bans (drill_banned_users) - connect always denied,
         regardless of anything above, including the host layer - the one
         thing nobody can override for their own drill.

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

    def ow(target):
        """Returns the in-progress PermissionOverwrite for target, creating
        an empty one on first use - lets later layers set just the flags
        they care about without clobbering flags an earlier layer already
        set on the same target."""
        return overwrites.setdefault(target, discord.PermissionOverwrite())

    # 0. Access-tier roles - see docstring above for why this has to be
    # rebuilt here on every sync instead of just living on the category.
    if UNDER_REVIEW_ROLE_ID:
        under_review_role = guild.get_role(UNDER_REVIEW_ROLE_ID)
        if under_review_role:
            under_review_ow = ow(under_review_role)
            under_review_ow.view_channel = False
            under_review_ow.connect = False
            under_review_ow.speak = False
            under_review_ow.stream = False
            under_review_ow.use_voice_activation = False
            under_review_ow.request_to_speak = False
            under_review_ow.send_messages = False
            under_review_ow.add_reactions = False

    if EXPENDABLE_ROLE_ID:
        expendable_role = guild.get_role(EXPENDABLE_ROLE_ID)
        if expendable_role:
            # Redundant with @everyone's view_channel=False below as long as
            # that stays unconditional, but kept explicit as a safety net -
            # if @everyone's deny is ever loosened later, Expendable should
            # still be locked out on its own.
            expendable_role_ow = ow(expendable_role)
            expendable_role_ow.view_channel = False
            expendable_role_ow.connect = False

    default_role_ow = ow(guild.default_role)
    default_role_ow.view_channel = False
    default_role_ow.connect = False
    if d["vc_mode"] == "private" or d["vc_locked"]:
        default_role_ow.connect = False

    if d["vc_mode"] == "private" and not d["vc_locked"]:
        for discord_id in get_active_participant_discord_ids(drill_id):
            member = guild.get_member(discord_id)
            if member:
                ow(member).connect = True

    # 2b. Visibility exception for the roster, independent of vc_mode/lock.
    for discord_id in get_active_participant_discord_ids(drill_id):
        member = guild.get_member(discord_id)
        if member:
            ow(member).view_channel = True
            ow(member).connect = True

    host_member = guild.get_member(d["host_discord_id"])
    if host_member:
        host_ow = ow(host_member)
        host_ow.view_channel = True
        host_ow.connect = True
        host_ow.mute_members = True
        host_ow.deafen_members = True
        host_ow.move_members = True

    cursor.execute(
        "SELECT target_type, target_id, permission FROM drill_vc_overrides WHERE drill_id = ?",
        (drill_id,)
    )
    for target_type, target_id, permission in cursor.fetchall():
        target = guild.get_role(target_id) if target_type == "role" else guild.get_member(target_id)
        if target:
            ow(target).connect = (permission == "allowed")

    cursor.execute("SELECT discord_id FROM drill_banned_users")
    for (banned_discord_id,) in cursor.fetchall():
        member = guild.get_member(banned_discord_id)
        if member:
            ow(member).connect = False

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