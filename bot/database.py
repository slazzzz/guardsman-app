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

# One row per (player, stat_type) - this is the *verified*, display-ready
# value shown on profiles/leaderboards. source distinguishes how it got here
# ('manual' = approved from a submission, 'admin' = trusted-admin direct add,
# 'auto' = reserved for a future Open Cloud pull, unused today) since a
# profile may want to show that distinction later even though all three
# currently render identically.
cursor.execute("""
CREATE TABLE IF NOT EXISTS player_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER,
    stat_type TEXT,
    value INTEGER DEFAULT 0,
    source TEXT DEFAULT 'manual',
    verified_by INTEGER,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY(player_id) REFERENCES players(id),
    UNIQUE(player_id, stat_type)
)
""")

# The approval queue. A row here is pending review until a staff member hits
# Approve/Reject on the message it was posted with - approval is what copies
# the value into player_stats, this table itself is just history/audit trail.
cursor.execute("""
CREATE TABLE IF NOT EXISTS stat_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER,
    stat_type TEXT,
    value INTEGER,
    proof_url TEXT,
    status TEXT DEFAULT 'pending',
    reviewed_by INTEGER,
    submitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TEXT,

    FOREIGN KEY(player_id) REFERENCES players(id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS player_badges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER,
    badge_name TEXT,
    awarded_by INTEGER,
    awarded_at TEXT DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY(player_id) REFERENCES players(id),
    UNIQUE(player_id, badge_name)
)
""")

# Queue for /badge_submit when roblox_owns_badge() can't confirm ownership
# outright (private inventory or a badge the scan didn't reach) - same
# approve/reject shape as stat_submissions, kept separate since a badge
# carries a badge_id/name instead of a numeric value.
cursor.execute("""
CREATE TABLE IF NOT EXISTS badge_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER,
    badge_id INTEGER,
    badge_name TEXT,
    proof_url TEXT,
    status TEXT DEFAULT 'pending',
    reviewed_by INTEGER,
    submitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TEXT,

    FOREIGN KEY(player_id) REFERENCES players(id)
)
""")

# Config for the auto-updating leaderboard channels - one row per stat_type
# that's been set up with /leaderboard_stats_setup. message_id/last_updated_at
# are maintained by the bot; the rest is set once by staff.
cursor.execute("""
CREATE TABLE IF NOT EXISTS stat_leaderboards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stat_type TEXT UNIQUE,
    channel_id INTEGER,
    message_id INTEGER,
    update_interval_minutes INTEGER DEFAULT 60,
    last_updated_at TEXT
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
ensure_column("player_badges", "source", "TEXT DEFAULT 'manual'")  # 'manual' (admin), 'role' (badge_role_ids match, via /badge_submit or /badge_role_sync), 'submitted' (staff-approved from the screenshot queue)
# Groups the rows a single /stat_submit call created (one row per stat filled
# in) so staff can approve/reject them together with one button press instead
# of once per stat. NULL for rows submitted before this existed - those are
# treated as their own single-row batch (see app.py's restore query).
ensure_column("stat_submissions", "batch_id", "INTEGER")
# Full-body avatar image, cached separately from the headshot in avatar_url
# (leaderboards use the headshot, /profile uses this one).
ensure_column("roblox_cache", "full_avatar_url", "TEXT")
# Lets staff pause an auto-updating leaderboard (/leaderboard_stats_disable)
# without deleting its row - stat_leaderboard_loop skips anything with
# enabled = 0. Deleting the posted message alone doesn't stop the loop, since
# it just posts a fresh one next tick - this column is the actual off switch.
ensure_column("stat_leaderboards", "enabled", "INTEGER DEFAULT 1")
# Opt-in per board: render as a PNG (leaderboard_image(), heavier - Roblox
# avatar fetches + a Pillow render every tick) instead of the default text
# embed. Off by default since a busy board on a short interval can add up.
ensure_column("stat_leaderboards", "use_image", "INTEGER DEFAULT 0")


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