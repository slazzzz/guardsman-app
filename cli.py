#!/usr/bin/env python3
"""
Ops CLI for the Pressure leaderboard bot.

This is a *separate* tool from app.py. It talks directly to the same SQLite
file and config on disk, so it works even when the bot process is down —
useful once this is hosted remotely and you don't want to SSH in and hand-run
sqlite3 every time you need to check something or the bot won't start.

It intentionally does NOT duplicate the admin slash commands (event_add,
player_results_update, etc.) - those already exist in Discord and require the
bot to be online with guild context anyway. This covers the gap: preflight
checks before a deploy, DB backup/restore, offline stats/export, and log
tailing.

Usage:
    python cli.py preflight
    python cli.py db backup
    python cli.py db list-backups
    python cli.py db restore <backup_file>
    python cli.py db stats
    python cli.py db export -o results.csv
    python cli.py logs tail
    python cli.py logs list
"""

import argparse
import csv
import importlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

ENV_PATH = BASE_DIR / ".env"
BOT_DATA_PATH = BASE_DIR / "bot_data.json"
FONT_PATH = BASE_DIR / "assets" / "font.ttf"
FALLBACK_AVATAR_PATH = BASE_DIR / "anger.png"
DB_DIR = BASE_DIR / "database"
BACKUP_DIR = DB_DIR / "backups"
LOG_DIR = BASE_DIR / "discordlogs"

# NOTE: keep this in sync with DB_YEAR in app.py - it's only duplicated here
# because the CLI has to work even when app.py can't be imported (e.g. no
# DISCORD_TOKEN set yet, or bot_data.json missing during initial setup).
DB_YEAR = 2026

REQUIRED_PACKAGES = ["discord", "requests", "dotenv", "PIL"]

REQUIRED_BOT_DATA_KEYS = {
    "event_data": ["event_modes", "event_types"],
    "form_data": ["form_channel_id"],
    "guild_data": ["main_guild_id", "admin_users", "member_roles", "staff_roles"],
    "leaderboard_data": ["leaderboard_channel_id"],
}


def db_path(year: int) -> Path:
    return DB_DIR / f"leaderboard_{year}.db"


def ok(msg: str):
    print(f"  [OK]   {msg}")


def warn(msg: str):
    print(f"  [WARN] {msg}")


def fail(msg: str):
    print(f"  [FAIL] {msg}")


### PREFLIGHT ###

def cmd_preflight(args):
    print(f"Preflight check - {BASE_DIR}\n")
    failures = 0
    warnings = 0

    # Python version (load_font relies on 3.10+ union type hints)
    if sys.version_info >= (3, 10):
        ok(f"Python {sys.version.split()[0]}")
    else:
        fail(f"Python {sys.version.split()[0]} - app.py needs 3.10+")
        failures += 1

    # Required packages importable
    for pkg in REQUIRED_PACKAGES:
        try:
            importlib.import_module(pkg)
            ok(f"Package '{pkg}' importable")
        except ImportError:
            fail(f"Package '{pkg}' not installed - pip install -r requirements.txt")
            failures += 1

    # .env / token
    if not ENV_PATH.exists():
        fail(f".env not found at {ENV_PATH}")
        failures += 1
    else:
        token_found = False
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line.startswith("DISCORD_TOKEN=") and line.split("=", 1)[1].strip():
                token_found = True
                break
        if token_found:
            ok(".env has DISCORD_TOKEN set")
        else:
            fail(".env exists but DISCORD_TOKEN is missing or empty")
            failures += 1

    # bot_data.json structure
    if not BOT_DATA_PATH.exists():
        fail(f"bot_data.json not found at {BOT_DATA_PATH}")
        failures += 1
    else:
        try:
            bot_data = json.loads(BOT_DATA_PATH.read_text())
            for section, keys in REQUIRED_BOT_DATA_KEYS.items():
                section_data = bot_data.get(section)
                if section_data is None:
                    fail(f"bot_data.json missing top-level key '{section}'")
                    failures += 1
                    continue
                for key in keys:
                    if section_data.get(key) in (None, [], {}):
                        fail(f"bot_data.json['{section}']['{key}'] is missing or empty")
                        failures += 1
            if failures == 0:
                ok("bot_data.json has all required keys")
        except json.JSONDecodeError as e:
            fail(f"bot_data.json is not valid JSON: {e}")
            failures += 1

    # Fallback avatar - hard dependency, no fallback exists for it in app.py
    if FALLBACK_AVATAR_PATH.exists():
        ok("anger.png (fallback avatar) present")
    else:
        fail(f"anger.png missing at {FALLBACK_AVATAR_PATH} - leaderboard image generation will crash whenever a Roblox avatar fetch fails")
        failures += 1

    # Font - soft dependency, app.py falls back to Pillow's default font
    if FONT_PATH.exists():
        ok("assets/font.ttf present")
    else:
        warn(f"assets/font.ttf missing at {FONT_PATH} - will fall back to Pillow's default font (leaderboard images will look worse)")
        warnings += 1

    # database/ and discordlogs/ writable (create if missing)
    for label, path in [("database/", DB_DIR), ("discordlogs/", LOG_DIR)]:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_test"
            probe.write_text("ok")
            probe.unlink()
            ok(f"{label} exists and is writable")
        except OSError as e:
            fail(f"{label} not writable: {e}")
            failures += 1

    print(f"\n{failures} failure(s), {warnings} warning(s).")
    if failures:
        print("Fix the failures above before deploying.")
        sys.exit(1)
    print("Looks good to deploy.")


