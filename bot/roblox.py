### ROBLOX INTEGRATION ###
# 429-aware request helper, username/avatar lookups, and the persistent cache
# backing them (in-memory dicts for speed, mirrored into the roblox_cache
# table so a restart doesn't mean re-hitting the Roblox API for every player).

import time
from datetime import datetime
from typing import Optional

import requests

from bot.database import conn, cursor

roblox_username_cache: dict[int, str] = {}
roblox_avatar_cache: dict[int, str] = {}

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


### ROBLOX CACHE PERSISTENCE ###

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
