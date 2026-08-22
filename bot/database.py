### DATABASE SETUP ###

import sqlite3

from bot.config import DB_YEAR

conn = sqlite3.connect(f"database/leaderboard_{DB_YEAR}.db")
cursor = conn.cursor()

# SQLite does NOT enforce FOREIGN KEY constraints by default - it has to be
# turned on per connection, every time. Without this, an insert with a
# player_id/event_id that doesn't actually exist in players/events would
# succeed silently instead of failing loudly.
cursor.execute("PRAGMA foreign_keys = ON")

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
