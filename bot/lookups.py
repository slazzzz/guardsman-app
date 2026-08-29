### DB LOOKUP HELPERS ###
# These collapse the "find event / player / result or bail out with an error
# message" boilerplate that used to be duplicated across ~8 commands each.

from typing import Optional

from discord import Interaction, Member

from bot.database import conn, cursor
from bot.roblox import get_roblox_id_from_username


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


async def require_drill(interaction: Interaction, drill_number: int) -> Optional[tuple]:
    """Resolves a drill by its 1-indexed position (0 = most recent), same
    convention as require_event. Sends an ephemeral error and returns None if
    drill_number is invalid or nothing matches."""
    if drill_number < 0:
        await interaction.response.send_message("drill_number must be greater than 0.", ephemeral=True)
        return None

    if drill_number != 0:
        cursor.execute("SELECT * FROM drills ORDER BY id LIMIT 1 OFFSET ?", (drill_number - 1,))
    else:
        cursor.execute("SELECT * FROM drills ORDER BY id DESC LIMIT 1")

    drill = cursor.fetchone()

    if not drill:
        await interaction.response.send_message("Drill not found.", ephemeral=True)
        return None

    return drill


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


def upsert_player_roblox_id(discord_id: int, roblox_id: int):
    """Creates the player row if discord_id doesn't have one yet, otherwise
    updates its roblox_id. The single write path for linking a Discord member
    to a Roblox account - used by the verified self-service /roblox_link flow,
    the event-join form, and the staff /player_roblox_id_update command, so
    all three stay consistent instead of each hand-rolling their own
    insert-or-update."""
    cursor.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id,))
    player = cursor.fetchone()

    if player:
        cursor.execute("UPDATE players SET roblox_id = ? WHERE discord_id = ?", (roblox_id, discord_id))
    else:
        cursor.execute("INSERT INTO players (discord_id, roblox_id) VALUES (?, ?)", (discord_id, roblox_id))
    conn.commit()


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