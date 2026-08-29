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

MEMBER_ROLES = guild_data.get("member_roles")
STAFF_ROLES = guild_data.get("staff_roles")
HELPER_ROLES = guild_data.get("helper_roles")

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

### GUARDSMAN DRILLS ###
# Add a "drill_data" block to bot_data.json to configure this feature, e.g.:
#   "drill_data": {
#       "drills_channel_id": 123456789012345678,
#       "drill_vc_category_id": 123456789012345678,
#       "drill_proof_channel_id": 123456789012345678
#   }
# drill_vc_category_id is optional - if omitted, /drill_start creates the
# voice channel outside any category (still works, just less tidy).
# drill_proof_channel_id is also optional, but strongly recommended - see the
# comment above /drill_end in bot/cogs/drills.py for what it's for. If
# omitted, /drill_end accepts a proof link from any channel.

drill_data = bot_data.get("drill_data", {})

DRILLS_CHANNEL_ID: int = drill_data.get("drills_channel_id")
DRILL_VC_CATEGORY_ID: Optional[int] = drill_data.get("drill_vc_category_id")
DRILL_PROOF_CHANNEL_ID: Optional[int] = drill_data.get("drill_proof_channel_id")

# Per-user cooldown on /drill_create, in seconds - stops one person from
# flooding the drills channel with back-to-back posts. Staff/helpers/admins
# are exempt (see bot.decorators.cooldown). Defaults to 15 minutes.
DRILL_CREATE_COOLDOWN_SECONDS: float = drill_data.get("create_cooldown_seconds", 900)

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