import asyncio
import httpx

from src.extractor import anilist_query
from src.providers import _cache
from src.providers._http import UA

ARM = "https://arm.haglund.dev/api/v2/ids"
ANIZIP = "https://api.ani.zip/mappings"

MEDIA_QUERY = (
    "query($id:Int){Media(id:$id,type:ANIME){id idMal title{english romaji native} "
    "status format episodes seasonYear startDate{year} synonyms "
    "nextAiringEpisode{episode airingAt timeUntilAiring}}}"
)

_STATUS = {"RELEASING": "RELEASING", "FINISHED": "FINISHED",
            "NOT_YET_RELEASED": "NOT_YET_RELEASED", "CANCELLED": "FINISHED",
            "HIATUS": "HIATUS"}


async def _fetch_arm(anilist_id: int):
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.get(
                f"{ARM}?source=anilist&id={anilist_id}",
                headers={"User-Agent": UA, "Accept": "application/json"},
            )
        if res.status_code != 200:
            return None
        return res.json()
    except Exception:
        return None


async def get_media(anilist_id: int) -> dict:
    key = f"np-media:{anilist_id}"
    hit = _cache.cached(key, _cache.MAPPING_TTL)
    if hit is not None:
        return hit
    arm, data = await asyncio.gather(
        _fetch_arm(anilist_id),
        anilist_query(MEDIA_QUERY, {"id": int(anilist_id)}),
    )
    al = (data or {}).get("Media")
    if not al:
        raise RuntimeError(f"No data found for AniList ID {anilist_id}")
    arm = arm or {}
    media = {
        "id": int(anilist_id),
        "idMal": arm.get("myanimelist") or al.get("idMal"),
        "title": {
            "english": (al.get("title") or {}).get("english"),
            "romaji": (al.get("title") or {}).get("romaji"),
            "native": (al.get("title") or {}).get("native"),
        },
        "status": _STATUS.get(al.get("status"), "RELEASING"),
        "format": al.get("format"),
        "episodes": al.get("episodes"),
        "seasonYear": al.get("seasonYear"),
        "startDate": al.get("startDate"),
        "nextAiringEpisode": al.get("nextAiringEpisode"),
        "synonyms": al.get("synonyms") or [],
        "arm": arm,
    }
    _cache.set(key, media, _cache.MAPPING_TTL)
    return media


async def get_anizip(anilist_id: int):
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            res = await client.get(
                f"{ANIZIP}?anilist_id={anilist_id}",
                headers={"User-Agent": UA, "Accept": "application/json"},
            )
        if res.status_code != 200:
            return None
        return res.json()
    except Exception:
        return None


async def build_ctx(anilist_id: int, media: dict | None = None) -> dict:
    # AniZip is optional metadata; fetch it alongside the essential AniList data
    # so an unavailable mapping service does not add another full timeout.
    media_task = get_media(anilist_id) if media is None else asyncio.sleep(0, result=media)
    media_result, anizip = await asyncio.gather(media_task, get_anizip(anilist_id))
    return {"media": media_result, "anizip": anizip}
