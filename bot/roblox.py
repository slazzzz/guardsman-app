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
# Avatar caches store (url, cached_at) tuples rather than bare strings so
# freshness can be checked on every read (see _avatar_cache_fresh below), not
# just once at startup in load_roblox_cache() - otherwise an entry loaded (or
# fetched) at process start would be served forever regardless of
# ROBLOX_AVATAR_CACHE_TTL_SECONDS, until the bot restarts.
roblox_avatar_cache: dict[int, tuple[str, datetime]] = {}
roblox_full_avatar_cache: dict[int, tuple[str, datetime]] = {}

ROBLOX_MAX_RETRIES = 3
ROBLOX_DEFAULT_BACKOFF_SECONDS = 1.5

# Avatars change more often than usernames, so unlike roblox_username_cache
# (which never expires), avatar cache entries go stale after this long -
# checked on every read via _avatar_cache_fresh(), not just at startup.
ROBLOX_AVATAR_CACHE_TTL_SECONDS = 30 * 60  # 30 minutes


def _avatar_cache_fresh(entry: Optional[tuple[str, datetime]]) -> bool:
    """True if entry is a (url, cached_at) tuple younger than the TTL. Used by
    every avatar read path so a stale entry - whether it's been sitting in
    memory since this process started or was just loaded from roblox_cache -
    is treated as a cache miss and re-fetched from Roblox."""
    if entry is None:
        return False
    _, cached_at = entry
    return (datetime.now() - cached_at).total_seconds() < ROBLOX_AVATAR_CACHE_TTL_SECONDS


def get_badge_name(badge_id: int) -> Optional[str]:
    """Looks up a badge's display name from its id, so /badge_submit doesn't need
    a hardcoded list of every trackable Pressure badge - staff/players can submit
    any badge id and this fills in the name for the review embed."""
    response = _roblox_request("GET", f"https://badges.roblox.com/v1/badges/{badge_id}")
    if response is None or response.status_code != 200:
        return None
    try:
        return response.json().get("name")
    except ValueError:
        return None


ROBLOX_BADGE_SCAN_PAGE_SIZE = 100
ROBLOX_BADGE_SCAN_MAX_PAGES = 5  # caps the scan at 500 badges so a badge-heavy account can't hang this


def roblox_owns_badge(roblox_id: int, badge_id: int) -> bool:
    """Best-effort check of whether roblox_id owns badge_id, via Roblox's public
    badges.roblox.com list endpoint.

    NOTE: no longer called by /badge_submit (badge role links + a staff-reviewed
    screenshot queue replaced it - see stats.py/config.py's BADGE_ROLE_IDS) since
    this could only ever return a reliable True in the first place. Left here in
    case a future feature wants a best-effort ownership probe again.

    IMPORTANT: this can only return a reliable True. Roblox has repeatedly changed
    whether/how badge visibility respects the "who can see my inventory" privacy
    setting, so a private inventory or a badge outside the scanned page window will
    both come back as "not found" here indistinguishably from genuinely not owning
    it. Callers must treat a False return as "couldn't confirm" and fall back to a
    manual/screenshot review - never as proof the player doesn't have the badge.
    """
    if not roblox_id:
        return False

    cursor_token = None
    for _ in range(ROBLOX_BADGE_SCAN_MAX_PAGES):
        url = (
            f"https://badges.roblox.com/v1/users/{roblox_id}/badges"
            f"?limit={ROBLOX_BADGE_SCAN_PAGE_SIZE}&sortOrder=Desc"
        )
        if cursor_token:
            url += f"&cursor={cursor_token}"

        response = _roblox_request("GET", url)
        if response is None or response.status_code != 200:
            return False

        try:
            payload = response.json()
        except ValueError:
            return False

        if any(entry.get("id") == badge_id for entry in payload.get("data", [])):
            return True

        cursor_token = payload.get("nextPageCursor")
        if not cursor_token:
            break

    return False


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
    """Single-id HEADSHOT lookup, kept for call sites that only need one avatar
    (e.g. leaderboard rows). For rendering a leaderboard, prefer
    get_avatar_urls_batch() instead so multiple players are fetched in one
    Roblox API call. For a full-body avatar (e.g. /profile), use
    get_full_avatar_url() instead."""
    cached = roblox_avatar_cache.get(roblox_id)
    if _avatar_cache_fresh(cached):
        return cached[0]

    urls = get_avatar_urls_batch([roblox_id])
    return urls.get(roblox_id)


