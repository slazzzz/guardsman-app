### GENERIC HELPER FUNCTIONS ###
# No dependency on the bot, database, or Discord - safe to import from anywhere.

import json
from typing import Any


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


PLACEMENT_EMOJIS = {0: "🥇", 1: "🥈", 2: "🥉"}
UNIT_EXCLUDE = ["Robux", "%"]


def placement_line(i: int, user_id: int, score: int, label: str = "Point") -> str:
    """Formats a single leaderboard row with medal emoji / ordinal placement."""
    suffix = "s" if score != 1 and label not in UNIT_EXCLUDE else ""
    if i in PLACEMENT_EMOJIS:
        place_word = {0: "1st", 1: "2nd", 2: "3rd"}[i]
        return f"{PLACEMENT_EMOJIS[i]} **{place_word} place** - <@{user_id}> - {score} {label}{suffix}\n"
    return f"**{i + 1}{ordinal(i + 1)} place** - <@{user_id}> - {score} {label}{suffix}\n"


def team_placement_line(i: int, team_name: str, score: int, label: str = "Point") -> str:
    """Same as placement_line, but for a team name instead of a Discord mention."""
    suffix = "s" if score != 1 and label not in UNIT_EXCLUDE else ""
    if i in PLACEMENT_EMOJIS:
        place_word = {0: "1st", 1: "2nd", 2: "3rd"}[i]
        return f"{PLACEMENT_EMOJIS[i]} **{place_word} place** - {team_name} - {score} {label}{suffix}\n"
    return f"**{i + 1}{ordinal(i + 1)} place** - {team_name} - {score} {label}{suffix}\n"
