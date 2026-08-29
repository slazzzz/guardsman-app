### DATABASE SETUP ###

import sqlite3

from bot.config import DB_YEAR

conn = sqlite3.connect(f"database/leaderboard_{DB_YEAR}.db", check_same_thread=False)
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

# A Guardsman Drill's lifecycle: recruiting -> ready -> in_progress -> completed,
# or cancelled from any state before completed. "ready" just means the roster
# hit max_participants - it's informational for the host (shown in the
# embed), it does NOT lock the roster; someone can still leave a "ready"
# drill and it drops back to "recruiting" (see bot/ui.py's DrillRosterView).
#
# host_discord_id is stored directly (not a player_id FK) since hosting
# doesn't require a linked Roblox account, unlike drill_participants below.
#
# vc_mode ('open'/'private') and vc_locked control who can actually CONNECT
# to the drill's voice channel once it exists - see sync_drill_vc_permissions()
# in bot/drills.py for how these combine with drill_vc_overrides and
# drill_banned_users below into the channel's actual permission overwrites.
cursor.execute("""
CREATE TABLE IF NOT EXISTS drills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id INTEGER,
    host_discord_id INTEGER,
    drill_name TEXT,
    drill_size TEXT,
    objective TEXT,
    max_participants INTEGER,
    status TEXT DEFAULT 'recruiting',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    start_time TEXT,
    ended_at TEXT,
    vc_channel_id INTEGER,
    roster_message_id INTEGER,
    roster_channel_id INTEGER,

    FOREIGN KEY(season_id) REFERENCES seasons(id)
)
""")

# One row per (drill, player) - kept even after someone leaves (left_at gets
# set rather than the row being deleted) so participation history survives
# for future features (drill-count achievements, activity leaderboards) - see
# bot/drills.py's get_active_participant_count() for how "currently on the
# roster" is derived from this (left_at IS NULL). completed/result/points_awarded
# are reserved for when per-participant outcome tracking is wired up (not yet -
# /drill_end currently only records an aggregate completed_count, see
# bot/cogs/drills.py) rather than being written today.
cursor.execute("""
CREATE TABLE IF NOT EXISTS drill_participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    drill_id INTEGER,
    player_id INTEGER,
    joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
    left_at TEXT,
    completed INTEGER,
    result TEXT,
    points_awarded INTEGER,

    FOREIGN KEY(drill_id) REFERENCES drills(id),
    FOREIGN KEY(player_id) REFERENCES players(id),
    UNIQUE(drill_id, player_id)
)
""")

# Per-drill voice permission overrides - a host or staff blocking/allowing a
# specific member or role for one drill's VC. target_type distinguishes which
# 'target_id' is (a user's discord_id or a role's id) since both are just
# integers. These win over the drill's vc_mode default (see
# sync_drill_vc_permissions() in bot/drills.py) but lose to a global ban
# below - a host can admit a guest into their own private drill, but can't
# override a division-wide ban.
cursor.execute("""
CREATE TABLE IF NOT EXISTS drill_vc_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    drill_id INTEGER,
    target_type TEXT,
    target_id INTEGER,
    permission TEXT,
    set_by INTEGER,
    set_at TEXT DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY(drill_id) REFERENCES drills(id),
    UNIQUE(drill_id, target_type, target_id)
)
""")

# Division-wide drill VC ban list (staff-managed, /drill_ban and /drill_unban)
# - unlike drill_vc_overrides above, this isn't scoped to one drill. Applied
# as the last, highest-priority layer in sync_drill_vc_permissions() so it
# can't be overridden by a host's own per-drill allow.
cursor.execute("""
CREATE TABLE IF NOT EXISTS drill_banned_users (
    discord_id INTEGER PRIMARY KEY,
    banned_by INTEGER,
    reason TEXT,
    banned_at TEXT DEFAULT CURRENT_TIMESTAMP
)
""")

# A host's own standing block/allow list (/drill_default_block etc.) - copied
# into drill_vc_overrides for a brand new drill at /drill_create time, so a
# host doesn't have to re-block the same troublemaker every time they run a
# drill. It's a COPY, not a live reference: editing a drill's own overrides
# afterward (or editing your defaults later) doesn't touch the other one -
# each drill's overrides are independent from the moment it's created.
cursor.execute("""
CREATE TABLE IF NOT EXISTS drill_host_defaults (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_discord_id INTEGER,
    target_type TEXT,
    target_id INTEGER,
    permission TEXT,
    set_at TEXT DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(host_discord_id, target_type, target_id)
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

# Holds an in-flight "prove you own this Roblox account" attempt - see
# bot/verification.py. One row per discord_id (a fresh /roblox_link attempt
# or event-join attempt overwrites any earlier pending one for that member,
# it doesn't stack). code is what they're asked to paste into their Roblox
# profile's About section; created_at is what verify_pending() checks
# against VERIFICATION_TTL to expire stale attempts.
cursor.execute("""
CREATE TABLE IF NOT EXISTS roblox_verifications (
    discord_id INTEGER PRIMARY KEY,
    roblox_id INTEGER,
    code TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
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
# 'open' (default - normal VC, only global bans/explicit per-drill blocks are
# denied) or 'private' (only roster members + explicit per-drill allows can
# connect - see sync_drill_vc_permissions() in bot/drills.py).
ensure_column("drills", "vc_mode", "TEXT DEFAULT 'open'")
# Independent of vc_mode - when set, denies new connections to everyone
# except explicit per-drill 'allowed' overrides, without touching whoever's
# already in the channel (Discord doesn't disconnect on an overwrite change,
# which is exactly the "stop letting people in, don't kick anyone" behavior
# a lock is supposed to have).
ensure_column("drills", "vc_locked", "INTEGER DEFAULT 0")
# Set at /drill_start (when the VC is actually created) - distinct from
# created_at (when /drill_create was run, which can be well before the drill
# actually happens). Used by /drill_end to reject a proof message that
# predates the drill starting - see bot/cogs/drills.py.
ensure_column("drills", "started_at", "TEXT")
# Where the /drill_end proof message lives - set together, both NULL until a
# drill is actually completed with proof. See the comment above /drill_end
# in bot/cogs/drills.py for the full fact-checking flow these support.
ensure_column("drills", "proof_channel_id", "INTEGER")
ensure_column("drills", "proof_message_id", "INTEGER")


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