### DB BACKUP / RESTORE ###

def cmd_db_backup(args):
    source = db_path(args.year)
    if not source.exists():
        fail(f"No database found at {source}")
        sys.exit(1)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    label = f"_{args.label}" if args.label else ""
    dest = BACKUP_DIR / f"leaderboard_{args.year}_{timestamp}{label}.db"

    shutil.copy2(source, dest)
    ok(f"Backed up {source.name} -> {dest.relative_to(BASE_DIR)}")


def cmd_db_list_backups(args):
    if not BACKUP_DIR.exists() or not any(BACKUP_DIR.glob("*.db")):
        print("No backups found.")
        return

    backups = sorted(BACKUP_DIR.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for b in backups:
        size_kb = b.stat().st_size / 1024
        mtime = datetime.fromtimestamp(b.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {b.name}  ({size_kb:.1f} KB, {mtime})")


def cmd_db_restore(args):
    backup_file = BACKUP_DIR / args.backup_file
    if not backup_file.exists():
        # Also allow a full/relative path in case they didn't just pass a bare filename
        backup_file = Path(args.backup_file)
    if not backup_file.exists():
        fail(f"Backup file not found: {args.backup_file}")
        print("Run 'python cli.py db list-backups' to see available backups.")
        sys.exit(1)

    target = db_path(args.year)

    print(f"This will overwrite {target} with {backup_file.name}.")
    if target.exists():
        print("(The current database will be auto-backed-up first, just in case.)")
    confirm = input("Type 'yes' to continue: ").strip().lower()
    if confirm != "yes":
        print("Cancelled.")
        return

    if target.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        safety_copy = BACKUP_DIR / f"leaderboard_{args.year}_pre-restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy2(target, safety_copy)
        ok(f"Safety-copied current DB -> {safety_copy.relative_to(BASE_DIR)}")

    shutil.copy2(backup_file, target)
    ok(f"Restored {backup_file.name} -> {target.name}")


### DB STATS / EXPORT ###

def open_db(year: int) -> sqlite3.Connection:
    path = db_path(year)
    if not path.exists():
        fail(f"No database found at {path}")
        sys.exit(1)
    # Read-only URI connection so this can never accidentally write to a live DB
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def cmd_db_stats(args):
    conn = open_db(args.year)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM players")
    player_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM events")
    event_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM results")
    result_count = cur.fetchone()[0]

    cur.execute("SELECT event_name, event_date FROM events ORDER BY id DESC LIMIT 1")
    latest = cur.fetchone()

    print(f"Database: {db_path(args.year).name}")
    print(f"  Players:  {player_count}")
    print(f"  Events:   {event_count}")
    print(f"  Results:  {result_count}")
    if latest:
        print(f"  Latest event: {latest[0]} ({latest[1]})")

    cur.execute("""
        SELECT players.discord_id, SUM(results.player_score) as total_score
        FROM results
        JOIN players ON results.player_id = players.id
        GROUP BY results.player_id
        ORDER BY total_score DESC
        LIMIT 5
    """)
    top = cur.fetchall()
    if top:
        print("\n  Top 5 (all-time, this DB file):")
        for i, (discord_id, total_score) in enumerate(top, start=1):
            print(f"    {i}. discord_id={discord_id}  total_score={total_score}")

    conn.close()


def cmd_db_export(args):
    conn = open_db(args.year)
    cur = conn.cursor()

    if args.first == 0 and args.final == 0:
        cur.execute("SELECT id FROM events ORDER BY id")
        event_ids = [row[0] for row in cur.fetchall()]
    else:
        first = args.first or 1
        final = args.final or first
        if final < first:
            fail("--final must be >= --first")
            sys.exit(1)
        cur.execute("SELECT id FROM events ORDER BY id LIMIT ? OFFSET ?", (final - first + 1, first - 1))
        event_ids = [row[0] for row in cur.fetchall()]

    if not event_ids:
        print("No events found for that range.")
        conn.close()
        return

    output = Path(args.output) if args.output else BASE_DIR / f"results_export_{args.year}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "event_id", "event_date", "event_name", "event_mode", "event_type", "event_prize",
            "discord_id", "roblox_id", "player_score", "placement"
        ])

        for event_id in event_ids:
            cur.execute(
                "SELECT event_date, event_name, event_mode, event_type, event_prize FROM events WHERE id = ?",
                (event_id,)
            )
            event = cur.fetchone()
            if not event:
                continue
            event_date, event_name, event_mode, event_type, event_prize = event

            cur.execute("""
                SELECT players.discord_id, players.roblox_id, results.player_score
                FROM results
                JOIN players ON results.player_id = players.id
                WHERE results.event_id = ?
                ORDER BY results.player_score DESC
            """, (event_id,))

            for placement, (discord_id, roblox_id, player_score) in enumerate(cur.fetchall(), start=1):
                writer.writerow([
                    event_id, event_date, event_name, event_mode, event_type, event_prize,
                    discord_id, roblox_id, player_score, placement
                ])

    conn.close()
    ok(f"Exported {len(event_ids)} event(s) -> {output}")


