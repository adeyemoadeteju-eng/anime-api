import asyncio
import importlib
import time

from src.providers._media import build_ctx

RANKING = [
    "anineko", "anizone", "anikoto", "reanime", "aniwaves",
    "kaa", "anibd", "animegg", "mkissa", "animeonsen",
]

WATCH_TIMEOUT = 40.0
PREFERRED_WATCH_TIMEOUT = 12.0
WATCH_TOTAL_TIMEOUT = 35.0
WATCH_MERGE_GRACE = 1.5
EPISODES_TIMEOUT = 30.0
EPISODES_TOTAL_TIMEOUT = 25.0
EPISODES_MERGE_GRACE = 1.5
WATCH_CACHE_TTL = 45.0

_latency: dict = {}
_ep_latency: dict = {}
_failures: dict = {}
_watch_cache: dict = {}


def _load(name):
    try:
        return importlib.import_module(f"src.providers.{name}")
    except Exception as e:
        print(f"[RACE] provider {name} unavailable: {e}")
        return None


def providers():
    mods = []
    for name in RANKING:
        mod = _load(name)
        if mod is not None:
            mods.append((name, mod))
    return mods


def record_latency(name: str, seconds: float, ok: bool, kind: str = "watch"):
    table = _latency if kind == "watch" else _ep_latency
    if ok:
        prev = table.get(name)
        table[name] = seconds if prev is None else round(0.7 * prev + 0.3 * seconds, 3)
        _failures[name] = 0
    else:
        _failures[name] = _failures.get(name, 0) + 1


def default_provider():
    if not _latency:
        return RANKING[0]
    return sorted(_latency.items(), key=lambda kv: kv[1])[0][0]


def latency_table() -> dict:
    return {name: {"avg_seconds": _latency.get(name),
                   "episodes_seconds": _ep_latency.get(name),
                   "failures": _failures.get(name, 0)}
            for name in RANKING}


async def _watch_one(mod, name, anilist_id, ep, audio, ctx):
    key = (name, anilist_id, ep, audio)
    hit = _watch_cache.get(key)
    if hit and hit[0] > time.time():
        return name, hit[1], 0.0, True
    t0 = time.time()
    try:
        streams = await asyncio.wait_for(mod.watch(anilist_id, audio, ep, ctx), WATCH_TIMEOUT)
        dt = round(time.time() - t0, 3)
        if streams:
            _watch_cache[key] = (time.time() + WATCH_CACHE_TTL, streams)
            return name, streams, dt, True
        return name, None, dt, False
    except Exception:
        return name, None, round(time.time() - t0, 3), False


async def race_watch(anilist_id: int, ep: int, audio: str = "sub",
                     preferred: str | None = None) -> dict | None:
    mods = providers()
    if not mods:
        return None

    ctx = await build_ctx(anilist_id)
    timings: dict = {}

    # A manually selected provider gets a short head start, not a 75-second lock.
    if preferred:
        pick = next((m for m in mods if m[0] == preferred), None)
        if pick is not None:
            name, mod = pick
            started = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    _watch_one(mod, name, anilist_id, ep, audio, ctx),
                    timeout=PREFERRED_WATCH_TIMEOUT,
                )
                _, streams, dt, ok = result
                record_latency(name, dt, ok)
                timings[name] = {"seconds": dt, "ok": ok}
                if ok:
                    return {
                        "provider": name, "streams": streams,
                        "requestedProvider": preferred, "timings": timings,
                        "defaultProvider": default_provider(),
                    }
            except asyncio.TimeoutError:
                dt = round(time.monotonic() - started, 3)
                record_latency(name, dt, False)
                timings[name] = {"seconds": dt, "ok": False, "timeout": True}
            mods = [m for m in mods if m[0] != preferred]

    if not mods:
        return None

    pending = {
        asyncio.create_task(_watch_one(mod, name, anilist_id, ep, audio, ctx))
        for name, mod in mods
    }
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WATCH_TOTAL_TIMEOUT
    merge_deadline = None
    winner = None

    try:
        while pending:
            active_deadline = merge_deadline if merge_deadline is not None else deadline
            remaining = active_deadline - loop.time()
            if remaining <= 0:
                break
            done, pending = await asyncio.wait(
                pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                break
            for task in done:
                try:
                    name, streams, dt, ok = task.result()
                    timings[name] = {"seconds": dt, "ok": ok}
                    record_latency(name, dt, ok)
                    if ok and winner is None:
                        winner = {"provider": name, "streams": streams}
                except Exception as e:
                    print(f"[RACE] watch task failed: {str(e)[:120]}")
            if winner is not None and merge_deadline is None:
                merge_deadline = loop.time() + WATCH_MERGE_GRACE
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    if winner is None:
        return None
    winner["timings"] = timings
    winner["defaultProvider"] = default_provider()
    if preferred:
        winner["requestedProvider"] = preferred
    return winner


async def _episodes_one(mod, name, anilist_id, ctx):
    t0 = time.time()
    try:
        data = await asyncio.wait_for(mod.get_episodes(anilist_id, ctx), EPISODES_TIMEOUT)
        eps = (data.get("episodes") or {})
        count = sum(len(v) for v in eps.values() if isinstance(v, list))
        record_latency(name, time.time() - t0, count > 0, kind="episodes")
        return name, data if count > 0 else None
    except Exception as e:
        print(f"[RACE] {name} episodes failed: {str(e)[:120]}")
        record_latency(name, time.time() - t0, False, kind="episodes")
        return name, None


async def merged_episodes(anilist_id: int) -> dict | None:
    mods = providers()
    if not mods:
        return None

    ctx = await build_ctx(anilist_id)
    pending = {
        asyncio.create_task(_episodes_one(mod, name, anilist_id, ctx))
        for name, mod in mods
    }
    merged: dict = {}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + EPISODES_TOTAL_TIMEOUT
    merge_deadline = None

    try:
        while pending:
            active_deadline = merge_deadline if merge_deadline is not None else deadline
            remaining = active_deadline - loop.time()
            if remaining <= 0:
                break
            done, pending = await asyncio.wait(
                pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                break
            for task in done:
                try:
                    name, data = task.result()
                    if data:
                        merged[name] = data
                except Exception as e:
                    print(f"[RACE] episode task failed: {str(e)[:120]}")
            if merged and merge_deadline is None:
                merge_deadline = loop.time() + EPISODES_MERGE_GRACE
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    if not merged:
        return None
    return {"anilistId": anilist_id, "source": "native", "providers": merged}


def merge_subtitles(streams: list) -> list:
    seen, out = set(), []
    for s in streams or []:
        for sub in s.get("subtitles") or []:
            url = sub.get("url")
            if url and url not in seen:
                seen.add(url)
                out.append(sub)
    return out


def providers_status() -> dict:
    return {
        "defaultProvider": default_provider(),
        "ranking": RANKING,
        "available": [name for name, _ in providers()],
        "latency": latency_table(),
    }
