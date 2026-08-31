### CONFIG ###
# Central place for paths, bot_data.json contents, and every constant derived
# from them. Import from here instead of re-reading bot_data.json elsewhere.

from pathlib import Path
from typing import Optional

import discord

from bot.helpers import load_json

BASE_DIR = Path(__file__).resolve().parent.parent
FONT_PATH = BASE_DIR / "assets" / "font.ttf"
FALLBACK_AVATAR_PATH = BASE_DIR / "anger.png"

# Extension dotted-paths, one per cog - the single source of truth for
# "which cogs exist". app.py's setup_hook loads all of these at startup;
# /admin_reload_cog (bot/cogs/admin.py) offers the same list for hot-
# reloading one without restarting the whole bot. Add new cogs here, not
# directly in app.py, so the two never drift apart.
COGS = (
    "bot.cogs.events",
    "bot.cogs.players",
    "bot.cogs.teams",
    "bot.cogs.seasons",
    "bot.cogs.stats",
    "bot.cogs.roles",
    "bot.cogs.drills",
    "bot.cogs.admin",
)

# Bump this each year when rolling over to a new database file - it's the only
# place the year needs to change (db filename, event_list display, embed title).
DB_YEAR = 2026

# (i == 0/1/2) -> gold/silver/bronze, anything else falls through to white
PLACEMENT_COLORS = {
    0: (255, 215, 0),
    1: (192, 192, 192),
    2: (205, 127, 50),
}
DEFAULT_PLACEMENT_COLOR = (255, 255, 255)

EVENTS_PER_PAGE = 10
EVENT_DATE_FORMAT = "%W-%d-%m"

### BOT DATA (bot_data.json) ###

bot_data = load_json(str(BASE_DIR / "bot_data.json"))

event_data = bot_data.get("event_data")
form_data = bot_data.get("form_data")
guild_data = bot_data.get("guild_data")
leaderboard_data = bot_data.get("leaderboard_data")

GUILD_ID = guild_data.get("main_guild_id")
GUILD: discord.Guild = discord.Object(id=GUILD_ID)

ADMIN_USERS = guild_data.get("admin_users")

STAFF_ROLES = guild_data.get("staff_roles")
HELPER_ROLES = guild_data.get("helper_roles")
HOST_ROLES: Optional[int] = guild_data.get("host_roles")

# The single role that actually carries channel permissions division-wide -
# granted/revoked automatically by /guardsman_role_add and
# /guardsman_role_remove (bot/cogs/roles.py) alongside a member's first/last
# Guardsman role - see TIER_ROLES below, defined further down once the
# three lists it's built from exist.
GUARDSMAN_ACCESS_ROLE: Optional[int] = guild_data.get("guardsman_access_role_id")

EVENT_MODES = event_data.get("event_modes")
EVENT_TYPES = event_data.get("event_types")

LEADERBOARD_CHANNEL_ID: int = leaderboard_data.get("leaderboard_channel_id")
FORM_CHANNEL_ID: int = form_data.get("form_channel_id")

### PLAYER STATS SHOWCASE ###
# Add a "stats_data" block to bot_data.json to configure this feature, e.g.:
#   "stats_data": {
#       "stats_review_channel_id": 123456789012345678,
#       "endless_record_role_ids": [111, 222, 333],
#       "win_role_ids": [444, 555, 666, 777, 888],
#       "badge_role_ids": {"2124583458": 999888777}
#   }
# endless_record_role_ids / win_role_ids should be ordered lowest -> highest
# tier, matching the division's 3 Endless Record roles and 5 Win roles that
# are granted manually via ticket outside the bot - /profile shows whichever
# is the member's highest held role in each list, it doesn't grant them.
#
# badge_role_ids maps a Roblox badge id (as a string key, since JSON object
# keys can't be numbers) to the Discord role that's supposed to mean "has this
# badge". /badge_submit auto-awards when the submitter already holds the
# linked role; those roles are granted manually by staff and can drift out of
# date, so /badge_role_sync lets staff force a re-check for one member.

stats_data = bot_data.get("stats_data", {})

STATS_REVIEW_CHANNEL_ID: int = stats_data.get("stats_review_channel_id")
ENDLESS_RECORD_ROLE_IDS: list[int] = stats_data.get("endless_record_role_ids", [])
WIN_ROLE_IDS: list[int] = stats_data.get("win_role_ids", [])
ENDLESS_FIREWALL_ROLE_IDS: list[int] = stats_data.get("endless_firewall_role_ids", [])
BADGE_ROLE_IDS: dict[int, int] = {int(k): v for k, v in stats_data.get("badge_role_ids", {}).items()}

