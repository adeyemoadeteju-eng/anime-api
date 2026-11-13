import asyncio
import copy
import hashlib
import json
import time
from collections import OrderedDict

import httpx
from fastapi import HTTPException

from src.config import ANILIST_URL, iter_miruro_pipe_targets
from src.parser import encode_pipe_request, decode_pipe_response, deep_translate


_HTTP_CLIENT = None
_QUERY_CACHE = OrderedDict()
_QUERY_IN_FLIGHT = {}
_QUERY_CACHE_MAX_ENTRIES = 256


def _get_http_client():
    """Reuse one connection pool for AniList requests within this API process."""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        _HTTP_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0, pool=5.0),
            limits=httpx.Limits(
                max_connections=40,
                max_keepalive_connections=20,
                keepalive_expiry=30.0,
            ),
        )
    return _HTTP_CLIENT


async def close_http_client():
    """Close the shared connection pool when the FastAPI process shuts down."""
    global _HTTP_CLIENT
    client = _HTTP_CLIENT
    _HTTP_CLIENT = None
    if client is not None and not client.is_closed:
        await client.aclose()


def _query_ttl(query: str) -> int:
    normalized = " ".join(query.lower().split())
    if "genecollection" in normalized:
        return 24 * 60 * 60
    if "media(id:" in normalized:
        return 30 * 60
    if "page(" in normalized:
        return 3 * 60
    return 60


def _query_cache_key(query: str, variables: dict) -> str:
    value = json.dumps(
        {"query": query, "variables": variables or {}},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _request_anilist(query: str, variables: dict = None):
    body = {"query": query}
    if variables:
        body["variables"] = variables

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Origin": "https://www.miruro.ru",
        "Referer": "https://www.miruro.ru/",
    }

    response = await _get_http_client().post(ANILIST_URL, json=body, headers=headers)
    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="anilist query failed")
    return response.json().get("data", {})


async def _fetch_and_cache(key: str, query: str, variables: dict, ttl: int):
    try:
        data = await _request_anilist(query, variables)
        _QUERY_CACHE[key] = (time.monotonic() + ttl, data)
        _QUERY_CACHE.move_to_end(key)
        while len(_QUERY_CACHE) > _QUERY_CACHE_MAX_ENTRIES:
            _QUERY_CACHE.popitem(last=False)
        return data
    finally:
        current = asyncio.current_task()
        if _QUERY_IN_FLIGHT.get(key) is current:
            _QUERY_IN_FLIGHT.pop(key, None)


async def anilist_query(query: str, variables: dict = None):
    """Query AniList with short-lived caching and duplicate-request coalescing."""
    variables = variables or {}
    key = _query_cache_key(query, variables)
    ttl = _query_ttl(query)
    now = time.monotonic()

    cached = _QUERY_CACHE.get(key)
    if cached:
        expires_at, data = cached
        if expires_at > now:
            _QUERY_CACHE.move_to_end(key)
            return copy.deepcopy(data)
        _QUERY_CACHE.pop(key, None)

    task = _QUERY_IN_FLIGHT.get(key)
    if task is None:
        task = asyncio.create_task(_fetch_and_cache(key, query, variables, ttl))
        _QUERY_IN_FLIGHT[key] = task

    return copy.deepcopy(await asyncio.shield(task))


async def fetch_raw_episodes(anilist_id: int) -> dict:
    payload = {
        "path": "episodes",
        "method": "GET",
        "query": {"anilistId": anilist_id},
        "body": None,
        "version": "0.1.0",
    }
    encoded_req = encode_pipe_request(payload)

    async with httpx.AsyncClient(timeout=15.0) as client:
        last_status = None
        for pipe_target, headers in iter_miruro_pipe_targets(encoded_req):
            print(f"\\n[KUHI API] executing pipe for raw episodes:", pipe_target)
            try:
                res = await client.get(pipe_target, headers=headers)
                last_status = res.status_code
                if res.status_code == 200:
                    data = decode_pipe_response(res.text.strip())
                    deep_translate(data)
                    return data
            except Exception as e:
                print(f"[KUHI API] Failed to connect to {pipe_target}: {e}")

        raise HTTPException(status_code=last_status or 502, detail="pipe request failed")
