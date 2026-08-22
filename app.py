### LIBRARIES ###

import asyncio
import csv
import discord
import json
import logging
import os
import requests
import sqlite3
import time

from datetime import datetime, timedelta
from dotenv import load_dotenv
from discord import app_commands, Embed, Interaction, Member
from discord.ext import commands, tasks
from io import BytesIO, StringIO
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from typing import Any, Optional

### BOT ENVIRONMENT SETUP ###

load_dotenv()
token = os.getenv("DISCORD_TOKEN")

BASE_DIR = Path(__file__).resolve().parent
FONT_PATH = BASE_DIR / "assets" / "font.ttf"
FALLBACK_AVATAR_PATH = BASE_DIR / "anger.png"

# Bump this each year when rolling over to a new database file - it's the only
# place the year needs to change (db filename, event_list display, embed title).
DB_YEAR = 2026

date = datetime.now()
handler = logging.FileHandler(
    filename=f"discordlogs/discordlog_{date.strftime('%d-%m-%Y_%H-%M-%S')}.log",
    encoding="utf-8",
    mode="w"
)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

roblox_username_cache: dict[int, str] = {}
roblox_avatar_cache: dict[int, str] = {}

# (i == 0/1/2) -> gold/silver/bronze, anything else falls through to white
PLACEMENT_COLORS = {
    0: (255, 215, 0),
    1: (192, 192, 192),
    2: (205, 127, 50),
}
DEFAULT_PLACEMENT_COLOR = (255, 255, 255)
PLACEMENT_EMOJIS = {0: "🥇", 1: "🥈", 2: "🥉"}

EVENTS_PER_PAGE = 10

### HELPER FUNCTIONS ###

def load_json(filename: str) -> dict[str, Any]:
    with open(filename, mode="r") as file:
        return json.load(file)

def save_json(filename: str, data: dict):
    with open(filename, mode="w") as file:
        file.write(json.dumps(data, indent=4))

def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")

def capitalize(s: str) -> str:
    return s[0].upper() + s[1:]

def placement_line(i: int, user_id: int, score: int, label: str = "Point") -> str:
    """Formats a single leaderboard row with medal emoji / ordinal placement."""
    suffix = "s" if score != 1 else ""
    if i in PLACEMENT_EMOJIS:
        place_word = {0: "1st", 1: "2nd", 2: "3rd"}[i]
        return f"{PLACEMENT_EMOJIS[i]} **{place_word} place** - <@{user_id}> - {score} {label}{suffix}\n"
    return f"**{i + 1}{ordinal(i + 1)} place** - <@{user_id}> - {score} {label}{suffix}\n"

def team_placement_line(i: int, team_name: str, score: int, label: str = "Point") -> str:
    """Same as placement_line, but for a team name instead of a Discord mention."""
    suffix = "s" if score != 1 else ""
    if i in PLACEMENT_EMOJIS:
        place_word = {0: "1st", 1: "2nd", 2: "3rd"}[i]
        return f"{PLACEMENT_EMOJIS[i]} **{place_word} place** - {team_name} - {score} {label}{suffix}\n"
    return f"**{i + 1}{ordinal(i + 1)} place** - {team_name} - {score} {label}{suffix}\n"

### ROBLOX API REQUEST HELPER (429-AWARE) ###

ROBLOX_MAX_RETRIES = 3
ROBLOX_DEFAULT_BACKOFF_SECONDS = 1.5

def _roblox_request(method: str, url: str, **kwargs) -> Optional[requests.Response]:
    """Wraps requests.request with retry/backoff for Roblox's rate limiting.

    On a 429, sleeps for the Retry-After header (falling back to a small
    exponential backoff if the header is missing) and tries again, up to
    ROBLOX_MAX_RETRIES times. On persistent failure, returns None so callers
    fall back to cached/placeholder data instead of raising.
    """
    kwargs.setdefault("timeout", 5)
    delay = ROBLOX_DEFAULT_BACKOFF_SECONDS

    for attempt in range(ROBLOX_MAX_RETRIES):
        try:
            response = requests.request(method, url, **kwargs)
        except requests.RequestException as e:
            print(f"Roblox request failed ({method} {url}): {e}")
            return None

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                wait_seconds = float(retry_after) if retry_after else delay
            except ValueError:
                wait_seconds = delay
            print(f"Roblox API rate-limited us (attempt {attempt + 1}/{ROBLOX_MAX_RETRIES}), "
                  f"waiting {wait_seconds:.1f}s before retrying: {url}")
            time.sleep(wait_seconds)
            delay *= 2  # exponential backoff if Retry-After keeps being absent
            continue

        return response

    print(f"Roblox request still rate-limited after {ROBLOX_MAX_RETRIES} attempts, giving up: {url}")
    return None

def get_roblox_id_from_username(username: str) -> Optional[int]:
    if username in roblox_username_cache.values():
        return next((k for k, v in roblox_username_cache.items() if v == username), None)

    url = "https://users.roblox.com/v1/usernames/users"

    # The API expects a list of usernames
    payload = {
        "usernames": [username],
        "excludeBannedUsers": True
    }

    response = _roblox_request("POST", url, json=payload)
    if response is None or response.status_code != 200:
        return None

    data = response.json().get("data")
    if data:
        roblox_id = data[0].get("id")
        cache_roblox_username(roblox_id, username)
        return roblox_id

    return None

def get_avatar_url(roblox_id: int) -> Optional[str]:
    """Single-id avatar lookup, kept for call sites that only need one avatar.
    For rendering a leaderboard, prefer get_avatar_urls_batch() instead so
    multiple players are fetched in one Roblox API call."""
    if roblox_avatar_cache.get(roblox_id):
        return roblox_avatar_cache[roblox_id]

    urls = get_avatar_urls_batch([roblox_id])
    return urls.get(roblox_id)

# Roblox's thumbnails endpoint accepts a batch of userIds in one call; keep
# each request comfortably under Roblox's own per-request cap.
ROBLOX_AVATAR_BATCH_SIZE = 50

def get_avatar_urls_batch(roblox_ids: list[int]) -> dict[int, str]:
    """Resolves avatar URLs for many roblox_ids at once, using the cache for
    anything already known and issuing as few Roblox API calls as possible
    for the rest (chunked to ROBLOX_AVATAR_BATCH_SIZE ids per request).

    This is the preferred entry point when rendering a leaderboard, since it
    turns N sequential requests into ceil(N / ROBLOX_AVATAR_BATCH_SIZE).
    """
    results: dict[int, str] = {}
    missing: list[int] = []

    # De-duplicate while preserving a stable order, and skip anything cached.
    seen: set[int] = set()
    for roblox_id in roblox_ids:
        if not roblox_id or roblox_id in seen:
            continue
        seen.add(roblox_id)
        cached = roblox_avatar_cache.get(roblox_id)
        if cached:
            results[roblox_id] = cached
        else:
            missing.append(roblox_id)

    for i in range(0, len(missing), ROBLOX_AVATAR_BATCH_SIZE):
        chunk = missing[i:i + ROBLOX_AVATAR_BATCH_SIZE]
        ids_param = ",".join(str(rid) for rid in chunk)
        url = (
            "https://thumbnails.roblox.com/v1/users/avatar-headshot"
            f"?userIds={ids_param}&size=150x150&format=Png&isCircular=false"
        )

        response = _roblox_request("GET", url)
        if response is None or response.status_code != 200:
            print(f"Roblox batch avatar lookup failed for chunk starting at index {i}")
            continue

        try:
            data = response.json().get("data", [])
        except ValueError as e:
            print(f"Roblox batch avatar response wasn't JSON: {e}")
            continue

        for entry in data:
            roblox_id = entry.get("targetId")
            image_url = entry.get("imageUrl")
            if roblox_id and image_url:
                cache_roblox_avatar(roblox_id, image_url)
                results[roblox_id] = image_url

    return results

def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Loads the bundled font, falling back to Pillow's built-in font if it's missing.

    Ship a real .ttf at assets/font.ttf (any OFL font such as Inter or Roboto works) -
    "arial.ttf" only resolves on Windows and will crash on most hosting providers.
    """
    try:
        return ImageFont.truetype(str(FONT_PATH), size)
    except OSError:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            # Older Pillow versions don't support a `size` kwarg on load_default
            return ImageFont.load_default()

def make_circular_avatar(avatar: Image.Image, size: int) -> Image.Image:
    """Crops an avatar image to a circle of the given diameter."""
    avatar = avatar.convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    circular = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    circular.paste(avatar, (0, 0), mask=mask)
    return circular

def truncate_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    """Shortens text with a trailing ellipsis so it never overlaps the score column."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return (text + "…") if text else "…"

LEADERBOARD_WIDTH = 900
LEADERBOARD_PADDING = 36
LEADERBOARD_HEADER_HEIGHT = 96
LEADERBOARD_ROW_HEIGHT = 92
LEADERBOARD_ROW_GAP = 14
LEADERBOARD_AVATAR_SIZE = 64
LEADERBOARD_BADGE_SIZE = 40
LEADERBOARD_CARD_FILL = (255, 255, 255, 14)
LEADERBOARD_CARD_TOP3_ALPHA = 34

