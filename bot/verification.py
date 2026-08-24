### ROBLOX ACCOUNT VERIFICATION ###
# Proves a member actually owns the Roblox account they're linking, instead of
# blindly trusting typed text - without this, anyone could type someone else's
# username/ID into /roblox_link or the event join form and start accumulating
# stats/results under that account.
#
# Flow: generate a short code, ask the member to paste it into their Roblox
# profile's About/description box, then read it back via the Roblox API
# before accepting the link. This deliberately doesn't depend on Bloxlink or
# any other third-party verification service - it only needs the public
# Roblox API bot.roblox already talks to.
#
# Deliberately NOT used by staff-facing commands (/player_roblox_id_update,
# the bulk CSV commands) - a staff member linking someone on their behalf is
# already a trusted override, not a self-report that could be impersonation.

import random
import string
from datetime import datetime, timedelta
from typing import Optional

from bot.database import conn, cursor
from bot.roblox import _roblox_request

VERIFICATION_CODE_LENGTH = 8
VERIFICATION_TTL = timedelta(minutes=15)


def _generate_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=VERIFICATION_CODE_LENGTH))


def start_verification(discord_id: int, roblox_id: int) -> str:
    """Creates (or replaces) a pending verification for discord_id, returns the
    code they need to paste into their Roblox profile's About section.
    Replacing rather than stacking means only the most recent /roblox_link or
    event-join attempt is ever live for a given member."""
    code = _generate_code()
    cursor.execute("""
        INSERT INTO roblox_verifications (discord_id, roblox_id, code, created_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(discord_id) DO UPDATE SET
            roblox_id = excluded.roblox_id, code = excluded.code, created_at = CURRENT_TIMESTAMP
    """, (discord_id, roblox_id, code))
    conn.commit()
    return code


def _get_pending(discord_id: int) -> Optional[tuple[int, str]]:
    """Returns (roblox_id, code) for discord_id's pending verification, or None
    if there isn't one or it's expired (an expired row is deleted here so it
    doesn't linger)."""
    cursor.execute(
        "SELECT roblox_id, code, created_at FROM roblox_verifications WHERE discord_id = ?",
        (discord_id,)
    )
    row = cursor.fetchone()
    if not row:
        return None

    roblox_id, code, created_at = row
    try:
        created = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        clear_verification(discord_id)
        return None

    if datetime.now() - created > VERIFICATION_TTL:
        clear_verification(discord_id)
        return None

    return roblox_id, code


def clear_verification(discord_id: int):
    cursor.execute("DELETE FROM roblox_verifications WHERE discord_id = ?", (discord_id,))
    conn.commit()


def get_roblox_description(roblox_id: int) -> Optional[str]:
    """Fetches a Roblox account's profile description (the About box), used to
    check for a pasted verification code. Returns None on any lookup failure
    (private/deleted account, Roblox API hiccup, etc.)."""
    response = _roblox_request("GET", f"https://users.roblox.com/v1/users/{roblox_id}")
    if response is None or response.status_code != 200:
        return None
    try:
        return response.json().get("description")
    except ValueError:
        return None


def verify_pending(discord_id: int) -> tuple[bool, Optional[int], Optional[str]]:
    """Checks whether discord_id's pending verification code is present in
    their Roblox profile description. Returns (success, roblox_id, error) -
    error is a user-facing message to show when success is False. Clears the
    pending row on success so the same code can't be reused."""
    pending = _get_pending(discord_id)
    if pending is None:
        return False, None, "No pending verification found (it may have expired) - run the link command again."

    roblox_id, code = pending
    description = get_roblox_description(roblox_id)
    if description is None:
        return False, None, "Couldn't reach the Roblox API to check your profile - try again in a moment."

    if code not in description:
        return False, None, f"Didn't find the code `{code}` in your Roblox profile's About section yet - add it and try again."

    clear_verification(discord_id)
    return True, roblox_id, None