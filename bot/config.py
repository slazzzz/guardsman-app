### CONFIG ###
# Central place for paths, bot_data.json contents, and every constant derived
# from them. Import from here instead of re-reading bot_data.json elsewhere.

from pathlib import Path

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

EVENT_MODES = event_data.get("event_modes")
EVENT_TYPES = event_data.get("event_types")

LEADERBOARD_CHANNEL_ID: int = leaderboard_data.get("leaderboard_channel_id")
FORM_CHANNEL_ID: int = form_data.get("form_channel_id")
