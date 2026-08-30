### LEADERBOARD RENDERING ###

from io import BytesIO
from typing import Optional

import asyncio
import discord
import requests
from discord import Embed, Interaction
from PIL import Image, ImageDraw, ImageFont

from bot.client import bot
from bot.config import (
    DEFAULT_PLACEMENT_COLOR,
    DRILL_LEADERBOARD_TYPES,
    FALLBACK_AVATAR_PATH,
    FONT_PATH,
    PLACEMENT_COLORS,
    STAT_TYPES,
)
from bot.database import cursor
from bot.helpers import placement_line
from bot.roblox import get_avatar_urls_batch


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


def leaderboard_image(
    player_results: list[tuple],
    display_names: dict[int, str],
    title: str = "Leaderboard",
    unit: str = "pt",
) -> discord.File:
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

        # Score, right-aligned. unit is singular (e.g. "Win") and gets
        # pluralized the same way placement_line() does for the embed version.
        score_text = f"{score:,} {unit}{'s' if score != 1 else ''}"
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


### PLAYER STAT LEADERBOARDS ###
# These sit alongside the event-leaderboard helpers above and reuse the same
# leaderboard_image()/post_or_update_leaderboard_message() building blocks -
# a stat leaderboard is really just a differently-sourced set of (discord_id,
# value, roblox_id) rows.

def get_stat_leaderboard_rows(stat_type: str, limit: int = 10) -> list[tuple[int, int, int]]:
    """Top `limit` players for stat_type, highest value first, as
    (discord_id, value, roblox_id) - the same shape leaderboard_image() expects."""
    cursor.execute("""
        SELECT players.discord_id, player_stats.value, players.roblox_id
        FROM player_stats
        JOIN players ON players.id = player_stats.player_id
        WHERE player_stats.stat_type = ?
        ORDER BY player_stats.value DESC
        LIMIT ?
    """, (stat_type, limit))
    return cursor.fetchall()


### DRILL HOSTING LEADERBOARDS ###
# Same (discord_id, value, roblox_id) row shape as get_stat_leaderboard_rows()
# above, but aggregated live from the drills/drill_participants tables
# instead of read from player_stats - see config.DRILL_LEADERBOARD_TYPES for
# the categories this covers, and get_host_stats() in bot/drills.py for the
# equivalent per-host figures shown on /guardsman_profile.

def get_drill_leaderboard_rows(leaderboard_type: str, limit: int = 10) -> list[tuple[int, int, int]]:
    """Top `limit` hosts for a drill-derived leaderboard category. roblox_id
    is looked up via a LEFT JOIN since hosting a drill never requires a
    linked Roblox account (unlike drill_participants below, drills.host_discord_id
    isn't a player_id FK) - an unlinked host still shows up, just with the
    fallback avatar, same as an unlinked player anywhere else."""
    if leaderboard_type == "drills_hosted":
        cursor.execute("""
            SELECT d.host_discord_id, COUNT(*), COALESCE(p.roblox_id, 0)
            FROM drills d
            LEFT JOIN players p ON p.discord_id = d.host_discord_id
            GROUP BY d.host_discord_id
            ORDER BY 2 DESC
            LIMIT ?
        """, (limit,))
    elif leaderboard_type == "drills_completed":
        cursor.execute("""
            SELECT d.host_discord_id, COUNT(*), COALESCE(p.roblox_id, 0)
            FROM drills d
            LEFT JOIN players p ON p.discord_id = d.host_discord_id
            WHERE d.status = 'completed'
            GROUP BY d.host_discord_id
            ORDER BY 2 DESC
            LIMIT ?
        """, (limit,))
    elif leaderboard_type == "participants_mobilized":
        cursor.execute("""
            SELECT d.host_discord_id, COUNT(*), COALESCE(p.roblox_id, 0)
            FROM drill_participants dp
            JOIN drills d ON d.id = dp.drill_id
            LEFT JOIN players p ON p.discord_id = d.host_discord_id
            GROUP BY d.host_discord_id
            ORDER BY 2 DESC
            LIMIT ?
        """, (limit,))
    else:
        return []
    return cursor.fetchall()