def get_full_avatar_url(roblox_id: int) -> Optional[str]:
    """Single-id FULL-BODY avatar lookup (as opposed to get_avatar_url()'s
    headshot crop) - what /profile uses so a member's whole avatar shows up
    instead of just their face."""
    cached = roblox_full_avatar_cache.get(roblox_id)
    if _avatar_cache_fresh(cached):
        return cached[0]

    urls = get_full_avatar_urls_batch([roblox_id])
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
        if _avatar_cache_fresh(cached):
            results[roblox_id] = cached[0]
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


def get_full_avatar_urls_batch(roblox_ids: list[int]) -> dict[int, str]:
    """Same batching/caching strategy as get_avatar_urls_batch(), but against
    Roblox's full-body avatar-render endpoint instead of the headshot crop.
    Kept as a separate cache (roblox_full_avatar_cache / full_avatar_url) since
    a headshot and a full-body render are different images with different URLs -
    caching them under the same key would mean whichever was fetched last wins."""
    results: dict[int, str] = {}
    missing: list[int] = []

    seen: set[int] = set()
    for roblox_id in roblox_ids:
        if not roblox_id or roblox_id in seen:
            continue
        seen.add(roblox_id)
        cached = roblox_full_avatar_cache.get(roblox_id)
        if _avatar_cache_fresh(cached):
            results[roblox_id] = cached[0]
        else:
            missing.append(roblox_id)

    for i in range(0, len(missing), ROBLOX_AVATAR_BATCH_SIZE):
        chunk = missing[i:i + ROBLOX_AVATAR_BATCH_SIZE]
        ids_param = ",".join(str(rid) for rid in chunk)
        url = (
            "https://thumbnails.roblox.com/v1/users/avatar"
            f"?userIds={ids_param}&size=420x420&format=Png&isCircular=false"
        )

        response = _roblox_request("GET", url)
        if response is None or response.status_code != 200:
            print(f"Roblox batch full-avatar lookup failed for chunk starting at index {i}")
            continue

        try:
            data = response.json().get("data", [])
        except ValueError as e:
            print(f"Roblox batch full-avatar response wasn't JSON: {e}")
            continue

        for entry in data:
            roblox_id = entry.get("targetId")
            image_url = entry.get("imageUrl")
            if roblox_id and image_url:
                cache_roblox_full_avatar(roblox_id, image_url)
                results[roblox_id] = image_url

    return results


### ROBLOX CACHE PERSISTENCE ###

def load_roblox_cache():
    cursor.execute("SELECT roblox_id, username FROM roblox_cache WHERE username IS NOT NULL")
    for roblox_id, username in cursor.fetchall():
        roblox_username_cache[roblox_id] = username

    # Freshness is enforced on every read via _avatar_cache_fresh(), so loading
    # here just needs to parse the stored timestamp - a stale row still gets
    # loaded, it'll just be treated as a miss (and re-fetched) the first time
    # something asks for it, instead of being silently dropped for the rest
    # of the process's life like an un-cached id would be.
    cursor.execute("SELECT roblox_id, avatar_url, updated_at FROM roblox_cache WHERE avatar_url IS NOT NULL")
    for roblox_id, avatar_url, updated_at in cursor.fetchall():
        try:
            cached_at = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        roblox_avatar_cache[roblox_id] = (avatar_url, cached_at)

    cursor.execute("SELECT roblox_id, full_avatar_url, updated_at FROM roblox_cache WHERE full_avatar_url IS NOT NULL")
    for roblox_id, full_avatar_url, updated_at in cursor.fetchall():
        try:
            cached_at = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        roblox_full_avatar_cache[roblox_id] = (full_avatar_url, cached_at)


def cache_roblox_username(roblox_id: int, username: str):
    roblox_username_cache[roblox_id] = username
    cursor.execute("""
        INSERT INTO roblox_cache (roblox_id, username, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(roblox_id) DO UPDATE SET username = excluded.username, updated_at = CURRENT_TIMESTAMP
    """, (roblox_id, username))
    conn.commit()


def cache_roblox_avatar(roblox_id: int, avatar_url: str):
    roblox_avatar_cache[roblox_id] = (avatar_url, datetime.now())
    cursor.execute("""
        INSERT INTO roblox_cache (roblox_id, avatar_url, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(roblox_id) DO UPDATE SET avatar_url = excluded.avatar_url, updated_at = CURRENT_TIMESTAMP
    """, (roblox_id, avatar_url))
    conn.commit()


def cache_roblox_full_avatar(roblox_id: int, full_avatar_url: str):
    roblox_full_avatar_cache[roblox_id] = (full_avatar_url, datetime.now())
    cursor.execute("""
        INSERT INTO roblox_cache (roblox_id, full_avatar_url, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(roblox_id) DO UPDATE SET full_avatar_url = excluded.full_avatar_url, updated_at = CURRENT_TIMESTAMP
    """, (roblox_id, full_avatar_url))
    conn.commit()


load_roblox_cache()