def leaderboard_image(player_results: list[tuple], display_names: dict[int, str], title: str = "Leaderboard") -> discord.File:
    """Renders a leaderboard PNG entirely in memory (no shared file on disk, so concurrent
    calls can't clobber each other) and returns it as a ready-to-send discord.File.

    display_names must be resolved by the caller beforehand (bot.get_user() can return
    None for uncached users, which used to crash this function).
    """
    entry_count = max(len(player_results), 1)
    height = (
        LEADERBOARD_HEADER_HEIGHT + LEADERBOARD_PADDING
        + entry_count * (LEADERBOARD_ROW_HEIGHT + LEADERBOARD_ROW_GAP) - LEADERBOARD_ROW_GAP
        + LEADERBOARD_PADDING
    )

    # Background gradient, drawn straight into the opaque base layer.
    base = Image.new("RGBA", (LEADERBOARD_WIDTH, height), (0, 0, 0, 255))
    base_draw = ImageDraw.Draw(base)
    top_color, bottom_color = (32, 30, 46), (15, 14, 20)
    for y in range(height):
        t = y / max(height - 1, 1)
        r = int(top_color[0] + (bottom_color[0] - top_color[0]) * t)
        g = int(top_color[1] + (bottom_color[1] - top_color[1]) * t)
        b = int(top_color[2] + (bottom_color[2] - top_color[2]) * t)
        base_draw.line([(0, y), (LEADERBOARD_WIDTH, y)], fill=(r, g, b, 255))

    # Card backgrounds/rings live on their own transparent layer so they can be
    # semi-transparent without needing to hand-blend colors against the gradient.
    overlay = Image.new("RGBA", (LEADERBOARD_WIDTH, height), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)

    # Text and avatars go on a third layer, composited last so they're always crisp.
    text_layer = Image.new("RGBA", (LEADERBOARD_WIDTH, height), (0, 0, 0, 0))
    text_draw = ImageDraw.Draw(text_layer)

    font_title = load_font(34)
    font_name = load_font(24)
    font_score = load_font(22)
    font_badge = load_font(20)

    text_draw.text((LEADERBOARD_PADDING, 30), title, font=font_title, fill=(255, 255, 255, 255))
    overlay_draw.line(
        [(LEADERBOARD_PADDING, LEADERBOARD_HEADER_HEIGHT - 6), (LEADERBOARD_WIDTH - LEADERBOARD_PADDING, LEADERBOARD_HEADER_HEIGHT - 6)],
        fill=(255, 255, 255, 30), width=2
    )

    row_left = LEADERBOARD_PADDING
    row_right = LEADERBOARD_WIDTH - LEADERBOARD_PADDING
    y = LEADERBOARD_HEADER_HEIGHT + LEADERBOARD_PADDING - LEADERBOARD_ROW_GAP

    # Fetch every avatar this render needs in as few Roblox API calls as
    # possible, instead of one request per row inside the loop below.
    avatar_urls = get_avatar_urls_batch([result[2] for result in player_results if result[2]])

    for i, result in enumerate(player_results):
        user_id, score, roblox_id = result[0], result[1], result[2]
        name = display_names.get(user_id, f"Unknown ({user_id})")
        color = PLACEMENT_COLORS.get(i, DEFAULT_PLACEMENT_COLOR)

        row_top = y + LEADERBOARD_ROW_GAP
        row_bottom = row_top + LEADERBOARD_ROW_HEIGHT
        row_mid = row_top + LEADERBOARD_ROW_HEIGHT // 2

        card_fill = (*color, LEADERBOARD_CARD_TOP3_ALPHA) if i < 3 else LEADERBOARD_CARD_FILL
        overlay_draw.rounded_rectangle([row_left, row_top, row_right, row_bottom], radius=18, fill=card_fill)

        # Rank badge
        badge_cx = row_left + 30
        badge_fill = (*color, 255) if i < 3 else (70, 70, 82, 255)
        overlay_draw.ellipse(
            [badge_cx - LEADERBOARD_BADGE_SIZE // 2, row_mid - LEADERBOARD_BADGE_SIZE // 2,
             badge_cx + LEADERBOARD_BADGE_SIZE // 2, row_mid + LEADERBOARD_BADGE_SIZE // 2],
            fill=badge_fill
        )
        badge_text = str(i + 1)
        bbox = text_draw.textbbox((0, 0), badge_text, font=font_badge)
        text_draw.text(
            (badge_cx - (bbox[2] - bbox[0]) / 2, row_mid - (bbox[3] - bbox[1]) / 2 - bbox[1]),
            badge_text, font=font_badge,
            fill=(20, 20, 24, 255) if i < 3 else (230, 230, 235, 255)
        )

        # Avatar with a placement-colored ring
        avatar_cx = badge_cx + LEADERBOARD_BADGE_SIZE // 2 + 16 + LEADERBOARD_AVATAR_SIZE // 2
        avatar_left = avatar_cx - LEADERBOARD_AVATAR_SIZE // 2
        avatar_top = row_mid - LEADERBOARD_AVATAR_SIZE // 2
        ring_pad = 3
        overlay_draw.ellipse(
            [avatar_left - ring_pad, avatar_top - ring_pad,
             avatar_left + LEADERBOARD_AVATAR_SIZE + ring_pad, avatar_top + LEADERBOARD_AVATAR_SIZE + ring_pad],
            fill=(*color, 255) if i < 3 else (90, 90, 100, 255)
        )

        avatar_url = avatar_urls.get(roblox_id) if roblox_id else None
        avatar = None
        if avatar_url:
            try:
                response = requests.get(avatar_url, timeout=5)
                avatar = Image.open(BytesIO(response.content))
            except (requests.RequestException, OSError) as e:
                print(f"Could not load avatar for {user_id}: {e}")
        if avatar is None:
            avatar = Image.open(FALLBACK_AVATAR_PATH)

        circular_avatar = make_circular_avatar(avatar, LEADERBOARD_AVATAR_SIZE)
        text_layer.paste(circular_avatar, (avatar_left, avatar_top), mask=circular_avatar)

        # Name (truncated so it can never collide with the score column)
        text_x = avatar_left + LEADERBOARD_AVATAR_SIZE + 20
        max_name_width = row_right - 170 - text_x
        display_text = truncate_text(text_draw, name, font_name, max_name_width)
        name_bbox = text_draw.textbbox((0, 0), display_text, font=font_name)
        text_draw.text(
            (text_x, row_mid - (name_bbox[3] - name_bbox[1]) / 2 - name_bbox[1]),
            display_text, font=font_name, fill=(255, 255, 255, 255)
        )

        # Score, right-aligned
        score_text = f"{score:,} pts"
        score_bbox = text_draw.textbbox((0, 0), score_text, font=font_score)
        score_w = score_bbox[2] - score_bbox[0]
        text_draw.text(
            (row_right - 24 - score_w, row_mid - (score_bbox[3] - score_bbox[1]) / 2 - score_bbox[1]),
            score_text, font=font_score, fill=(*color, 255) if i < 3 else (220, 220, 226, 255)
        )

        y = row_bottom

    composited = Image.alpha_composite(base, overlay)
    composited = Image.alpha_composite(composited, text_layer).convert("RGB")

    buffer = BytesIO()
    composited.save(buffer, format="PNG")
    buffer.seek(0)

    return discord.File(buffer, filename="leaderboard.png")

async def resolve_display_names(interaction: Interaction, user_ids: list[int]) -> dict[int, str]:
    """Resolves display names for a batch of user IDs, preferring cached guild members
    and falling back to an API fetch for anyone not in cache."""
    names: dict[int, str] = {}
    for user_id in user_ids:
        member = interaction.guild.get_member(user_id) if interaction.guild else None
        if member:
            names[user_id] = member.display_name
            continue
        try:
            user = await bot.fetch_user(user_id)
            names[user_id] = user.name
        except discord.NotFound:
            names[user_id] = f"Unknown ({user_id})"
    return names

async def post_or_update_leaderboard_message(
    channel_id: int,
    embed: Embed,
    stored_message_id: Optional[int] = None,
    stored_channel_id: Optional[int] = None,
) -> discord.Message:
    """Edits the previously-posted leaderboard message in place if it can still be found,
    otherwise posts a fresh one. This is what makes re-running a leaderboard command update
    the existing channel message instead of spamming a new one every time - the caller is
    responsible for persisting the returned message's id/channel for next time (e.g. in the
    events table, or bot_data.json for the global leaderboard)."""
    if stored_message_id and stored_channel_id:
        try:
            existing_channel = bot.get_channel(stored_channel_id) or await bot.fetch_channel(stored_channel_id)
            existing_message = await existing_channel.fetch_message(stored_message_id)
            return await existing_message.edit(embed=embed)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass  # message or channel is gone (e.g. deleted by a mod) - fall through and post fresh

    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    return await channel.send(embed=embed)

### BOT DATA READ ###

bot_data = load_json("bot_data.json")

event_data = bot_data.get("event_data")
form_data = bot_data.get("form_data")
guild_data = bot_data.get("guild_data")
leaderboard_data = bot_data.get("leaderboard_data")

### CONSTANTS ###

GUILD_ID = guild_data.get("main_guild_id")
GUILD: discord.Guild = discord.Object(id=GUILD_ID)

ADMIN_USERS = guild_data.get("admin_users")

MEMBER_ROLES = guild_data.get("member_roles")
STAFF_ROLES = guild_data.get("staff_roles")

EVENT_MODES = event_data.get("event_modes")
EVENT_TYPES = event_data.get("event_types")

LEADERBOARD_CHANNEL_ID: int = leaderboard_data.get("leaderboard_channel_id")
FORM_CHANNEL_ID: int = form_data.get("form_channel_id")

EVENT_DATE_FORMAT = "%W-%d-%m"

### DECORATOR FUNCTIONS ###

def is_staff():
    async def predicate(interaction: Interaction):
        user_roles = [role.id for role in interaction.user.roles]
        return any(r in STAFF_ROLES for r in user_roles)
    return app_commands.check(predicate)

def is_member():
    async def predicate(interaction: Interaction):
        user_roles = [role.id for role in interaction.user.roles]
        return any(r in MEMBER_ROLES for r in user_roles)
    return app_commands.check(predicate)

def is_admin():
    async def predicate(interaction: Interaction):
        return interaction.user.id in ADMIN_USERS
    return app_commands.check(predicate)

def is_allowed():
    async def predicate(interaction: Interaction):
        # Check user ID
        if interaction.user.id in ADMIN_USERS:
            return True

        # Check roles
        if any(role.id in STAFF_ROLES for role in interaction.user.roles):
            return True

        # Check permissions
        if interaction.user.guild_permissions.manage_guild:
            return True

        return False

    return app_commands.check(predicate)

### DATABASE SETUP ###

conn = sqlite3.connect(f"database/leaderboard_{DB_YEAR}.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER UNIQUE,
    roblox_id INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date TEXT,
    event_name TEXT,
    event_mode TEXT,
    event_type TEXT,
    event_prize INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER,
    player_score INTEGER DEFAULT 0,
    event_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY(player_id) REFERENCES players(id),
    FOREIGN KEY(event_id) REFERENCES events(id),
    UNIQUE(player_id, event_id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS roblox_cache (
    roblox_id INTEGER PRIMARY KEY,
    username TEXT,
    avatar_url TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_name TEXT UNIQUE,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS seasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_number INTEGER,
    started_at TEXT DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT
)
""")

conn.commit()

def ensure_column(table: str, column: str, definition: str):
    """Adds a column to an existing table if it isn't already there. Safe to run
    against both brand-new and pre-existing database files (e.g. last year's
    leaderboard_2026.db picking up columns added in a later bot update)."""
    cursor.execute(f"PRAGMA table_info({table})")
    existing_columns = {row[1] for row in cursor.fetchall()}
    if column not in existing_columns:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()

ensure_column("events", "event_start_time", "TEXT")
ensure_column("events", "reminder_sent", "INTEGER DEFAULT 0")
ensure_column("events", "leaderboard_message_id", "INTEGER")
ensure_column("events", "leaderboard_channel_id", "INTEGER")
ensure_column("events", "team_leaderboard_message_id", "INTEGER")
ensure_column("events", "team_leaderboard_channel_id", "INTEGER")
ensure_column("results", "team_id", "INTEGER")
ensure_column("events", "season_id", "INTEGER")

def get_active_season_id() -> int:
    """Returns the id of the currently open season, bootstrapping Season 1 (and
    backfilling any pre-existing events into it) the first time this ever runs."""
    cursor.execute("SELECT id FROM seasons WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    if row:
        return row[0]

    cursor.execute("INSERT INTO seasons (season_number) VALUES (1)")
    conn.commit()
    season_id = cursor.lastrowid
    cursor.execute("UPDATE events SET season_id = ? WHERE season_id IS NULL", (season_id,))
    conn.commit()
    return season_id

### ROBLOX CACHE PERSISTENCE ###
# roblox_username_cache / roblox_avatar_cache stay as in-memory dicts for fast
# lookups during a run, but are now backed by the roblox_cache table so a
# restart doesn't mean re-hitting the Roblox API for every known player.

ROBLOX_AVATAR_CACHE_TTL_SECONDS = 6 * 60 * 60  # avatars change more often than usernames

def load_roblox_cache():
    cursor.execute("SELECT roblox_id, username FROM roblox_cache WHERE username IS NOT NULL")
    for roblox_id, username in cursor.fetchall():
        roblox_username_cache[roblox_id] = username

    cursor.execute("SELECT roblox_id, avatar_url, updated_at FROM roblox_cache WHERE avatar_url IS NOT NULL")
    now = datetime.now()
    for roblox_id, avatar_url, updated_at in cursor.fetchall():
        try:
            age_seconds = (now - datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")).total_seconds()
        except (TypeError, ValueError):
            age_seconds = float("inf")

        if age_seconds < ROBLOX_AVATAR_CACHE_TTL_SECONDS:
            roblox_avatar_cache[roblox_id] = avatar_url

def cache_roblox_username(roblox_id: int, username: str):
    roblox_username_cache[roblox_id] = username
    cursor.execute("""
        INSERT INTO roblox_cache (roblox_id, username, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(roblox_id) DO UPDATE SET username = excluded.username, updated_at = CURRENT_TIMESTAMP
    """, (roblox_id, username))
    conn.commit()

def cache_roblox_avatar(roblox_id: int, avatar_url: str):
    roblox_avatar_cache[roblox_id] = avatar_url
    cursor.execute("""
        INSERT INTO roblox_cache (roblox_id, avatar_url, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(roblox_id) DO UPDATE SET avatar_url = excluded.avatar_url, updated_at = CURRENT_TIMESTAMP
    """, (roblox_id, avatar_url))
    conn.commit()

load_roblox_cache()

### DB LOOKUP HELPERS ###
# These collapse the "find event / player / result or bail out with an error
# message" boilerplate that used to be duplicated across ~8 commands each.

async def require_event(interaction: Interaction, event_number: int) -> Optional[tuple]:
    """Resolves an event by its 1-indexed position (0 = most recent). Sends an
    ephemeral error and returns None if event_number is invalid or nothing matches."""
    if event_number < 0:
        await interaction.response.send_message("event_number must be greater than 0.", ephemeral=True)
        return None

    if event_number != 0:
        cursor.execute("SELECT * FROM events ORDER BY id LIMIT 1 OFFSET ?", (event_number - 1,))
    else:
        cursor.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1")

    event = cursor.fetchone()

    if not event:
        await interaction.response.send_message("Event not found.", ephemeral=True)
        return None

    return event

async def require_player(interaction: Interaction, user: Member) -> Optional[tuple]:
    cursor.execute("SELECT id FROM players WHERE discord_id = ?", (user.id,))
    player = cursor.fetchone()

    if not player:
        await interaction.response.send_message("Player not found.", ephemeral=True)
        return None

    return player

async def require_results(interaction: Interaction, player_id: int, event_id: int) -> Optional[tuple]:
    cursor.execute("SELECT id FROM results WHERE player_id = ? AND event_id = ?", (player_id, event_id))
    result = cursor.fetchone()

    if not result:
        await interaction.response.send_message("Player results not found.", ephemeral=True)
        return None

    return result

def resolve_roblox_ref(raw: str) -> tuple[Optional[int], Optional[str]]:
    """Parses a CSV cell that may be a numeric roblox_id or a Roblox username.
    Returns (roblox_id, error_message). error_message is None on success;
    roblox_id is None for a blank cell (not an error - just "no value given")."""
    raw = raw.strip()
    if not raw:
        return None, None
    if raw.lstrip("-").isdigit():
        return int(raw), None

    roblox_id = get_roblox_id_from_username(raw)
    if roblox_id is None:
        return None, f"could not resolve Roblox username '{raw}'"
    return roblox_id, None

### UI COMPONENTS ###

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

### BOT COMMANDS ###

@bot.tree.command(
    name="ping",
    description="Checks the bot's latency.",
    guild=GUILD
)
@is_staff()
async def ping(interaction: discord.Interaction):
    latency_ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! {latency_ms}ms", ephemeral=True)

@tree.command(
    name="event_add",
    description="Register a new event.",
    guild=GUILD
)
@app_commands.choices(
    mode=[app_commands.Choice(name=mode_name, value=mode_name.lower()) for mode_name in EVENT_MODES],
    type=[app_commands.Choice(name=type_name, value=type_name.lower()) for type_name in EVENT_TYPES]
)
@is_allowed()
async def _event_add(interaction: Interaction, name: str, mode: str, type: str, prize_pool: int = 0, ignore_weekly_condition: bool = False):
    if prize_pool < 0:
        await interaction.response.send_message("prize_pool must be greater than 0.", ephemeral=True)
        return

    now = datetime.now()
    event_week = now.strftime("%W")

    if not ignore_weekly_condition:
        cursor.execute("SELECT event_date FROM events ORDER BY id DESC LIMIT 1")
        last_event = cursor.fetchone()

        if last_event:
            last_event_week = last_event[0].split("-")[0]

            if last_event_week == event_week:
                await interaction.response.send_message("Event already exists for this week.", ephemeral=True)
                return

    event_date = now.strftime(EVENT_DATE_FORMAT)

    try:
        cursor.execute(
            "INSERT INTO events (event_date, event_name, event_mode, event_type, event_prize, season_id) VALUES (?, ?, ?, ?, ?, ?)",
            (event_date, name, mode, type, prize_pool, get_active_season_id())
        )
        conn.commit()

        await interaction.response.send_message("Event registered ✅", ephemeral=True)
    except Exception as e:
        print(f"Could not initialize event in database: {e}")

        await interaction.response.send_message(f"Error occured: {e}", ephemeral=True)

@tree.command(
    name="event_remove",
    description="Remove recent event.",
    guild=GUILD
)
@is_admin()
async def _event_remove(interaction: Interaction, event_number: int = 0):
    event = await require_event(interaction, event_number)
    if event is None:
        return

    event_id, event_name = event[0], event[2]

    view = ConfirmView(interaction.user.id)
    await interaction.response.send_message(
        f"Are you sure you want to remove **{event_name}**? This also deletes all of its results and cannot be undone.",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if not view.confirmed:
        return

    try:
        cursor.execute("DELETE FROM results WHERE event_id = ?", (event_id,))
        cursor.execute("DELETE FROM events WHERE id = ?", (event_id,))
        conn.commit()

        await interaction.followup.send("Event removed ✅", ephemeral=True)
    except Exception as e:
        print(f"Could not remove event from database: {e}")

        await interaction.followup.send(f"Error occured: {e}", ephemeral=True)

@tree.command(
    name="event_list",
    description="Get a list of all hosted events.",
    guild=GUILD
)
@is_allowed()
async def _event_list(interaction: Interaction):
    cursor.execute("SELECT * FROM events")
    events = cursor.fetchall()

    if len(events) == 0:
        await interaction.response.send_message("No events registered.", ephemeral=True)
        return

    lines = []

    for idx, event in enumerate(events):
        # (id, event_date, event_name, event_mode, event_type, event_prize)

        event_id = event[0]
        raw_date = event[1]
        event_name = event[2]
        event_mode = event[3]
        event_type = event[4]
        event_prize = event[5]

        cursor.execute("SELECT id FROM results WHERE event_id = ?", (event_id,))
        event_player_count = len(cursor.fetchall())

        dt_object = datetime.strptime(raw_date, EVENT_DATE_FORMAT)
        new_date = dt_object.strftime(f"%B %d, {DB_YEAR}")

        participants_suffix = "s" if event_player_count != 1 else ""
        lines.append(
            f"**{idx + 1}.** {event_name} - {capitalize(event_mode)} {capitalize(event_type)} - "
            f"{new_date} - {event_player_count} Participant{participants_suffix} - {event_prize} Robux"
        )

    pages = [lines[i:i + EVENTS_PER_PAGE] for i in range(0, len(lines), EVENTS_PER_PAGE)]
    embeds = [
        Embed(title=f"Event list - {DB_YEAR}", description="\n".join(page))
        .set_footer(text=f"Page {i + 1}/{len(pages)}")
        for i, page in enumerate(pages)
    ]

    if len(embeds) == 1:
        await interaction.response.send_message(embed=embeds[0])
    else:
        view = PaginatorView(embeds, interaction.user.id)
        await interaction.response.send_message(embed=embeds[0], view=view)

@tree.command(
    name="results_export",
    description="Export event results to a CSV file for archiving.",
    guild=GUILD
)
@is_allowed()
async def _results_export(interaction: Interaction, first_event_number: int = 0, final_event_number: int = 0):
    if first_event_number < 0 or final_event_number < 0:
        await interaction.response.send_message("Event numbers must be greater than 0.", ephemeral=True)
        return

    if first_event_number == 0 and final_event_number == 0:
        # No range given - export the whole season.
        cursor.execute("SELECT id FROM events ORDER BY id")
        event_ids = [row[0] for row in cursor.fetchall()]
    else:
        first = first_event_number or 1
        final = final_event_number or first

        if final < first:
            await interaction.response.send_message("final_event_number must be greater than first_event_number.", ephemeral=True)
            return

        cursor.execute(
            "SELECT id FROM events ORDER BY id LIMIT ? OFFSET ?",
            (final - first + 1, first - 1)
        )
        event_ids = [row[0] for row in cursor.fetchall()]

    if not event_ids:
        await interaction.response.send_message("No events found.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "event_id", "event_date", "event_name", "event_mode", "event_type", "event_prize",
        "discord_id", "roblox_id", "player_score", "placement"
    ])

    for event_id in event_ids:
        cursor.execute(
            "SELECT event_date, event_name, event_mode, event_type, event_prize FROM events WHERE id = ?",
            (event_id,)
        )
        event = cursor.fetchone()
        if not event:
            continue
        event_date, event_name, event_mode, event_type, event_prize = event

        cursor.execute("""
            SELECT players.discord_id, players.roblox_id, results.player_score
            FROM results
            JOIN players ON results.player_id = players.id
            WHERE results.event_id = ?
            ORDER BY results.player_score DESC
        """, (event_id,))

        for placement, (discord_id, roblox_id, player_score) in enumerate(cursor.fetchall(), start=1):
            writer.writerow([
                event_id, event_date, event_name, event_mode, event_type, event_prize,
                discord_id, roblox_id, player_score, placement
            ])

    csv_file = BytesIO(buffer.getvalue().encode("utf-8"))
    filename = f"results_export_{DB_YEAR}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    await interaction.followup.send(file=discord.File(csv_file, filename=filename), ephemeral=True)

@tree.command(
    name="event_leaderboard",
    description="Display a leaderboard compiling the result of an event.",
    guild=GUILD

)
@is_allowed()
async def _event_leadeboard(interaction: Interaction, event_number: int = 0, display_in_channel: bool = False, generate_image: bool = False):
    event = await require_event(interaction, event_number)
    if event is None:
        return

    event_id, event_name = event[0], event[2]

    try:
        cursor.execute("""
            SELECT players.discord_id, results.player_score, players.roblox_id
            FROM results
            JOIN players ON results.player_id = players.id
            WHERE results.event_id = ?
            GROUP BY results.player_id
            ORDER BY results.player_score DESC
        """, (event_id,))
        event_results = cursor.fetchall()
    except Exception as e:
        print(f"Could not fetch event leaderboard: {e}")
        await interaction.response.send_message(f"Error occured: {e}", ephemeral=True)
        return

    desc = "".join(placement_line(i, row[0], row[1]) for i, row in enumerate(event_results))

    embed = Embed(title=f"🏆 {event_name} Leaderboard", description="No results found." if desc == "" else desc)
    await interaction.response.send_message(embed=embed)

    if display_in_channel:
        cursor.execute(
            "SELECT leaderboard_message_id, leaderboard_channel_id FROM events WHERE id = ?",
            (event_id,)
        )
        existing_message_id, existing_channel_id = cursor.fetchone() or (None, None)

        posted_message = await post_or_update_leaderboard_message(
            LEADERBOARD_CHANNEL_ID, embed, existing_message_id, existing_channel_id
        )

        cursor.execute(
            "UPDATE events SET leaderboard_message_id = ?, leaderboard_channel_id = ? WHERE id = ?",
            (posted_message.id, posted_message.channel.id, event_id)
        )
        conn.commit()

    if generate_image and event_results:
        names = await resolve_display_names(interaction, [row[0] for row in event_results])
        # leaderboard_image() makes blocking Roblox API/image requests. Run it in a
        # worker thread so it can't freeze the bot's single event loop (heartbeats,
        # other users' commands, etc.) while it waits on the network.
        image = await asyncio.to_thread(
            leaderboard_image, event_results, names, f"{event_name} Leaderboard"
        )
        await interaction.followup.send(file=image)

@tree.command(
    name="event_update",
    description="Update an event.",
    guild=GUILD
)
@app_commands.choices(
    field=[app_commands.Choice(name=field_name, value=field_name) for field_name in ["event_name", "event_mode", "event_type", "event_prize"]]
)
@is_allowed()
async def _event_update(interaction: Interaction, field: str, value: str, event_number: int = 0):
    event = await require_event(interaction, event_number)
    if event is None:
        return

    event_id = event[0]

    try:
        cursor.execute(
            f"UPDATE events SET {field} = ? WHERE id = ?",
            (int(value) if field == "event_prize" else value if field == "event_name" else value.lower(), event_id)
        )
        conn.commit()

        await interaction.response.send_message(f"{field} updated ✅", ephemeral=True)
    except Exception as e:
        print(f"Could not update event field value: {e}")

        await interaction.response.send_message(f"Error occured: {e}", ephemeral=True)

@tree.command(
    name="event_form",
    description="Open a form of an event in the forms channel.",
    guild=GUILD
)
@is_allowed()
async def _event_form(interaction: Interaction, event_number: int = 0):
    event = await require_event(interaction, event_number)
    if event is None:
        return

    event_id, event_name = event[0], event[2]

    form_data.update({"active_event_id": event_id})
    bot_data.update({"active_event_data": form_data})
    save_json("bot_data.json", bot_data)

    view = JoinEventView(event_id)

    active_event_channel = bot.get_channel(FORM_CHANNEL_ID)
    if active_event_channel is None:
        await interaction.response.send_message("Form channel not found - check FORM_CHANNEL_ID.", ephemeral=True)
        return

    await active_event_channel.send(f"Click below to join **{event_name}**!", view=view)

    await interaction.response.send_message(f"Form created for {event_name} ✅", ephemeral=True)

@tree.command(
    name="event_set_start_time",
    description="Set when an event starts so registered players get a reminder DM ~1 hour before.",
    guild=GUILD
)
@is_allowed()
async def _event_set_start_time(interaction: Interaction, day: int, month: int, hour: int, minute: int, event_number: int = 0):
    event = await require_event(interaction, event_number)
    if event is None:
        return

    event_id, event_name = event[0], event[2]

    try:
        # Scoped to DB_YEAR since each season gets its own database file.
        start_time = datetime(DB_YEAR, month, day, hour, minute)
    except ValueError as e:
        await interaction.response.send_message(f"Invalid date/time: {e}", ephemeral=True)
        return

    cursor.execute(
        "UPDATE events SET event_start_time = ?, reminder_sent = 0 WHERE id = ?",
        (start_time.strftime("%Y-%m-%d %H:%M:%S"), event_id)
    )
    conn.commit()

    await interaction.response.send_message(
        f"Start time for **{event_name}** set to {start_time.strftime('%B %d, %H:%M')} - "
        f"registered players will get a reminder DM about an hour before ✅"
    )

@tree.command(
    name="player_register",
    description="Register a player to the active event",
    guild=GUILD
)
@is_allowed()
async def _player_register(interaction: Interaction, user: Member, roblox_id: int = 0, event_number: int = 0):
    event = await require_event(interaction, event_number)
    if event is None:
        return

    event_id = event[0]
    user_id = user.id

    cursor.execute("SELECT id FROM players WHERE discord_id = ?", (user_id,))
    player = cursor.fetchone()

    if not player:
        try:
            cursor.execute(
                "INSERT INTO players (discord_id, roblox_id) VALUES (?, ?)",
                (user_id, roblox_id)
            )
            conn.commit()

            cursor.execute("SELECT id FROM players WHERE discord_id = ?", (user_id,))
            player = cursor.fetchone()
        except Exception as e:
            print(f"Could not initialize player in database: {e}")

            await interaction.response.send_message(f"Error occured: {e}", ephemeral=True)
            return

    player_id = player[0]

    cursor.execute("SELECT id FROM results WHERE player_id = ? AND event_id = ?", (player_id, event_id))
    existing_results = cursor.fetchone()

    if existing_results:
        await interaction.response.send_message("Player already registered.", ephemeral=True)
        return

    try:
        cursor.execute(
            "INSERT INTO results (player_id, player_score, event_id) VALUES (?, ?, ?)",
            (player_id, 0, event_id)
        )
        conn.commit()

        await interaction.response.send_message(f"<@{user_id}> registered ✅", ephemeral=True)
    except Exception as e:
        print(f"Could not initialize player results in database: {e}")

        await interaction.response.send_message(f"Error occured: {e}", ephemeral=True)

@tree.command(
    name="player_results_update",
    description="Update a player's score in an event",
    guild=GUILD
)
@is_allowed()
async def _player_results_update(interaction: Interaction, user: Member, event_number: int = 0, player_score: int = 0):
    if player_score < 0:
        await interaction.response.send_message("player_score must be greater than 0.", ephemeral=True)
        return

    event = await require_event(interaction, event_number)
    if event is None:
        return
    event_id = event[0]

    player = await require_player(interaction, user)
    if player is None:
        return
    player_id = player[0]

    if await require_results(interaction, player_id, event_id) is None:
        return

    try:
        cursor.execute(
            "UPDATE results SET player_score = ? WHERE player_id = ? AND event_id = ?",
            (player_score, player_id, event_id)
        )
        conn.commit()

        await interaction.response.send_message(f"Results updated for <@{user.id}> ✅", ephemeral=True)
    except Exception as e:
        print(f"Could not update player results in database: {e}")

        await interaction.response.send_message(f"Error occured: {e}", ephemeral=True)

@tree.command(
    name="player_results_delete",
    description="Delete a player's score in an event",
    guild=GUILD
)
@is_admin()
async def _player_results_delete(interaction: Interaction, user: Member, event_number: int = 0):
    event = await require_event(interaction, event_number)
    if event is None:
        return
    event_id = event[0]

    player = await require_player(interaction, user)
    if player is None:
        return
    player_id = player[0]

    if await require_results(interaction, player_id, event_id) is None:
        return

    view = ConfirmView(interaction.user.id)
    await interaction.response.send_message(
        f"Delete <@{user.id}>'s results for this event? This cannot be undone.",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if not view.confirmed:
        return

    try:
        cursor.execute(
            "DELETE FROM results WHERE (player_id, event_id) = (?, ?)",
            (player_id, event_id)
        )
        conn.commit()

        await interaction.followup.send(f"Results deleted for <@{user.id}> ✅", ephemeral=True)
    except Exception as e:
        print(f"Could not delete player results from database: {e}")

        await interaction.followup.send(f"Error occured: {e}", ephemeral=True)

@tree.command(
    name="player_info",
    description="Get a player's event information",
    guild=GUILD
)
@is_allowed()
async def _player_info(interaction: Interaction, user: Member):
    player = await require_player(interaction, user)
    if player is None:
        return
    player_id = player[0]

    cursor.execute("SELECT * FROM results WHERE player_id = ?", (player_id,))
    player_results = cursor.fetchall()

    fetched_user = await bot.fetch_user(user.id)

    embed = Embed(title=f"🏅 {fetched_user.name} Global Results")
    embed.set_thumbnail(url=fetched_user.display_avatar.url)
    embed.set_footer(text=f"User ID: {user.id}")

    if len(player_results) == 0:
        embed.description = "No event results found."
    else:
        for result in player_results:
            player_score = result[2]
            event_id = result[3]

            cursor.execute("SELECT * FROM events WHERE id = ?", (event_id,))
            event = cursor.fetchone()

            if not event:
                continue

            cursor.execute(
                "SELECT player_id FROM results WHERE event_id = ? ORDER BY player_score DESC",
                (event_id,)
            )
            rankings = [row[0] for row in cursor.fetchall()]

            player_ranking = rankings.index(player_id) + 1
            event_name = event[2]

            embed.add_field(name=event_name, value=f"Score: {player_score} - Placement: {player_ranking}")

    await interaction.response.send_message(embed=embed)

@tree.command(
    name="player_roblox_id_update",
    description="Update a player's Roblox ID.",
    guild=GUILD
)
@is_admin()
async def _player_roblox_id_update(interaction: Interaction, user: Member, roblox_id: int):
    if roblox_id <= 0:
        await interaction.response.send_message("roblox_id must be greater than 0.", ephemeral=True)
        return

    player = await require_player(interaction, user)
    if player is None:
        return

    try:
        cursor.execute(
            "UPDATE players SET roblox_id = ? WHERE discord_id = ?",
            (roblox_id, user.id)
        )
        conn.commit()

        await interaction.response.send_message(f"Roblox ID updated for <@{user.id}> ✅", ephemeral=True)
    except Exception as e:
        print(f"Could not update player results in database: {e}")

        await interaction.response.send_message(f"Error occured: {e}", ephemeral=True)

@tree.command(
    name="player_bulk_register",
    description="Bulk-create players from a file (discord_id[,roblox_id_or_username]).",
    guild=GUILD
)
@is_allowed()
async def _player_bulk_register(interaction: Interaction, file: discord.Attachment):
    if not file.filename.lower().endswith((".csv", ".txt")):
        await interaction.response.send_message("Please attach a .csv or .txt file.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        text = (await file.read()).decode("utf-8-sig")
    except Exception as e:
        await interaction.followup.send(f"Could not read attachment: {e}", ephemeral=True)
        return

    rows = [row for row in csv.reader(StringIO(text)) if row and any(cell.strip() for cell in row)]
    if rows and rows[0][0].strip().lower() in ("discord_id", "discord id", "id"):
        rows = rows[1:]  # optional header row

    if not rows:
        await interaction.followup.send("File is empty.", ephemeral=True)
        return

    created = skipped = 0
    errors: list[str] = []

    for line_number, row in enumerate(rows, start=1):
        if not row or not row[0].strip():
            errors.append(f"Row {line_number}: missing discord_id.")
            continue

        try:
            discord_id = int(row[0].strip())
        except ValueError:
            errors.append(f"Row {line_number}: discord_id must be a number.")
            continue

        roblox_id = None
        if len(row) >= 2 and row[1].strip():
            roblox_id, resolve_error = resolve_roblox_ref(row[1])
            if resolve_error:
                errors.append(f"Row {line_number}: {resolve_error}.")
                continue

        cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
        if cursor.fetchone():
            skipped += 1
            continue

        try:
            cursor.execute(
                "INSERT INTO players (discord_id, roblox_id) VALUES (?, ?)",
                (discord_id, roblox_id or 0)
            )
            created += 1
        except Exception as e:
            errors.append(f"Row {line_number}: {e}")

    conn.commit()

    summary = f"Bulk register: {created} created, {skipped} already registered"
    summary += " ✅" if not errors else f", {len(errors)} error(s):\n" + "\n".join(errors[:15])
    if len(errors) > 15:
        summary += f"\n...and {len(errors) - 15} more."

    await interaction.followup.send(summary[:2000], ephemeral=True)

@tree.command(
    name="player_bulk_update",
    description="Bulk-update players' Roblox IDs from a file (discord_id,roblox_id_or_username).",
    guild=GUILD
)
@is_admin()
async def _player_bulk_update(interaction: Interaction, file: discord.Attachment):
    if not file.filename.lower().endswith((".csv", ".txt")):
        await interaction.response.send_message("Please attach a .csv or .txt file.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        text = (await file.read()).decode("utf-8-sig")
    except Exception as e:
        await interaction.followup.send(f"Could not read attachment: {e}", ephemeral=True)
        return

    rows = [row for row in csv.reader(StringIO(text)) if row and any(cell.strip() for cell in row)]
    if rows and rows[0][0].strip().lower() in ("discord_id", "discord id", "id"):
        rows = rows[1:]  # optional header row

    if not rows:
        await interaction.followup.send("File is empty.", ephemeral=True)
        return

    updated = not_found = 0
    errors: list[str] = []

    for line_number, row in enumerate(rows, start=1):
        if len(row) < 2 or not row[0].strip() or not row[1].strip():
            errors.append(f"Row {line_number}: expected discord_id,roblox_id_or_username.")
            continue

        try:
            discord_id = int(row[0].strip())
        except ValueError:
            errors.append(f"Row {line_number}: discord_id must be a number.")
            continue

        roblox_id, resolve_error = resolve_roblox_ref(row[1])
        if resolve_error:
            errors.append(f"Row {line_number}: {resolve_error}.")
            continue
        if roblox_id is None:
            errors.append(f"Row {line_number}: roblox_id_or_username is required.")
            continue

        cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
        if not cursor.fetchone():
            not_found += 1
            continue

        cursor.execute("UPDATE players SET roblox_id = ? WHERE discord_id = ?", (roblox_id, discord_id))
        updated += 1

    conn.commit()

    summary = f"Bulk update: {updated} updated, {not_found} not registered"
    summary += " ✅" if not errors else f", {len(errors)} error(s):\n" + "\n".join(errors[:15])
    if len(errors) > 15:
        summary += f"\n...and {len(errors) - 15} more."

    await interaction.followup.send(summary[:2000], ephemeral=True)

@tree.command(
    name="player_bulk_remove",
    description="Bulk-remove players and ALL their results from a file (one discord_id per line).",
    guild=GUILD
)
@is_admin()
async def _player_bulk_remove(interaction: Interaction, file: discord.Attachment):
    try:
        text = (await file.read()).decode("utf-8-sig")
    except Exception as e:
        await interaction.response.send_message(f"Could not read attachment: {e}", ephemeral=True)
        return

    rows = [row for row in csv.reader(StringIO(text)) if row and any(cell.strip() for cell in row)]
    if rows and rows[0][0].strip().lower() in ("discord_id", "discord id", "id"):
        rows = rows[1:]  # optional header row

    discord_ids: list[int] = []
    skipped = 0
    for row in rows:
        try:
            discord_ids.append(int(row[0].strip()))
        except (ValueError, IndexError):
            skipped += 1

    if not discord_ids:
        await interaction.response.send_message("No valid discord_id values found in file.", ephemeral=True)
        return

    view = ConfirmView(interaction.user.id)
    warning = f"Remove {len(discord_ids)} player(s) and ALL of their event results? This cannot be undone."
    if skipped:
        warning += f"\n({skipped} row(s) couldn't be parsed and will be skipped.)"
    await interaction.response.send_message(warning, view=view, ephemeral=True)
    await view.wait()

    if not view.confirmed:
        return

    removed = not_found = 0
    for discord_id in discord_ids:
        cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
        player = cursor.fetchone()
        if not player:
            not_found += 1
            continue

        player_id = player[0]
        cursor.execute("DELETE FROM results WHERE player_id = ?", (player_id,))
        cursor.execute("DELETE FROM players WHERE id = ?", (player_id,))
        removed += 1

    conn.commit()

    await interaction.followup.send(f"Removed {removed} player(s) ({not_found} not found) ✅", ephemeral=True)

@tree.command(
    name="player_results_bulk_update",
    description="Bulk add/update player results for an event from a CSV file (discord_id,player_score[,roblox_id]).",
    guild=GUILD
)
@is_allowed()
async def _player_results_bulk_update(interaction: Interaction, file: discord.Attachment, event_number: int = 0):
    event = await require_event(interaction, event_number)
    if event is None:
        return
    event_id, event_name = event[0], event[2]

    if not file.filename.lower().endswith((".csv", ".txt")):
        await interaction.response.send_message("Please attach a .csv or .txt file.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        text = (await file.read()).decode("utf-8-sig")
    except Exception as e:
        await interaction.followup.send(f"Could not read attachment: {e}", ephemeral=True)
        return

    rows = [row for row in csv.reader(StringIO(text)) if row and any(cell.strip() for cell in row)]
    if rows and rows[0][0].strip().lower() in ("discord_id", "discord id", "id"):
        rows = rows[1:]  # optional header row

    if not rows:
        await interaction.followup.send("File is empty.", ephemeral=True)
        return

    created = updated = 0
    errors: list[str] = []

    for line_number, row in enumerate(rows, start=1):
        if len(row) < 2:
            errors.append(f"Row {line_number}: expected at least discord_id,player_score.")
            continue

        try:
            discord_id = int(row[0].strip())
            player_score = int(row[1].strip())
        except ValueError:
            errors.append(f"Row {line_number}: discord_id and player_score must be numbers.")
            continue

        if player_score < 0:
            errors.append(f"Row {line_number}: player_score must be >= 0.")
            continue

        roblox_id = None
        if len(row) >= 3 and row[2].strip():
            try:
                roblox_id = int(row[2].strip())
            except ValueError:
                errors.append(f"Row {line_number}: roblox_id must be a number if provided.")
                continue

        cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
        player = cursor.fetchone()

        if not player:
            cursor.execute(
                "INSERT INTO players (discord_id, roblox_id) VALUES (?, ?)",
                (discord_id, roblox_id or 0)
            )
            cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
            player = cursor.fetchone()
        elif roblox_id:
            cursor.execute("UPDATE players SET roblox_id = ? WHERE discord_id = ?", (roblox_id, discord_id))

        player_id = player[0]

        cursor.execute("SELECT id FROM results WHERE player_id = ? AND event_id = ?", (player_id, event_id))
        if cursor.fetchone():
            cursor.execute(
                "UPDATE results SET player_score = ? WHERE player_id = ? AND event_id = ?",
                (player_score, player_id, event_id)
            )
            updated += 1
        else:
            cursor.execute(
                "INSERT INTO results (player_id, player_score, event_id) VALUES (?, ?, ?)",
                (player_id, player_score, event_id)
            )
            created += 1

    conn.commit()

    summary = f"Bulk update for **{event_name}**: {created} created, {updated} updated"
    summary += " ✅" if not errors else f", {len(errors)} error(s):\n" + "\n".join(errors[:15])
    if len(errors) > 15:
        summary += f"\n...and {len(errors) - 15} more."

    await interaction.followup.send(summary[:2000], ephemeral=True)

@tree.command(
    name="player_results_bulk_delete",
    description="Bulk delete player results for an event from a CSV file (one discord_id per line).",
    guild=GUILD
)
@is_admin()
async def _player_results_bulk_delete(interaction: Interaction, file: discord.Attachment, event_number: int = 0):
    event = await require_event(interaction, event_number)
    if event is None:
        return
    event_id, event_name = event[0], event[2]

    try:
        text = (await file.read()).decode("utf-8-sig")
    except Exception as e:
        await interaction.response.send_message(f"Could not read attachment: {e}", ephemeral=True)
        return

    rows = [row for row in csv.reader(StringIO(text)) if row and any(cell.strip() for cell in row)]
    if rows and rows[0][0].strip().lower() in ("discord_id", "discord id", "id"):
        rows = rows[1:]  # optional header row

    discord_ids: list[int] = []
    skipped = 0
    for row in rows:
        try:
            discord_ids.append(int(row[0].strip()))
        except (ValueError, IndexError):
            skipped += 1

    if not discord_ids:
        await interaction.response.send_message("No valid discord_id values found in file.", ephemeral=True)
        return

    view = ConfirmView(interaction.user.id)
    warning = f"Delete results for {len(discord_ids)} player(s) in **{event_name}**? This cannot be undone."
    if skipped:
        warning += f"\n({skipped} row(s) couldn't be parsed and will be skipped.)"
    await interaction.response.send_message(warning, view=view, ephemeral=True)
    await view.wait()

    if not view.confirmed:
        return

    deleted = 0
    for discord_id in discord_ids:
        cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
        player = cursor.fetchone()
        if not player:
            continue
        cursor.execute("DELETE FROM results WHERE player_id = ? AND event_id = ?", (player[0], event_id))
        deleted += cursor.rowcount

    conn.commit()

    await interaction.followup.send(f"Deleted results for {deleted} player(s) in **{event_name}** ✅", ephemeral=True)

@tree.command(
    name="team_add",
    description="Create a new team.",
    guild=GUILD
)
@is_allowed()
async def _team_add(interaction: Interaction, name: str):
    try:
        cursor.execute("INSERT INTO teams (team_name) VALUES (?)", (name,))
        conn.commit()
        await interaction.response.send_message(f"Team **{name}** created ✅", ephemeral=True)
    except sqlite3.IntegrityError:
        await interaction.response.send_message(f"A team named **{name}** already exists.", ephemeral=True)

@tree.command(
    name="team_remove",
    description="Delete a team. Members keep their individual results but lose their team tag.",
    guild=GUILD
)
@is_admin()
async def _team_remove(interaction: Interaction, name: str):
    cursor.execute("SELECT id FROM teams WHERE team_name = ?", (name,))
    team = cursor.fetchone()
    if not team:
        await interaction.response.send_message(f"Team **{name}** not found.", ephemeral=True)
        return
    team_id = team[0]

    view = ConfirmView(interaction.user.id)
    await interaction.response.send_message(
        f"Delete team **{name}**? Members keep their individual results but lose their team tag in every event.",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if not view.confirmed:
        return

    cursor.execute("UPDATE results SET team_id = NULL WHERE team_id = ?", (team_id,))
    cursor.execute("DELETE FROM teams WHERE id = ?", (team_id,))
    conn.commit()

    await interaction.followup.send(f"Team **{name}** deleted ✅", ephemeral=True)

@tree.command(
    name="team_assign",
    description="Assign a player to a team for a specific event.",
    guild=GUILD
)
@is_allowed()
async def _team_assign(interaction: Interaction, user: Member, team: str, event_number: int = 0):
    event = await require_event(interaction, event_number)
    if event is None:
        return
    event_id, event_name = event[0], event[2]

    cursor.execute("SELECT id FROM teams WHERE team_name = ?", (team,))
    team_row = cursor.fetchone()
    if not team_row:
        await interaction.response.send_message(f"Team **{team}** not found - create it first with /team_add.", ephemeral=True)
        return
    team_id = team_row[0]

    cursor.execute("SELECT id FROM players WHERE discord_id = ?", (user.id,))
    player = cursor.fetchone()
    if not player:
        cursor.execute("INSERT INTO players (discord_id) VALUES (?)", (user.id,))
        conn.commit()
        cursor.execute("SELECT id FROM players WHERE discord_id = ?", (user.id,))
        player = cursor.fetchone()
    player_id = player[0]

    # Team assignment lives on the results row (per event), not the player - a
    # player can be on a different team, or no team, in each event. Assigning a
    # team also registers the player for the event (score 0) if they weren't
    # already, same as /player_register.
    cursor.execute("SELECT id FROM results WHERE player_id = ? AND event_id = ?", (player_id, event_id))
    result = cursor.fetchone()
    if result:
        cursor.execute("UPDATE results SET team_id = ? WHERE id = ?", (team_id, result[0]))
    else:
        cursor.execute(
            "INSERT INTO results (player_id, player_score, event_id, team_id) VALUES (?, 0, ?, ?)",
            (player_id, event_id, team_id)
        )
    conn.commit()

    await interaction.response.send_message(f"<@{user.id}> assigned to **{team}** for **{event_name}** ✅", ephemeral=True)

@tree.command(
    name="team_unassign",
    description="Remove a player from their team for a specific event.",
    guild=GUILD
)
@is_allowed()
async def _team_unassign(interaction: Interaction, user: Member, event_number: int = 0):
    event = await require_event(interaction, event_number)
    if event is None:
        return
    event_id = event[0]

    player = await require_player(interaction, user)
    if player is None:
        return
    player_id = player[0]

    if await require_results(interaction, player_id, event_id) is None:
        return

    cursor.execute("UPDATE results SET team_id = NULL WHERE player_id = ? AND event_id = ?", (player_id, event_id))
    conn.commit()

    await interaction.response.send_message(f"<@{user.id}> removed from their team for this event ✅", ephemeral=True)

@tree.command(
    name="team_list",
    description="List all teams and their member counts for an event.",
    guild=GUILD
)
@is_allowed()
async def _team_list(interaction: Interaction, event_number: int = 0):
    event = await require_event(interaction, event_number)
    if event is None:
        return
    event_id, event_name = event[0], event[2]

    cursor.execute("""
        SELECT teams.team_name, COUNT(results.id)
        FROM teams
        LEFT JOIN results ON results.team_id = teams.id AND results.event_id = ?
        GROUP BY teams.id
        ORDER BY teams.team_name
    """, (event_id,))
    teams = cursor.fetchall()

    if not teams:
        await interaction.response.send_message("No teams created yet.", ephemeral=True)
        return

    desc = "\n".join(f"**{name}** - {count} member{'s' if count != 1 else ''}" for name, count in teams)
    await interaction.response.send_message(embed=Embed(title=f"Teams - {event_name}", description=desc))

@tree.command(
    name="team_leaderboard",
    description="Display team totals for an event. Players without a team still show up individually.",
    guild=GUILD
)
@is_allowed()
async def _team_leaderboard(interaction: Interaction, event_number: int = 0, display_in_channel: bool = False):
    event = await require_event(interaction, event_number)
    if event is None:
        return
    event_id, event_name = event[0], event[2]

    cursor.execute("""
        SELECT results.team_id, teams.team_name, players.discord_id, results.player_score
        FROM results
        JOIN players ON results.player_id = players.id
        LEFT JOIN teams ON results.team_id = teams.id
        WHERE results.event_id = ?
    """, (event_id,))
    rows = cursor.fetchall()

    # Players on a team get pooled into a team total; players with no team (we
    # don't force everyone onto one) keep their own individual score and rank
    # alongside the teams rather than getting dropped from the board.
    team_totals: dict[int, list] = {}
    unassigned: list[tuple[int, int]] = []

    for team_id, team_name, discord_id, score in rows:
        if team_id is not None:
            if team_id not in team_totals:
                team_totals[team_id] = [team_name, 0]
            team_totals[team_id][1] += score
        else:
            unassigned.append((discord_id, score))

    combined = [(name, total) for name, total in team_totals.values()]
    combined += [(f"<@{discord_id}> *(no team)*", score) for discord_id, score in unassigned]
    combined.sort(key=lambda entry: entry[1], reverse=True)

    desc = "".join(team_placement_line(i, label, score) for i, (label, score) in enumerate(combined))
    embed = Embed(
        title=f"🏆 {event_name} Team Leaderboard",
        description="No results found." if desc == "" else desc
    )
    await interaction.response.send_message(embed=embed)

    if display_in_channel:
        cursor.execute(
            "SELECT team_leaderboard_message_id, team_leaderboard_channel_id FROM events WHERE id = ?",
            (event_id,)
        )
        existing_message_id, existing_channel_id = cursor.fetchone() or (None, None)

        posted_message = await post_or_update_leaderboard_message(
            LEADERBOARD_CHANNEL_ID, embed, existing_message_id, existing_channel_id
        )

        cursor.execute(
            "UPDATE events SET team_leaderboard_message_id = ?, team_leaderboard_channel_id = ? WHERE id = ?",
            (posted_message.id, posted_message.channel.id, event_id)
        )
        conn.commit()

@tree.command(
    name="team_bulk_add",
    description="Bulk create teams from a CSV/text file (one team name per line).",
    guild=GUILD
)
@is_allowed()
async def _team_bulk_add(interaction: Interaction, file: discord.Attachment):
    if not file.filename.lower().endswith((".csv", ".txt")):
        await interaction.response.send_message("Please attach a .csv or .txt file.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        text = (await file.read()).decode("utf-8-sig")
    except Exception as e:
        await interaction.followup.send(f"Could not read attachment: {e}", ephemeral=True)
        return

    rows = [row for row in csv.reader(StringIO(text)) if row and any(cell.strip() for cell in row)]
    if rows and rows[0][0].strip().lower() in ("team_name", "team", "name"):
        rows = rows[1:]  # optional header row

    created = 0
    errors: list[str] = []

    for line_number, row in enumerate(rows, start=1):
        name = row[0].strip()
        if not name:
            continue
        try:
            cursor.execute("INSERT INTO teams (team_name) VALUES (?)", (name,))
            created += 1
        except sqlite3.IntegrityError:
            errors.append(f"Row {line_number}: team **{name}** already exists.")

    conn.commit()

    summary = f"Bulk team creation: {created} created"
    summary += " ✅" if not errors else f", {len(errors)} skipped:\n" + "\n".join(errors[:15])
    if len(errors) > 15:
        summary += f"\n...and {len(errors) - 15} more."

    await interaction.followup.send(summary[:2000], ephemeral=True)

@tree.command(
    name="team_bulk_remove",
    description="Bulk delete teams from a CSV/text file (one team name per line). Members keep their results.",
    guild=GUILD
)
@is_admin()
async def _team_bulk_remove(interaction: Interaction, file: discord.Attachment):
    try:
        text = (await file.read()).decode("utf-8-sig")
    except Exception as e:
        await interaction.response.send_message(f"Could not read attachment: {e}", ephemeral=True)
        return

    rows = [row for row in csv.reader(StringIO(text)) if row and any(cell.strip() for cell in row)]
    if rows and rows[0][0].strip().lower() in ("team_name", "team", "name"):
        rows = rows[1:]  # optional header row

    names = [row[0].strip() for row in rows if row and row[0].strip()]
    if not names:
        await interaction.response.send_message("No team names found in file.", ephemeral=True)
        return

    view = ConfirmView(interaction.user.id)
    await interaction.response.send_message(
        f"Delete {len(names)} team(s)? Members keep their individual results but lose the team tag in every event.",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if not view.confirmed:
        return

    deleted = 0
    not_found: list[str] = []

    for name in names:
        cursor.execute("SELECT id FROM teams WHERE team_name = ?", (name,))
        team = cursor.fetchone()
        if not team:
            not_found.append(name)
            continue
        cursor.execute("UPDATE results SET team_id = NULL WHERE team_id = ?", (team[0],))
        cursor.execute("DELETE FROM teams WHERE id = ?", (team[0],))
        deleted += 1

    conn.commit()

    summary = f"Deleted {deleted} team(s)."
    if not_found:
        shown = ", ".join(not_found[:15])
        summary += f" Not found: {shown}" + (f" (+{len(not_found) - 15} more)" if len(not_found) > 15 else "")

    await interaction.followup.send(summary[:2000], ephemeral=True)

@tree.command(
    name="team_bulk_update",
    description="Bulk rename teams from a CSV/text file (old_name,new_name).",
    guild=GUILD
)
@is_allowed()
async def _team_bulk_update(interaction: Interaction, file: discord.Attachment):
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        text = (await file.read()).decode("utf-8-sig")
    except Exception as e:
        await interaction.followup.send(f"Could not read attachment: {e}", ephemeral=True)
        return

    rows = [row for row in csv.reader(StringIO(text)) if row and any(cell.strip() for cell in row)]
    if rows and rows[0][0].strip().lower() in ("old_name", "team_name", "team"):
        rows = rows[1:]  # optional header row

    updated = 0
    errors: list[str] = []

    for line_number, row in enumerate(rows, start=1):
        if len(row) < 2:
            errors.append(f"Row {line_number}: expected old_name,new_name.")
            continue

        old_name, new_name = row[0].strip(), row[1].strip()
        if not old_name or not new_name:
            errors.append(f"Row {line_number}: both old_name and new_name are required.")
            continue

        cursor.execute("SELECT id FROM teams WHERE team_name = ?", (old_name,))
        team = cursor.fetchone()
        if not team:
            errors.append(f"Row {line_number}: team **{old_name}** not found.")
            continue

        try:
            cursor.execute("UPDATE teams SET team_name = ? WHERE id = ?", (new_name, team[0]))
            updated += 1
        except sqlite3.IntegrityError:
            errors.append(f"Row {line_number}: a team named **{new_name}** already exists.")

    conn.commit()

    summary = f"Bulk rename: {updated} updated"
    summary += " ✅" if not errors else f", {len(errors)} error(s):\n" + "\n".join(errors[:15])
    if len(errors) > 15:
        summary += f"\n...and {len(errors) - 15} more."

    await interaction.followup.send(summary[:2000], ephemeral=True)

@tree.command(
    name="team_bulk_assign",
    description="Bulk assign players to teams for an event from a CSV file (discord_id,team_name).",
    guild=GUILD
)
@is_allowed()
async def _team_bulk_assign(interaction: Interaction, file: discord.Attachment, event_number: int = 0):
    event = await require_event(interaction, event_number)
    if event is None:
        return
    event_id, event_name = event[0], event[2]

    if not file.filename.lower().endswith((".csv", ".txt")):
        await interaction.response.send_message("Please attach a .csv or .txt file.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        text = (await file.read()).decode("utf-8-sig")
    except Exception as e:
        await interaction.followup.send(f"Could not read attachment: {e}", ephemeral=True)
        return

    rows = [row for row in csv.reader(StringIO(text)) if row and any(cell.strip() for cell in row)]
    if rows and rows[0][0].strip().lower() in ("discord_id", "discord id", "id"):
        rows = rows[1:]  # optional header row

    assigned = 0
    errors: list[str] = []
    team_cache: dict[str, Optional[int]] = {}  # avoids re-querying the same team name every row

    for line_number, row in enumerate(rows, start=1):
        if len(row) < 2:
            errors.append(f"Row {line_number}: expected discord_id,team_name.")
            continue

        try:
            discord_id = int(row[0].strip())
        except ValueError:
            errors.append(f"Row {line_number}: discord_id must be a number.")
            continue

        team_name = row[1].strip()
        if not team_name:
            errors.append(f"Row {line_number}: team_name is required.")
            continue

        if team_name not in team_cache:
            cursor.execute("SELECT id FROM teams WHERE team_name = ?", (team_name,))
            team_row = cursor.fetchone()
            team_cache[team_name] = team_row[0] if team_row else None

        team_id = team_cache[team_name]
        if team_id is None:
            errors.append(f"Row {line_number}: team **{team_name}** not found.")
            continue

        cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
        player = cursor.fetchone()
        if not player:
            cursor.execute("INSERT INTO players (discord_id) VALUES (?)", (discord_id,))
            cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
            player = cursor.fetchone()
        player_id = player[0]

        cursor.execute("SELECT id FROM results WHERE player_id = ? AND event_id = ?", (player_id, event_id))
        result = cursor.fetchone()
        if result:
            cursor.execute("UPDATE results SET team_id = ? WHERE id = ?", (team_id, result[0]))
        else:
            cursor.execute(
                "INSERT INTO results (player_id, player_score, event_id, team_id) VALUES (?, 0, ?, ?)",
                (player_id, event_id, team_id)
            )
        assigned += 1

    conn.commit()

    summary = f"Bulk team assignment for **{event_name}**: {assigned} assigned"
    summary += " ✅" if not errors else f", {len(errors)} error(s):\n" + "\n".join(errors[:15])
    if len(errors) > 15:
        summary += f"\n...and {len(errors) - 15} more."

    await interaction.followup.send(summary[:2000], ephemeral=True)

@tree.command(
    name="team_bulk_unassign",
    description="Bulk remove players from their teams for an event from a CSV file (one discord_id per line).",
    guild=GUILD
)
@is_allowed()
async def _team_bulk_unassign(interaction: Interaction, file: discord.Attachment, event_number: int = 0):
    event = await require_event(interaction, event_number)
    if event is None:
        return
    event_id, event_name = event[0], event[2]

    try:
        text = (await file.read()).decode("utf-8-sig")
    except Exception as e:
        await interaction.response.send_message(f"Could not read attachment: {e}", ephemeral=True)
        return

    rows = [row for row in csv.reader(StringIO(text)) if row and any(cell.strip() for cell in row)]
    if rows and rows[0][0].strip().lower() in ("discord_id", "discord id", "id"):
        rows = rows[1:]  # optional header row

    discord_ids: list[int] = []
    skipped = 0
    for row in rows:
        try:
            discord_ids.append(int(row[0].strip()))
        except (ValueError, IndexError):
            skipped += 1

    if not discord_ids:
        await interaction.response.send_message("No valid discord_id values found in file.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True, ephemeral=True)

    unassigned = 0
    for discord_id in discord_ids:
        cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
        player = cursor.fetchone()
        if not player:
            continue
        cursor.execute(
            "UPDATE results SET team_id = NULL WHERE player_id = ? AND event_id = ?",
            (player[0], event_id)
        )
        unassigned += cursor.rowcount

    conn.commit()

    summary = f"Removed team assignment for {unassigned} player(s) in **{event_name}**"
    if skipped:
        summary += f" ({skipped} row(s) couldn't be parsed and were skipped)"
    summary += " ✅"

    await interaction.followup.send(summary[:2000], ephemeral=True)

@tree.command(
    name="season_current",
    description="Show info about the current season.",
    guild=GUILD
)
@is_allowed()
async def _season_current(interaction: Interaction):
    season_id = get_active_season_id()
    cursor.execute("SELECT season_number, started_at FROM seasons WHERE id = ?", (season_id,))
    season_number, started_at = cursor.fetchone()

    cursor.execute("SELECT COUNT(*) FROM events WHERE season_id = ?", (season_id,))
    event_count = cursor.fetchone()[0]

    embed = Embed(
        title=f"Season {season_number} (active)",
        description=f"Started: {started_at}\nEvents so far: {event_count}"
    )
    await interaction.response.send_message(embed=embed)

@tree.command(
    name="season_list",
    description="List every season, past and current.",
    guild=GUILD
)
@is_allowed()
async def _season_list(interaction: Interaction):
    cursor.execute("SELECT season_number, started_at, ended_at FROM seasons ORDER BY id")
    seasons = cursor.fetchall()

    if not seasons:
        await interaction.response.send_message("No seasons yet.", ephemeral=True)
        return

    desc = "\n".join(
        f"**Season {number}** - {started} → {ended or 'ongoing'}"
        for number, started, ended in seasons
    )
    await interaction.response.send_message(embed=Embed(title="Seasons", description=desc))

@tree.command(
    name="season_leaderboard",
    description="Cumulative leaderboard for a season (default: the current one).",
    guild=GUILD
)
@is_allowed()
async def _season_leaderboard(interaction: Interaction, season_number: int = 0, top: int = 10):
    if top <= 0:
        await interaction.response.send_message("top must be greater than 0.", ephemeral=True)
        return

    if season_number == 0:
        season_id = get_active_season_id()
    else:
        cursor.execute("SELECT id FROM seasons WHERE season_number = ?", (season_number,))
        row = cursor.fetchone()
        if not row:
            await interaction.response.send_message(f"Season {season_number} not found.", ephemeral=True)
            return
        season_id = row[0]

    cursor.execute("SELECT season_number, ended_at FROM seasons WHERE id = ?", (season_id,))
    resolved_number, ended_at = cursor.fetchone()

    cursor.execute("""
        SELECT players.discord_id, SUM(results.player_score) as total_score
        FROM results
        JOIN players ON results.player_id = players.id
        JOIN events ON results.event_id = events.id
        WHERE events.season_id = ?
        GROUP BY results.player_id
        ORDER BY total_score DESC
        LIMIT ?
    """, (season_id, top))
    leaderboard = cursor.fetchall()

    desc = "".join(placement_line(i, discord_id, score) for i, (discord_id, score) in enumerate(leaderboard))
    status = "ongoing" if ended_at is None else f"ended {ended_at}"
    embed = Embed(
        title=f"🏆 Season {resolved_number} Leaderboard - TOP {top} ({status})",
        description="No results found." if desc == "" else desc
    )
    await interaction.response.send_message(embed=embed)

@tree.command(
    name="season_reset",
    description="Close the current season and start a new one. Nothing is deleted - past events keep their data.",
    guild=GUILD
)
@is_admin()
async def _season_reset(interaction: Interaction, top: int = 10):
    if top <= 0:
        await interaction.response.send_message("top must be greater than 0.", ephemeral=True)
        return

    season_id = get_active_season_id()
    cursor.execute("SELECT season_number, started_at FROM seasons WHERE id = ?", (season_id,))
    season_number, started_at = cursor.fetchone()

    cursor.execute("""
        SELECT players.discord_id, SUM(results.player_score) as total_score
        FROM results
        JOIN players ON results.player_id = players.id
        JOIN events ON results.event_id = events.id
        WHERE events.season_id = ?
        GROUP BY results.player_id
        ORDER BY total_score DESC
        LIMIT ?
    """, (season_id, top))
    leaderboard = cursor.fetchall()
    desc = "".join(placement_line(i, discord_id, score) for i, (discord_id, score) in enumerate(leaderboard))

    preview_embed = Embed(
        title=f"⚠️ End Season {season_number}?",
        description=(
            f"This closes Season {season_number} (started {started_at}) and opens Season {season_number + 1}. "
            f"No data is deleted - past events and results stay exactly as they are.\n\n"
            f"**Top {top} for this season** (hand out rewards from this manually):\n\n"
            + ("No results found." if desc == "" else desc)
        )
    )

    view = ConfirmView(interaction.user.id)
    await interaction.response.send_message(embed=preview_embed, view=view, ephemeral=True)
    await view.wait()

    if not view.confirmed:
        return

    cursor.execute("UPDATE seasons SET ended_at = ? WHERE id = ?", (datetime.now().isoformat(), season_id))
    cursor.execute("INSERT INTO seasons (season_number) VALUES (?)", (season_number + 1,))
    conn.commit()

    final_embed = Embed(
        title=f"🏁 Season {season_number} Final Standings - TOP {top}",
        description="No results found." if desc == "" else desc
    )
    await interaction.followup.send(
        content=f"Season {season_number} closed. Season {season_number + 1} is now active.",
        embed=final_embed
    )

@tree.error
async def on_app_command_error(interaction: Interaction, error):
    if isinstance(error, app_commands.errors.CheckFailure):
        await interaction.response.send_message(
            "You don't have permission to use this command.",
            ephemeral=True
        )
    else:
        print(f"Unhandled app command error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Something went wrong running that command.",
                ephemeral=True
            )

### BOT INIT ###

REMINDER_WINDOW = timedelta(hours=1)

@tasks.loop(minutes=1)
async def reminder_loop():
    now = datetime.now()
    window_end = now + REMINDER_WINDOW

    try:
        cursor.execute("""
            SELECT id, event_name, event_start_time FROM events
            WHERE event_start_time IS NOT NULL AND reminder_sent = 0
        """)
        candidates = cursor.fetchall()
    except sqlite3.Error as e:
        print(f"reminder_loop: could not query events, skipping this tick: {e}")
        return

    for event_id, event_name, start_time_str in candidates:
        try:
            start_time = datetime.strptime(start_time_str, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue

        if not (now <= start_time <= window_end):
            continue

        try:
            cursor.execute("""
                SELECT players.discord_id FROM results
                JOIN players ON results.player_id = players.id
                WHERE results.event_id = ?
            """, (event_id,))
            discord_ids = [row[0] for row in cursor.fetchall()]

            for discord_id in discord_ids:
                try:
                    user = await bot.fetch_user(discord_id)
                    await user.send(f"⏰ **{event_name}** starts in about an hour - get ready!")
                except (discord.Forbidden, discord.NotFound) as e:
                    print(f"Could not DM reminder to {discord_id}: {e}")

            cursor.execute("UPDATE events SET reminder_sent = 1 WHERE id = ?", (event_id,))
            conn.commit()

            print(f"Sent reminders for event {event_id} ({event_name}) to {len(discord_ids)} player(s).")
        except sqlite3.Error as e:
            # Don't let one bad event stop the rest of the events in this tick,
            # or crash the loop entirely (which would silently kill all future
            # reminders for the process's lifetime).
            print(f"reminder_loop: failed processing event {event_id} ({event_name}): {e}")
            continue

@reminder_loop.error
async def reminder_loop_error(error: Exception):
    # Safety net: if something outside the try/except above still slips
    # through (e.g. a Discord API outage), log it loudly instead of letting
    # discord.py silently stop the loop, and restart it so reminders keep
    # working rather than going dark for the rest of the process's life.
    print(f"reminder_loop crashed unexpectedly, restarting it: {error}")
    if not reminder_loop.is_running():
        reminder_loop.restart()

@reminder_loop.before_loop
async def before_reminder_loop():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync(guild=GUILD)
        print(f"{len(synced)} command{'s' if len(synced) != 1 else ''} synchronized.")
    except Exception as e:
        print(f"Could not synchronize commands: {e}")
    print(f"Bot is ready: {bot.user.name}")

    # Re-register the Join Event button so it keeps working after a restart.
    # on_ready can fire more than once (e.g. after a reconnect), so guard
    # against re-adding the same view repeatedly.
    if not getattr(bot, "_join_view_restored", False):
        active_event_id = bot_data.get("active_event_data", {}).get("active_event_id")
        if active_event_id:
            bot.add_view(JoinEventView(active_event_id))
            print(f"Restored Join Event view for event {active_event_id} ✅")
        bot._join_view_restored = True

    if not reminder_loop.is_running():
        reminder_loop.start()

    print("Fetching division members...")
    try:
        current_guild: discord.Guild = bot.get_guild(GUILD_ID)

        division_members = [
            member for member in current_guild.members
            if any(role.id in MEMBER_ROLES for role in member.roles)
        ]

        for member in division_members:
            cursor.execute("SELECT id FROM players WHERE discord_id = ?", (member.id,))
            player = cursor.fetchone()

            if not player:
                cursor.execute("INSERT INTO players (discord_id) VALUES (?)", (member.id,))
                conn.commit()

        print("Player database updated ✅")
    except Exception as e:
        print(f"Could not fetch division members: {e}")

@bot.event
async def on_disconnect():
    # NOTE: this fires on every websocket drop, including the brief blips
    # discord.py auto-reconnects from - it does NOT mean the bot is shutting
    # down. Do not close shared resources like the db connection here, or
    # every reconnect after the first disconnect leaves the app running
    # against a closed connection.
    print("The bot is now offline (will attempt to reconnect if this wasn't a shutdown).")

try:
    bot.run(token, log_handler=handler, log_level=logging.DEBUG)
finally:
    # This only runs once the process is actually exiting (bot.run returns
    # or raises), so it's the right place to close the db connection - unlike
    # on_disconnect, which can fire many times over the bot's lifetime.
    conn.close()