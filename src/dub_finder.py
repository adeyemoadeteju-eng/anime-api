"""Official streaming availability lookup for dubbed-audio discovery.

This module lists licensed streaming services from TMDB's watch-provider
catalogue. TMDB does not expose per-title audio-track information, so the
API deliberately never claims that a provider has an English dub.
"""
import os
import re

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from src.extractor import anilist_query

router = APIRouter()


def _normalize_region(region: str) -> str:
    normalized = (region or "CA").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", normalized):
        raise HTTPException(
            status_code=400,
            detail="region must be a two-letter ISO country code, such as CA or US",
        )
    return normalized


def _select_tmdb_candidate(candidates: list[dict]) -> dict | None:
    """Choose only a confident identity match; never map a sequel to its parent."""
    exact = [
        item for item in candidates
        if item.get("exactTitle") and not item.get("containedTitle")
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        ranked_exact = sorted(
            exact,
            key=lambda item: (
                item.get("yearDelta")
                if item.get("yearDelta") is not None
                else 999
            ),
        )
        first_delta = ranked_exact[0].get("yearDelta")
        second_delta = ranked_exact[1].get("yearDelta")
        if first_delta is not None and (
            second_delta is None or second_delta - first_delta >= 2
        ):
            return ranked_exact[0]
        return None

    # Fuzzy title matching is acceptable only when the result is clearly
    # separated from the next candidate and is not merely a franchise parent.
    fuzzy = [
        item for item in candidates
        if not item.get("containedTitle")
    ]
    if not fuzzy:
        return None

    top = fuzzy[0]
    second_score = fuzzy[1].get("score", 0) if len(fuzzy) > 1 else 0
    clearly_best = (
        top.get("score", 0) >= 0.93
        and top.get("score", 0) - second_score >= 0.08
    )
    single_strong = len(fuzzy) == 1 and top.get("score", 0) >= 0.95
    return top if clearly_best or single_strong else None


def _shape_watch_providers(country_data: dict | None) -> list[dict]:
    """Merge TMDB provider categories into one compact row per service."""
    if not isinstance(country_data, dict):
        return []

    categories = (
        ("flatrate", "Subscription"),
        ("free", "Free"),
        ("ads", "Free with ads"),
        ("rent", "Rent"),
        ("buy", "Buy"),
    )
    providers: dict[int, dict] = {}

    for category_key, category_label in categories:
        for item in country_data.get(category_key) or []:
            provider_id = item.get("provider_id")
            name = item.get("provider_name")
            if not provider_id or not name:
                continue

            entry = providers.setdefault(
                int(provider_id),
                {
                    "id": int(provider_id),
                    "name": name,
                    "logoUrl": (
                        "https://image.tmdb.org/t/p/w92" + item["logo_path"]
                        if item.get("logo_path", "").startswith("/")
                        else None
                    ),
                    "displayPriority": item.get("display_priority", 999),
                    "availabilityTypes": [],
                },
            )
            if category_label not in entry["availabilityTypes"]:
                entry["availabilityTypes"].append(category_label)

    return sorted(
        providers.values(),
        key=lambda item: (
            item["displayPriority"],
            item["name"].casefold(),
        ),
    )


@router.get("/dub-finder/{anilist_id}")
async def find_dub_options(
    anilist_id: int,
    region: str = Query(
        "CA",
        min_length=2,
        max_length=2,
        description="Two-letter ISO country code; defaults to Canada",
    ),
):
    """Find official streaming services for a title and region.

    The response is a service-availability guide, not a video-source scraper.
    Audio/dub availability is intentionally reported as unknown because
    TMDB's provider catalogue does not identify audio tracks.
    """
    api_key = os.getenv("TMDB_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="TMDB_API_KEY is not configured",
        )

    country = _normalize_region(region)
    gql = """
    query ($id: Int) {
        Media(id: $id, type: ANIME) {
            id
            format
            title { romaji english native }
            startDate { year }
        }
    }
    """
    data = await anilist_query(gql, {"id": anilist_id})
    media = data.get("Media")
    if not media:
        raise HTTPException(status_code=404, detail="anime not found")

    title_data = media.get("title") or {}
    from src.endpoints import _tmdb_candidates, _tmdb_title_aliases

    _, aliases = _tmdb_title_aliases(title_data)
    year = (media.get("startDate") or {}).get("year")
    kind = "movie" if media.get("format") == "MOVIE" else "tv"

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            candidates = await _tmdb_candidates(
                client, api_key, kind, aliases, year
            )
            match = _select_tmdb_candidate(candidates)
            country_data = {}
            if match:
                response = await client.get(
                    f"https://api.themoviedb.org/3/{kind}/{match['tmdbId']}/watch/providers",
                    params={"api_key": api_key},
                )
                if response.status_code != 404:
                    response.raise_for_status()
                    country_data = (
                        response.json().get("results", {}).get(country) or {}
                    )
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="official streaming availability could not be reached",
        ) from exc

    providers = _shape_watch_providers(country_data)
    status = (
        "unmatched"
        if not match
        else "services_found"
        if providers
        else "no_listings"
    )
    guide_url = country_data.get("link")
    if not isinstance(guide_url, str) or not guide_url.startswith("https://"):
        guide_url = None

    return JSONResponse(
        content={
            "anilistId": anilist_id,
            "title": (
                title_data.get("english")
                or title_data.get("romaji")
                or title_data.get("native")
            ),
            "region": country,
            "status": status,
            "match": (
                {
                    "tmdbId": match.get("tmdbId"),
                    "title": match.get("title") or match.get("originalTitle"),
                }
                if match
                else None
            ),
            "providers": providers,
            "watchGuideUrl": guide_url,
            "dubStatus": "not_reported",
            "dubNote": (
                "Streaming catalogues identify services, not audio tracks. "
                "English-dub availability must be checked on the provider."
            ),
        },
        headers={
            "Cache-Control": "public, max-age=21600, stale-while-revalidate=43200"
        },
    )