def leaderboard_type_label(leaderboard_type: str) -> Optional[tuple[str, str]]:
    """Resolves a leaderboard key to its (label, unit), checking STAT_TYPES
    then DRILL_LEADERBOARD_TYPES - the single lookup every rendering
    function below goes through, so a new category only needs adding to one
    of those two dicts, never taught to each call site separately."""
    if leaderboard_type in STAT_TYPES:
        return STAT_TYPES[leaderboard_type]
    if leaderboard_type in DRILL_LEADERBOARD_TYPES:
        return DRILL_LEADERBOARD_TYPES[leaderboard_type]
    return None


def get_leaderboard_rows(leaderboard_type: str, limit: int = 10) -> list[tuple[int, int, int]]:
    """Same dispatch as leaderboard_type_label() - routes to whichever
    table actually backs this leaderboard key."""
    if leaderboard_type in STAT_TYPES:
        return get_stat_leaderboard_rows(leaderboard_type, limit)
    if leaderboard_type in DRILL_LEADERBOARD_TYPES:
        return get_drill_leaderboard_rows(leaderboard_type, limit)
    return []


async def resolve_display_names_for_guild(guild: Optional[discord.Guild], user_ids: list[int]) -> dict[int, str]:
    """Same job as resolve_display_names(), but usable from contexts with no
    Interaction (e.g. the background auto-update loop in tasks.py)."""
    names: dict[int, str] = {}
    for user_id in user_ids:
        member = guild.get_member(user_id) if guild else None
        if member:
            names[user_id] = member.display_name
            continue
        try:
            user = await bot.fetch_user(user_id)
            names[user_id] = user.name
        except discord.NotFound:
            names[user_id] = f"Unknown ({user_id})"
    return names


async def build_stat_leaderboard_embed(guild: Optional[discord.Guild], leaderboard_type: str) -> Optional[Embed]:
    """Builds the embed shown in an auto-updating leaderboard channel, on
    /leaderboard_stats_image, and by the /leaderboard browser (bot/ui.py's
    LeaderboardBrowserView). leaderboard_type can be any STAT_TYPES key
    (player_stats-backed) or DRILL_LEADERBOARD_TYPES key (aggregated live
    from the drills tables) - see leaderboard_type_label()/get_leaderboard_rows()
    above for the dispatch. Returns None for an unrecognized key so callers
    can report that cleanly."""
    label_unit = leaderboard_type_label(leaderboard_type)
    if label_unit is None:
        return None
    label, unit = label_unit

    rows = get_leaderboard_rows(leaderboard_type)
    names = await resolve_display_names_for_guild(guild, [row[0] for row in rows])

    embed = Embed(title=f"🏆 {label}", color=discord.Color.gold())
    if not rows:
        embed.description = "Nothing on record yet - be the first!"
        return embed

    embed.description = "".join(
        placement_line(i, discord_id, value, label=unit)
        for i, (discord_id, value, _roblox_id) in enumerate(rows)
    )
    return embed


async def build_stat_leaderboard_image(guild: Optional[discord.Guild], leaderboard_type: str) -> Optional[discord.File]:
    """The on-request PNG version (e.g. from /profile or /leaderboard_stats_image),
    also usable from the auto-update loop for boards with use_image = 1 - see
    stats.py and tasks.py. Same STAT_TYPES-or-DRILL_LEADERBOARD_TYPES dispatch
    as build_stat_leaderboard_embed() above. leaderboard_image() makes blocking
    Roblox API/image requests, so it's run in a worker thread here (once, for
    every caller) rather than each call site having to remember to do that itself."""
    label_unit = leaderboard_type_label(leaderboard_type)
    if label_unit is None:
        return None
    label, unit = label_unit

    rows = get_leaderboard_rows(leaderboard_type)
    names = await resolve_display_names_for_guild(guild, [row[0] for row in rows])
    return await asyncio.to_thread(leaderboard_image, rows, names, label, unit)