# Every role id across the three achievement ladders above, flattened into
# one list - there's no separate "rank ladder" beyond these; a member's
# Guardsman standing IS whichever of these roles they hold. Computed here
# (rather than kept as its own manually-maintained bot_data.json key) so it
# can never drift out of sync with the three lists it's made of - add a
# role to any one of them and this picks it up automatically.
# /guardsman_role_add and /guardsman_role_remove (bot/cogs/roles.py) use
# this to detect a member gaining their first Guardsman role of any kind
# (new applicant - also grant Guardsman Access) or losing their last one
# (also strip Guardsman Access).
TIER_ROLES: list[int] = ENDLESS_RECORD_ROLE_IDS + WIN_ROLE_IDS + ENDLESS_FIREWALL_ROLE_IDS

# Membership, for bot purposes (is_allowed()/on_ready's player-DB sync),
# relies on EITHER a Guardsman role OR Guardsman Access - not strictly one
# or the other. In steady state everyone with a Guardsman role also has
# Guardsman Access (that's what the two commands above enforce), but this
# covers the gap right after only one side of that pairing has been
# granted (e.g. a role added manually in Discord rather than through the
# bot), so nobody is unexpectedly locked out of is_allowed()-gated commands
# mid-transition.
MEMBER_ROLES: list[int] = list(TIER_ROLES) + ([GUARDSMAN_ACCESS_ROLE] if GUARDSMAN_ACCESS_ROLE else [])

# key -> (display label, unit label used in leaderboard rows)
STAT_TYPES: dict[str, tuple[str, str]] = {
    "hadal_wins": ("Hadal Blacksite Wins", "Win"),
    "endless_record": ("Endless Record", "Door"),
    "modifier_wins": ("Modified Run Wins (Total)", "Win"),
    "death_count": ("Deaths", "Death"),
    "heartburn_score": ("Heartburn Score", "Point"),
    "heartburn_wins": ("Heartburn Wins", "Win"),
    "raveyard_wins": ("Raveyard Wins", "Win"),
    "hunted_wins": ("Hunted Wins", "Win"),
    "firewall_record": ("Endless Firewall Record", "Door"),
    "robux_spent": ("Robux Spent", "Robux"),
    "max_modifier_percentage": ("Max Modifier Percentage", "%"),
    "modifier_wins_1star": ("Modified Run Wins (1★)", "Win"),
    "modifier_wins_2star": ("Modified Run Wins (2★)", "Win"),
    "modifier_wins_3star": ("Modified Run Wins (3★)", "Win"),
    "modifier_wins_4star": ("Modified Run Wins (4★)", "Win"),
}

# Stats in this dict are NOT written to player_stats directly - nobody
# (member or staff) submits/sets them by hand. Instead the key's value is the
# list of component stat_types that get summed together on the fly whenever
# a total is displayed (currently just /profile and /profile_lookup - see
# stats.py's build_profile_embed). This keeps a single derived number
# (e.g. "Modified Run Wins (Total)") from ever drifting out of sync with its
# star-tier breakdown, and means nobody has to add the breakdown up
# themselves before submitting/approving it.
#
# Every key here must also exist in STAT_TYPES (for its display label/unit) -
# stats.py filters these out of every command that writes a stat_type
# (/stat_submit, /stat_add, /stat_bulk_add, /leaderboard_stats_setup, etc.)
# so no row for one can ever be inserted into player_stats in the first
# place. Adding a new composite stat later just means adding an entry here
# (plus a STAT_TYPES label if it doesn't have one yet) - no schema change.
COMPOSITE_STAT_TYPES: dict[str, list[str]] = {
    "modifier_wins": [
        "modifier_wins_1star",
        "modifier_wins_2star",
        "modifier_wins_3star",
        "modifier_wins_4star",
    ],
}

# key -> (display label, unit label) - drill-derived leaderboard categories,
# parallel to STAT_TYPES but sourced live from the drills/drill_participants
# tables instead of player_stats (a host's drill count isn't a row anyone
# submits or sets - it's aggregated on the fly). See get_drill_leaderboard_rows()
# in bot/leaderboard.py for the actual queries, and get_leaderboard_rows()/
# leaderboard_type_label() there for how a key from either this dict or
# STAT_TYPES gets resolved through the same rendering path (embeds, images,
# auto-updating channel boards via /leaderboard_stats_setup, and the
# /leaderboard browser) without those call sites needing to care which one
# it came from.
DRILL_LEADERBOARD_TYPES: dict[str, tuple[str, str]] = {
    "drills_hosted": ("Drills Hosted", "Drill"),
    "drills_completed": ("Drills Completed", "Drill"),
    "participants_mobilized": ("Participants Mobilized", "Participant"),
}

# Powers the /leaderboard browser's category buttons (bot/ui.py's
# LeaderboardBrowserView) - key -> (button label, {leaderboard_key: (label, unit)}).
# Composite stats (e.g. modifier_wins) are excluded from the Player Stats
# category the same way STAT_CHOICES excludes them in stats.py - they're
# derived/display-only, never a standalone leaderboard. Add a new category
# here (plus its own dict of types) and the browser picks it up automatically,
# no changes needed in bot/ui.py itself.
LEADERBOARD_CATEGORIES: dict[str, tuple[str, dict[str, tuple[str, str]]]] = {
    "stats": ("📊 Player Stats", {
        key: value for key, value in STAT_TYPES.items() if key not in COMPOSITE_STAT_TYPES
    }),
    "drills": ("🛡️ Drill Hosting", DRILL_LEADERBOARD_TYPES),
}