### LOGS ###

def latest_log_file():
    if not LOG_DIR.exists():
        return None
    logs = sorted(LOG_DIR.glob("discordlog_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def cmd_logs_list(args):
    if not LOG_DIR.exists() or not any(LOG_DIR.glob("discordlog_*.log")):
        print("No logs found.")
        return
    logs = sorted(LOG_DIR.glob("discordlog_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for log in logs:
        size_kb = log.stat().st_size / 1024
        mtime = datetime.fromtimestamp(log.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {log.name}  ({size_kb:.1f} KB, last modified {mtime})")


def cmd_logs_tail(args):
    target = LOG_DIR / args.file if args.file else latest_log_file()
    if target is None or not target.exists():
        fail("No log file found." if args.file is None else f"Log file not found: {args.file}")
        sys.exit(1)

    with open(target, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    print(f"--- {target.name} (last {min(args.lines, len(lines))} of {len(lines)} lines) ---")
    for line in lines[-args.lines:]:
        print(line.rstrip("\n"))


### ARGPARSE WIRING ###

def build_parser():
    parser = argparse.ArgumentParser(prog="cli.py", description="Ops CLI for the leaderboard bot (works offline, no bot process needed).")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("preflight", help="Check config/env/deps before deploying or starting the bot.").set_defaults(func=cmd_preflight)

    db = sub.add_parser("db", help="Database backup, restore, stats, and export.")
    db_sub = db.add_subparsers(dest="db_command", required=True)

    p = db_sub.add_parser("backup", help="Copy the current DB into database/backups/.")
    p.add_argument("--year", type=int, default=DB_YEAR)
    p.add_argument("--label", default=None, help="Optional label appended to the backup filename.")
    p.set_defaults(func=cmd_db_backup)

    p = db_sub.add_parser("list-backups", help="List available backups.")
    p.set_defaults(func=cmd_db_list_backups)

    p = db_sub.add_parser("restore", help="Restore a backup over the live DB (asks for confirmation).")
    p.add_argument("backup_file", help="Filename from 'db list-backups', or a path to a .db file.")
    p.add_argument("--year", type=int, default=DB_YEAR)
    p.set_defaults(func=cmd_db_restore)

    p = db_sub.add_parser("stats", help="Print player/event/result counts and a quick top-5, read-only.")
    p.add_argument("--year", type=int, default=DB_YEAR)
    p.set_defaults(func=cmd_db_stats)

    p = db_sub.add_parser("export", help="Export results to CSV, same schema as the in-Discord /results_export.")
    p.add_argument("--year", type=int, default=DB_YEAR)
    p.add_argument("-o", "--output", default=None, help="Output CSV path (default: results_export_<year>_<timestamp>.csv)")
    p.add_argument("--first", type=int, default=0, dest="first", help="1-indexed first event (default: whole season)")
    p.add_argument("--final", type=int, default=0, dest="final", help="1-indexed final event (default: whole season)")
    p.set_defaults(func=cmd_db_export)

    logs = sub.add_parser("logs", help="Inspect discordlogs/ without SSH-ing around for filenames.")
    logs_sub = logs.add_subparsers(dest="logs_command", required=True)

    p = logs_sub.add_parser("list", help="List available log files, most recent first.")
    p.set_defaults(func=cmd_logs_list)

    p = logs_sub.add_parser("tail", help="Print the last N lines of a log file (default: the most recent one).")
    p.add_argument("-n", "--lines", type=int, default=50, dest="lines")
    p.add_argument("--file", default=None, help="Specific log filename in discordlogs/ (default: most recent).")
    p.set_defaults(func=cmd_logs_tail)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()