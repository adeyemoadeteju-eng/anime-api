"""Extra iframe providers added to the regular playback server list.

AnimeEmbed and AniEmbed accept AniList episode IDs directly. Aanikoto's API
uses its own series IDs, discovered from its recent catalogue or configured
with ANIKOTO_SERIES_MAP. All remote lookups happen server-side and are cached.
"""
import asyncio
import json
import re
import time
from urllib.parse import quote, urlencode, urlparse

import httpx

ANIKOTO_BASE = "https://anikotoapi.site"
ANIMEEMBED_BASE = "https://animeembed.com"
ANIEMBED_BASE = "https://aniembed.se"
VIDY_BASE = "https://www.vidy.st"
RECENT_TTL = 600
SERIES_TTL = 600
REQUEST_TIMEOUT = 3.0

_recent_cache = {"expires": 0.0, "rows": []}
_recent_lock = asyncio.Lock()
_recent_task = None
_series_cache = {}


def normalize_title(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def media_title_aliases(media: dict) -> set[str]:
    title = media.get("title") or {}
    values = list(title.values()) if isinstance(title, dict) else []
    values.extend(media.get("synonyms") or [])
    return {normalized for item in values if (normalized := normalize_title(item))}


def _row_titles(row: dict) -> set[str]:
    values = []
    title = row.get("title")
    if isinstance(title, dict):
        values.extend(title.values())
    elif isinstance(title, str):
        values.append(title)
    anime = row.get("anime")
    if isinstance(anime, dict):
        nested_title = anime.get("title") or anime.get("name")
        if isinstance(nested_title, dict):
            values.extend(nested_title.values())
        elif isinstance(nested_title, str):
            values.append(nested_title)
    values.extend((
        row.get("name"), row.get("title_english"), row.get("title_romaji"),
        row.get("english_title"), row.get("romaji_title"),
    ))
    return {normalized for item in values if (normalized := normalize_title(item))}


def extract_recent_rows(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict) or payload.get("ok") is False:
        return []
    for key in ("results", "anime", "recent_anime", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = extract_recent_rows(value)
            if nested:
                return nested
    return []


def match_recent_series_id(media: dict, rows: list[dict]) -> str | None:
    aliases = media_title_aliases(media)
    if not aliases:
        return None
    found = []
    for row in rows:
        if not (_row_titles(row) & aliases):
            continue
        series_id = row.get("id", row.get("series_id", row.get("anime_id", row.get("animeId"))))
        if series_id is None and isinstance(row.get("anime"), dict):
            series_id = row["anime"].get("id")
        if series_id is not None and str(series_id).strip():
            found.append(str(series_id).strip())
    unique = list(dict.fromkeys(found))
    return unique[0] if len(unique) == 1 else None


def extract_episode_row(payload: object, episode: int) -> dict | None:
    if not isinstance(payload, dict):
        return None
    rows = payload.get("episodes")
    if isinstance(rows, dict):
        values = []
        for key, item in rows.items():
            if isinstance(item, dict):
                item = dict(item)
                item.setdefault("number", key)
            values.append(item)
        rows = values
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        number = row.get("number", row.get("episode", row.get("episode_number")))
        try:
            if int(number) == int(episode):
                return row
        except (TypeError, ValueError):
            continue
    return None


def is_safe_embed_url(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def build_animeembed_url(anilist_id: int, episode: int, audio: str) -> str:
    query = urlencode({
        "track": audio, "autoPlay": "0", "muted": "0",
        "autoSkip": "1", "autoNext": "0", "start": "0",
        "accent": "9b87ff",
    })
    return f"{ANIMEEMBED_BASE}/embed/ani/{int(anilist_id)}/{int(episode)}?{query}"


def build_aniembed_url(anilist_id: int, episode: int, audio: str) -> str:
    query = urlencode({"lang": audio, "autoplay": "0", "t": "0"})
    return f"{ANIEMBED_BASE}/e/{int(anilist_id)}/{int(episode)}?{query}"


def build_vidy_url(anilist_id: int, episode: int) -> str:
    """Build Vidy's documented AniList anime route; autoplay remains off."""
    query = urlencode({
        "color": "9b87ff",
        "nextEpisode": "true",
        "episodeSelector": "true",
    })
    return f"{VIDY_BASE}/anime/{int(anilist_id)}/{int(episode)}?{query}"


async def _fetch_recent_rows() -> list[dict]:
    global _recent_task
    async with _recent_lock:
        if _recent_cache["expires"] > time.monotonic():
            return _recent_cache["rows"]
        task = _recent_task
        if task is None:
            task = asyncio.create_task(_request_recent_rows())
            _recent_task = task
    try:
        return await task
    finally:
        async with _recent_lock:
            if _recent_task is task and task.done():
                _recent_task = None


async def _request_recent_rows() -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(
                f"{ANIKOTO_BASE}/recent-anime",
                params={"page": 1, "per_page": 50},
                headers={"Accept": "application/json", "User-Agent": "AniOrbit/1.0"},
            )
        if response.status_code == 200:
            rows, ttl = extract_recent_rows(response.json()), RECENT_TTL
        else:
            rows, ttl = [], 60
    except (httpx.HTTPError, ValueError, TypeError):
        rows, ttl = [], 60
    async with _recent_lock:
        _recent_cache.update({"expires": time.monotonic() + ttl, "rows": rows})
    return rows


async def _fetch_series(series_id: str) -> dict | None:
    now = time.monotonic()
    cached = _series_cache.get(series_id)
    if cached and cached[0] > now:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(
                f"{ANIKOTO_BASE}/series/{quote(series_id, safe='-_')}",
                headers={"Accept": "application/json", "User-Agent": "AniOrbit/1.0"},
            )
        if response.status_code != 200:
            _series_cache[series_id] = (now + 60, None)
            return None
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("ok") is False:
            payload = None
        _series_cache[series_id] = (now + SERIES_TTL, payload)
        return payload
    except (httpx.HTTPError, ValueError, TypeError):
        _series_cache[series_id] = (now + 60, None)
        return None


def _configured_series_id(anilist_id: int) -> str | None:
    """Optional exact mapping for Aanikoto catalogue titles absent from the recent page."""
    import os
    try:
        mapping = json.loads(os.getenv("ANIKOTO_SERIES_MAP", "{}"))
    except (TypeError, ValueError):
        return None
    if not isinstance(mapping, dict):
        return None
    value = mapping.get(str(anilist_id))
    return str(value).strip() if value is not None and str(value).strip() else None


async def _anikoto_source(media: dict, anilist_id: int, episode: int, audio: str) -> dict | None:
    series_id = _configured_series_id(anilist_id)
    if not series_id:
        series_id = match_recent_series_id(media, await _fetch_recent_rows())
    if not series_id:
        return None
    payload = await _fetch_series(series_id)
    row = extract_episode_row(payload, episode)
    if not row:
        return None
    embed_urls = row.get("embed_url") or row.get("embedUrl") or {}
    url = embed_urls.get(audio) if isinstance(embed_urls, dict) else None
    if not is_safe_embed_url(url):
        return None
    return {
        "name": "Aanikoto API",
        "provider": "anikoto-api",
        "type": "embed",
        "audio": audio,
        "url": url.strip(),
    }


async def get_extra_playback_sources(media: dict, anilist_id: int, episode: int, audio: str) -> list[dict]:
    """Return the added providers in the exact same source format as existing embeds."""
    embed_episode = 1 if media.get("format") == "MOVIE" else episode
    sources = [
        {
            "name": "AnimeEmbed",
            "provider": "animeembed",
            "type": "embed",
            "audio": audio,
            "url": build_animeembed_url(anilist_id, embed_episode, audio),
        },
        {
            "name": "AniEmbed",
            "provider": "aniembed",
            "type": "embed",
            "audio": audio,
            "url": build_aniembed_url(anilist_id, embed_episode, audio),
        },
        {
            "name": "Vidy",
            "provider": "vidy",
            "type": "embed",
            "url": build_vidy_url(anilist_id, embed_episode),
        },
    ]
    try:
        candidate = await asyncio.wait_for(
            _anikoto_source(media, anilist_id, embed_episode, audio),
            timeout=5.5,
        )
        if candidate:
            sources.append(candidate)
    except Exception:
        pass
    return sources
