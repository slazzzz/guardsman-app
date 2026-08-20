### LIBRARIES ###

import csv
import discord
import json
import logging
import os
import requests
import sqlite3

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

def get_roblox_id_from_username(username: str) -> Optional[int]:
    if username in roblox_username_cache.values():
        return next((k for k, v in roblox_username_cache.items() if v == username), None)

    url = "https://users.roblox.com/v1/usernames/users"

    # The API expects a list of usernames
    payload = {
        "usernames": [username],
        "excludeBannedUsers": True
    }

    try:
        response = requests.post(url, json=payload, timeout=5)
    except requests.RequestException as e:
        print(f"Roblox username lookup failed: {e}")
        return None

    if response.status_code == 200:
        data = response.json().get("data")
        if data:
            roblox_id = data[0].get("id")
            cache_roblox_username(roblox_id, username)
            return roblox_id

    return None

def get_avatar_url(roblox_id: int) -> Optional[str]:
    if roblox_avatar_cache.get(roblox_id):
        return roblox_avatar_cache[roblox_id]

    url = f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={roblox_id}&size=150x150&format=Png&isCircular=false"

    try:
        res = requests.get(url, timeout=5).json()
        image_url = res["data"][0]["imageUrl"]
    except (requests.RequestException, KeyError, IndexError) as e:
        print(f"Roblox avatar lookup failed for {roblox_id}: {e}")
        return None

    cache_roblox_avatar(roblox_id, image_url)
    return image_url

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

def leaderboard_image(player_results: list[tuple], display_names: dict[int, str]) -> discord.File:
    """Renders a leaderboard PNG entirely in memory (no shared file on disk, so concurrent
    calls can't clobber each other) and returns it as a ready-to-send discord.File.

    display_names must be resolved by the caller beforehand (bot.get_user() can return
    None for uncached users, which used to crash this function).
    """
    img = Image.new("RGB", (800, 600), color=(20, 20, 20))
    draw = ImageDraw.Draw(img)

    font_big = load_font(40)
    font_small = load_font(24)

    y_offset = 50

    for i, result in enumerate(player_results):
        user_id, score, roblox_id = result[0], result[1], result[2]

        name = display_names.get(user_id, f"Unknown ({user_id})")

        avatar_url = get_avatar_url(roblox_id) if roblox_id else None
        avatar = None

        if avatar_url:
            try:
                response = requests.get(avatar_url, timeout=5)
                avatar = Image.open(BytesIO(response.content)).resize((80, 80))
            except (requests.RequestException, OSError) as e:
                print(f"Could not load avatar for {user_id}: {e}")

        if avatar is None:
            avatar = Image.open(FALLBACK_AVATAR_PATH).resize((80, 80))

        img.paste(avatar, (50, y_offset))

        color = PLACEMENT_COLORS.get(i, DEFAULT_PLACEMENT_COLOR)

        draw.text((150, y_offset), f"{i + 1}. {name}", font=font_big, fill=color)
        draw.text((150, y_offset + 40), f"Score: {score}", font=font_small, fill=color)

        y_offset += 100

    buffer = BytesIO()
    img.save(buffer, format="PNG")
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
            "INSERT INTO events (event_date, event_name, event_mode, event_type, event_prize) VALUES (?, ?, ?, ?, ?)",
            (event_date, name, mode, type, prize_pool)
        )
        conn.commit()

        await interaction.response.send_message("Event registered ✅")
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
        channel = interaction.guild.get_channel(LEADERBOARD_CHANNEL_ID)
        if channel:
            await channel.send(embed=embed)

    if generate_image and event_results:
        names = await resolve_display_names(interaction, [row[0] for row in event_results])
        image = leaderboard_image(event_results, names)
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

        await interaction.response.send_message(f"{field} updated ✅")
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

    await interaction.response.send_message(f"Form created for {event_name} ✅")

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
    name="global_leaderboard",
    description="Display a leaderboard compiling results from multiple events.",
    guild=GUILD
)
@is_allowed()
async def _global_leadeboard(interaction: Interaction, first_event_number: int = 1, final_event_number: int = 1, display_in_channel: bool = False, generate_image: bool = False):
    if first_event_number <= 0:
        await interaction.response.send_message("first_event_number must be greater than 0.", ephemeral=True)
        return

    if final_event_number <= 0:
        await interaction.response.send_message("final_event_number must be greater than 0.", ephemeral=True)
        return

    if final_event_number < first_event_number:
        await interaction.response.send_message("final_event_number must be greater than first_event_number.", ephemeral=True)
        return

    cursor.execute("""
        SELECT id FROM events
        ORDER BY id
        LIMIT ? OFFSET ?
    """, (final_event_number - first_event_number + 1, first_event_number - 1))

    event_ids = [row[0] for row in cursor.fetchall()]

    if not event_ids:
        await interaction.response.send_message("No events found.", ephemeral=True)
        return

    placeholders = ",".join("?" for _ in event_ids)

    cursor.execute(f"""
        SELECT players.discord_id, SUM(results.player_score) as total_score, players.roblox_id
        FROM results
        JOIN players ON results.player_id = players.id
        WHERE results.event_id IN ({placeholders})
        GROUP BY results.player_id
        ORDER BY total_score DESC
        LIMIT 10
    """, event_ids)

    leaderboard = cursor.fetchall()

    desc = "".join(placement_line(i, row[0], row[1], label="Point") for i, row in enumerate(leaderboard))

    embed = Embed(
        title=f"🌍 Global Leaderboard - TOP 10 - (Events {first_event_number} → {final_event_number})",
        description="No results found." if desc == "" else desc
    )

    await interaction.response.send_message(embed=embed)

    if display_in_channel:
        channel = interaction.guild.get_channel(LEADERBOARD_CHANNEL_ID)
        if channel:
            await channel.send(embed=embed)

    if generate_image and leaderboard:
        names = await resolve_display_names(interaction, [row[0] for row in leaderboard])
        image = leaderboard_image(leaderboard, names)
        await interaction.followup.send(file=image)

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

        await interaction.response.send_message(f"<@{user_id}> registered ✅")
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

        await interaction.response.send_message(f"Results updated for <@{user.id}> ✅")
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

        await interaction.response.send_message(f"Roblox ID updated for <@{user.id}> ✅")
    except Exception as e:
        print(f"Could not update player results in database: {e}")

        await interaction.response.send_message(f"Error occured: {e}", ephemeral=True)

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

    cursor.execute("""
        SELECT id, event_name, event_start_time FROM events
        WHERE event_start_time IS NOT NULL AND reminder_sent = 0
    """)
    candidates = cursor.fetchall()

    for event_id, event_name, start_time_str in candidates:
        try:
            start_time = datetime.strptime(start_time_str, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue

        if not (now <= start_time <= window_end):
            continue

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
    print("The bot is now offline.")
    conn.close()

bot.run(token, log_handler=handler, log_level=logging.DEBUG)