### GUARDSMAN DRILLS ###
# Add a "drill_data" block to bot_data.json to configure this feature, e.g.:
#   "drill_data": {
#       "drills_channel_id": 123456789012345678,
#       "drill_vc_category_id": 123456789012345678,
#       "drill_proof_channel_id": 123456789012345678,
#       "drill_ping_role_ids": [123456789012345678]
#   }
# drill_vc_category_id is optional - if omitted, /drill_start creates the
# voice channel outside any category (still works, just less tidy).
# drill_proof_channel_id is also optional, but strongly recommended - see the
# comment above /drill_end in bot/cogs/drills.py for what it's for. If
# omitted, /drill_end accepts a proof link from any channel.
# drill_log_channel_id is optional too - if set, every completed drill gets
# a bot-posted summary there (host, roster size, completed/failed counts,
# a link to the proof message) and the resulting message's id is saved on
# the drill row (drills.log_channel_id/log_message_id) - see
# post_drill_completion_log() in bot/drills.py. Unlike proof_channel_id
# (the host's own screenshot, which they could edit or delete), this is a
# durable, staff-controlled record with the counts already computed, so
# there's a permanent paper trail even if the host's proof post disappears
# later.

drill_data = bot_data.get("drill_data", {})

DRILLS_CHANNEL_ID: int = drill_data.get("drills_channel_id")
DRILL_VC_CATEGORY_ID: Optional[int] = drill_data.get("drill_vc_category_id")
DRILL_PROOF_CHANNEL_ID: Optional[int] = drill_data.get("drill_proof_channel_id")
DRILL_LOG_CHANNEL_ID: Optional[int] = drill_data.get("drill_log_channel_id")

# Both optional - if either is omitted, sync_drill_vc_permissions() just
# skips that layer (see bot/drills.py). These exist because vc.edit(
# overwrites=...) replaces a channel's ENTIRE overwrite set on every sync,
# so anything inherited from the category (e.g. a server-wide "UNDER
# REVIEW" deny-all set up on the category itself) gets silently wiped the
# first time a drill VC is synced unless it's rebuilt here every time too.
#
# under_review_role_id: denied every relevant voice/text-in-voice
# permission on every drill VC, no exceptions - mirrors the server-wide
# convention of locking this role out of voice channels entirely.
# expendable_role_id: denied view_channel only, so members with this role
# can't see a drill VC exists unless they're the host or an active
# participant (those two get an explicit per-member view_channel=True
# overwrite, which - being member-specific - wins over the role-level deny
# regardless of the drill's public/private mode).
UNDER_REVIEW_ROLE_ID: Optional[int] = drill_data.get("under_review_role_id")
EXPENDABLE_ROLE_ID: Optional[int] = drill_data.get("expendable_role_id")

# Per-user cooldown on /drill_create, in seconds - stops one person from
# flooding the drills channel with back-to-back posts. Staff/helpers/admins
# are exempt (see bot.decorators.cooldown). Defaults to 15 minutes.
DRILL_CREATE_COOLDOWN_SECONDS: float = drill_data.get("create_cooldown_seconds", 900)

# Role(s) pinged in the drills channel when a new drill is posted (see
# /drill_create in bot/cogs/drills.py) - e.g. a "Drill Notifications" role
# members opt into, separate from the division's regular ping roles so
# people who don't care about drills specifically aren't bothered. Optional
# - leave unset/empty for no ping at all. A host can still opt any single
# drill out of the ping (e.g. a small, low-key run) via /drill_create's own
# `ping` option, which defaults to on.
DRILL_PING_ROLE_IDS: list[int] = drill_data.get("drill_ping_role_ids", [])

# How long (in hours) a drill can sit in recruiting/ready with no
# /drill_start before bot.tasks.drill_expiry_loop nudges the host, and how
# long after THAT with still no /drill_start before it's auto-cancelled -
# stops a "created it, then forgot about it" drill from sitting in the
# drills channel forever with a live Join button. Either can be set to 0 to
# disable that stage (0 for the cancel threshold disables auto-cancel
# entirely but keeps the nudge; 0 for both turns the whole loop off).
DRILL_STALE_WARNING_HOURS: float = drill_data.get("stale_warning_hours", 24)
DRILL_STALE_CANCEL_HOURS: float = drill_data.get("stale_cancel_hours", 72)

# label -> (min, max) participant count. max=None means uncapped. This is
# purely for display/defaulting a drill's roster cap - hosts can still pick
# any explicit max_participants when creating one (e.g. a 30-player "Large"
# drill instead of the default 50).
DRILL_SIZES: dict[str, tuple[int, Optional[int]]] = {
    "small": (4, 8),
    "medium": (10, 20),
    "large": (20, 50),
    "mega": (50, None),
}