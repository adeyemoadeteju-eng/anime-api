from fastapi import APIRouter, Query, HTTPException
from typing import Optional
import httpx
import base64
import asyncio
import time

_TMDB_CANDIDATE_CACHE = {}
_TMDB_CANDIDATE_CACHE_TTL = 300

from src.queries import MEDIA_LIST_FIELDS, MEDIA_FULL_FIELDS
from src.parser import proxy_deep_images, inject_source_slugs, encode_pipe_request, decode_pipe_response
from src.extractor import anilist_query, fetch_raw_episodes
from src.config import iter_miruro_pipe_targets
from src.providers import _race as provider_race

router = APIRouter()


@router.get("/search")
async def search_anime(
    query: str,
    page: int = Query(1, ge=1, description="page number"),
    per_page: int = Query(20, ge=1, le=50, description="results per page"),
):

    gql = f"""
    query ($search: String, $page: Int, $perPage: Int) {{
        Page(page: $page, perPage: $perPage) {{
            pageInfo {{ total currentPage lastPage hasNextPage perPage }}
            media(search: $search, type: ANIME, sort: SEARCH_MATCH) {{
                {MEDIA_LIST_FIELDS}
            }}
        }}
    }}
    """
    data = await anilist_query(gql, {"search": query, "page": page, "perPage": per_page})
    page_data = data.get("Page", {})
    page_info = page_data.get("pageInfo", {})

    response = {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "results": page_data.get("media", []),
    }
    return proxy_deep_images(response)


@router.get("/suggestions")
async def search_suggestions(
    query: str = Query(..., min_length=1, description="small search query for dropdowns"),
):

    gql = """
    query ($search: String) {
        Page(page: 1, perPage: 8) {
            media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
                id
                title { romaji english }
                coverImage { large }
                format
                status
                startDate { year }
                episodes
            }
        }
    }
    """
    data = await anilist_query(gql, {"search": query})
    results = []

    for item in data.get("Page", {}).get("media", []):
        results.append({
            "id": item["id"],
            "title": item["title"].get("english") or item["title"].get("romaji"),
            "title_romaji": item["title"].get("romaji"),
            "poster": item["coverImage"]["large"],
            "format": item.get("format"),
            "status": item.get("status"),
            "year": (item.get("startDate") or {}).get("year"),
            "episodes": item.get("episodes"),
        })
    return proxy_deep_images({"suggestions": results})

SORT_MAP = {
    "SCORE_DESC": "SCORE_DESC",
    "POPULARITY_DESC": "POPULARITY_DESC",
    "TRENDING_DESC": "TRENDING_DESC",
    "START_DATE_DESC": "START_DATE_DESC",
    "FAVOURITES_DESC": "FAVOURITES_DESC",
    "UPDATED_AT_DESC": "UPDATED_AT_DESC",
}


@router.get("/filter")
async def filter_anime(
    genre: Optional[str] = Query(None, description="genre name"),
    tag: Optional[str] = Query(None, description="tag name"),
    year: Optional[int] = Query(None, description="season year"),
    season: Optional[str] = Query(None, description="season time"),
    format: Optional[str] = Query(None, description="media format"),
    status: Optional[str] = Query(None, description="release status"),
    sort: str = Query("POPULARITY_DESC", description="ordering logic"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
):

    args = ["type: ANIME", f"sort: [{SORT_MAP.get(sort, 'POPULARITY_DESC')}]"]
    variables = {"page": page, "perPage": per_page}

    if genre:
        args.append("genre: $genre")
        variables["genre"] = genre
    if tag:
        args.append("tag: $tag")
        variables["tag"] = tag
    if year:
        args.append("seasonYear: $seasonYear")
        variables["seasonYear"] = year
    if season:
        args.append("season: $season")
        variables["season"] = season.upper()
    if format:
        args.append("format: $format")
        variables["format"] = format.upper()
    if status:
        args.append("status: $status")
        variables["status"] = status.upper()

    var_types = ["$page: Int", "$perPage: Int"]
    if genre:
        var_types.append("$genre: String")
    if tag:
        var_types.append("$tag: String")
    if year:
        var_types.append("$seasonYear: Int")
    if season:
        var_types.append("$season: MediaSeason")
    if format:
        var_types.append("$format: MediaFormat")
    if status:
        var_types.append("$status: MediaStatus")

    gql = f"""
    query ({', '.join(var_types)}) {{
        Page(page: $page, perPage: $perPage) {{
            pageInfo {{ total currentPage lastPage hasNextPage perPage }}
            media({', '.join(args)}) {{
                {MEDIA_LIST_FIELDS}
            }}
        }}
    }}
    """

    data = await anilist_query(gql, variables)
    page_data = data.get("Page", {})
    page_info = page_data.get("pageInfo", {})

    response = {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "results": page_data.get("media", []),
    }
    return proxy_deep_images(response)


async def _fetch_collection(sort_type: str, status: str = None, page: int = 1, per_page: int = 20):
    status_filter = f", status: {status}" if status else ""
    gql = f"""
    query ($page: Int, $perPage: Int) {{
        Page(page: $page, perPage: $perPage) {{
            pageInfo {{ total currentPage lastPage hasNextPage perPage }}
            media(type: ANIME, sort: [{sort_type}]{status_filter}) {{
                {MEDIA_LIST_FIELDS}
            }}
        }}
    }}
    """
    data = await anilist_query(gql, {"page": page, "perPage": per_page})
    page_data = data.get("Page", {})
    page_info = page_data.get("pageInfo", {})

    response = {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "results": page_data.get("media", []),
    }
    return proxy_deep_images(response)


@router.get("/spotlight")
async def get_spotlight():

    gql = f"""
    query {{
        Page(page: 1, perPage: 10) {{
            media(sort: [TRENDING_DESC, POPULARITY_DESC], type: ANIME) {{
                {MEDIA_LIST_FIELDS}
            }}
        }}
    }}
    """
    data = await anilist_query(gql)
    media = data.get("Page", {}).get("media", [])
    return proxy_deep_images({"results": media})


@router.get("/trending")
async def get_trending(page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=50)):

    return await _fetch_collection("TRENDING_DESC", page=page, per_page=per_page)


@router.get("/popular")
async def get_popular(page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=50)):

    return await _fetch_collection("POPULARITY_DESC", page=page, per_page=per_page)


@router.get("/upcoming")
async def get_upcoming(page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=50)):

    return await _fetch_collection("POPULARITY_DESC", "NOT_YET_RELEASED", page=page, per_page=per_page)


@router.get("/recent")
async def get_recent(page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=50)):

    return await _fetch_collection("START_DATE_DESC", "RELEASING", page=page, per_page=per_page)


@router.get("/schedule")
async def get_schedule(page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=50)):

    gql = f"""
    query ($page: Int, $perPage: Int) {{
        Page(page: $page, perPage: $perPage) {{
            pageInfo {{ total currentPage lastPage hasNextPage perPage }}
            airingSchedules(notYetAired: true, sort: TIME) {{
                episode
                airingAt
                timeUntilAiring
                media {{
                    {MEDIA_LIST_FIELDS}
                }}
            }}
        }}
    }}
    """
    data = await anilist_query(gql, {"page": page, "perPage": per_page})
    page_data = data.get("Page", {})
    page_info = page_data.get("pageInfo", {})

    results = []
    for item in page_data.get("airingSchedules", []):
        entry = item.get("media", {})
        entry["next_episode"] = item.get("episode")
        entry["airingAt"] = item.get("airingAt")
        entry["timeUntilAiring"] = item.get("timeUntilAiring")
        results.append(entry)

    response = {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "results": results,
    }
    return proxy_deep_images(response)


@router.get("/info/{anilist_id}")
async def get_anime_info(anilist_id: int):

    gql = f"""
    query ($id: Int) {{
        Media(id: $id, type: ANIME) {{
            {MEDIA_FULL_FIELDS}
        }}
    }}
    """
    data = await anilist_query(gql, {"id": anilist_id})
    media = data.get("Media")
    if not media:
        raise HTTPException(status_code=404, detail="anime not found sorry")
    return proxy_deep_images(media)


@router.get("/anime/{anilist_id}/characters")
async def get_anime_characters(
    anilist_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=50),
):

    gql = """
    query ($id: Int, $page: Int, $perPage: Int) {
        Media(id: $id, type: ANIME) {
            id
            title { romaji english }
            characters(sort: [ROLE, RELEVANCE], page: $page, perPage: $perPage) {
                pageInfo { total currentPage lastPage hasNextPage perPage }
                edges {
                    role
                    node {
                        id
                        name { full native userPreferred }
                        image { large medium }
                        description
                        gender
                        dateOfBirth { year month day }
                        age
                        favourites
                        siteUrl
                    }
                    voiceActors {
                        id
                        name { full native }
                        image { large }
                        languageV2
                    }
                }
            }
        }
    }
    """
    data = await anilist_query(gql, {"id": anilist_id, "page": page, "perPage": per_page})
    media = data.get("Media")
    if not media:
        raise HTTPException(status_code=404, detail="couldnt locate anime")

    chars = media.get("characters", {})
    page_info = chars.get("pageInfo", {})

    response = {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "characters": chars.get("edges", []),
    }
    return proxy_deep_images(response)


@router.get("/anime/{anilist_id}/relations")
async def get_anime_relations(anilist_id: int):

    gql = """
    query ($id: Int) {
        Media(id: $id, type: ANIME) {
            id
            title { romaji english }
            relations {
                edges {
                    relationType(version: 2)
                    node {
                        id
                        title { romaji english native }
                        coverImage { large }
                        bannerImage
                        format
                        type
                        status
                        episodes
                        chapters
                        meanScore
                        averageScore
                        popularity
                        startDate { year month day }
                    }
                }
            }
        }
    }
    """
    data = await anilist_query(gql, {"id": anilist_id})
    media = data.get("Media")
    if not media:
        raise HTTPException(status_code=404, detail="not found")

    response = {
        "id": media["id"],
        "title": media["title"],
        "relations": media.get("relations", {}).get("edges", []),
    }
    return proxy_deep_images(response)


@router.get("/anime/{anilist_id}/recommendations")
async def get_anime_recommendations(
    anilist_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=25),
):

    gql = """
    query ($id: Int, $page: Int, $perPage: Int) {
        Media(id: $id, type: ANIME) {
            id
            title { romaji english }
            recommendations(sort: RATING_DESC, page: $page, perPage: $perPage) {
                pageInfo { total currentPage lastPage hasNextPage perPage }
                nodes {
                    rating
                    mediaRecommendation {
                        id
                        title { romaji english native }
                        coverImage { large extraLarge }
                        bannerImage
                        format
                        episodes
                        status
                        meanScore
                        averageScore
                        popularity
                        genres
                        startDate { year }
                    }
                }
            }
        }
    }
    """
    data = await anilist_query(gql, {"id": anilist_id, "page": page, "perPage": per_page})
    media = data.get("Media")
    if not media:
        raise HTTPException(status_code=404, detail="where the anime at")

    recs = media.get("recommendations", {})
    page_info = recs.get("pageInfo", {})

    response = {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "recommendations": recs.get("nodes", []),
    }
    return proxy_deep_images(response)


@router.get("/episodes/{anilist_id}")
async def get_episodes(anilist_id: int):

    try:
        native = await provider_race.merged_episodes(anilist_id)
        if native:
            return proxy_deep_images(native)
    except Exception as e:
        print(f"[KUHI API] native episodes failed, falling back to pipe: {e}")
    data = await fetch_raw_episodes(anilist_id)
    return proxy_deep_images(inject_source_slugs(data, anilist_id))


@router.get("/providers/status")
async def get_providers_status():

    return provider_race.providers_status()


@router.get("/bridge-status")
async def get_bridge_status():

    return provider_race.providers_status()


@router.get("/genres")
async def get_genres():

    gql = """
    query {
        GenreCollection
    }
    """
    data = await anilist_query(gql)
    return {"genres": data.get("GenreCollection", [])}


@router.get("/sources")
async def get_sources(
    episodeId: str = Query(..., description="episode tracking string"),
    provider: str = Query(..., description="video provider host"),
    anilistId: int = Query(..., description="anime db identification code"),
    category: str = Query("sub", description="dubbed or subbed version"),
):

    enc_id = base64.urlsafe_b64encode(episodeId.encode()).decode().rstrip('=')
    payload = {
        "path": "sources",
        "method": "GET",
        "query": {
            "episodeId": enc_id,
            "provider": provider,
            "category": category,
            "anilistId": anilistId,
        },
        "body": None,
        "version": "0.1.0",
    }
    encoded_req = encode_pipe_request(payload)

    async with httpx.AsyncClient(timeout=15.0) as client:
        last_status = None
        for pipe_target, headers in iter_miruro_pipe_targets(encoded_req):
            print(f"\n[KUHI API] extracting stream sources pipe targeting:", pipe_target)
            try:
                res = await client.get(pipe_target, headers=headers)
                last_status = res.status_code
                if res.status_code == 200:
                    return proxy_deep_images(decode_pipe_response(res.text.strip()))
            except Exception as e:
                print(f"[KUHI API] Failed to connect to {pipe_target}: {e}")

        raise HTTPException(status_code=last_status or 502, detail="fetching pipe failed real bad")


@router.get("/extract/{query}")
async def extract_simple(
    query: str,
    episode: int = Query(1, alias="e"),
    audio: str = Query("sub", alias="type", description="sub or dub"),
    provider: str | None = Query(None, alias="provider", description="prefer one provider"),
):

    audio = (audio or "sub").lower()
    if audio not in ("sub", "dub"):
        raise HTTPException(status_code=422, detail="type must be sub or dub")
    wanted = (provider or "").lower() or None
    if wanted and wanted not in provider_race.RANKING:
        raise HTTPException(status_code=422, detail=f"unknown provider '{provider}'. valid: {', '.join(provider_race.RANKING)}")

    if query.isdigit():
        anilist_id = int(query)

        gql = """
        query ($id: Int) {
            Media(id: $id, type: ANIME) {
                id
                format
            }
        }
        """
        media_data = await anilist_query(gql, {"id": anilist_id})
        media = media_data.get("Media")
        if not media:
            raise HTTPException(status_code=404, detail="anime not found")

        if media.get("format") == "MOVIE":
            episode = 1
    else:

        gql = f"""
        query ($search: String) {{
            Page(page: 1, perPage: 1) {{
                media(search: $search, type: ANIME, sort: SEARCH_MATCH) {{
                    id
                    format
                }}
            }}
        }}
        """
        search_data = await anilist_query(gql, {"search": query.replace("-", " ")})
        media_list = search_data.get("Page", {}).get("media", [])
        if not media_list:
            raise HTTPException(status_code=404, detail="anime not found")

        anilist_id = media_list[0]["id"]
        media_format = media_list[0].get("format")

        if media_format == "MOVIE":
            episode = 1

    try:
        raced = await provider_race.race_watch(anilist_id, episode, audio, preferred=wanted)
        if raced:
            result = {
                "anilistId": anilist_id,
                "episode": episode,
                "type": audio,
                "provider": raced["provider"],
                "defaultProvider": raced.get("defaultProvider"),
                "requestedProvider": raced.get("requestedProvider"),
                "streams": raced["streams"],
                "subtitles": provider_race.merge_subtitles(raced["streams"]),
            }
            return proxy_deep_images(result)
    except Exception as e:
        print(f"[KUHI API] native race failed, falling back to pipe: {e}")

    data = await fetch_raw_episodes(anilist_id)
    providers = data.get("providers", {})

    if not providers:
        raise HTTPException(status_code=404, detail="no providers available for this anime")

    target_episode_id = None
    target_provider = None
    target_cat = None

    ranking = ["zoro", "bee", "telli", "arc", "yugen", "jet", "neo", "kiwi"]

    for prov in ranking:
        if prov in providers:
            prov_data = providers[prov]
            eps = prov_data.get("episodes", {})
            for cat in ["sub", "dub", "raw"]:
                if cat in eps:
                    cat_eps = eps[cat]
                    if isinstance(cat_eps, list):
                        for ep in cat_eps:
                            if ep.get("number") == episode:
                                target_episode_id = ep.get("id")
                                target_provider = prov
                                target_cat = cat
                                break
                if target_episode_id:
                    break
        if target_episode_id:
            break

    if not target_episode_id:
        raise HTTPException(status_code=404, detail=f"episode {episode} not found in any provider")

    last_error = None
    providers_to_try = [(target_provider, target_cat, target_episode_id)]

    for prov in ranking:
        if prov != target_provider and prov in providers:
            prov_data = providers[prov]
            eps = prov_data.get("episodes", {})
            for cat in ["sub", "dub", "raw"]:
                if cat in eps:
                    cat_eps = eps[cat]
                    if isinstance(cat_eps, list):
                        for ep in cat_eps:
                            if ep.get("number") == episode:
                                providers_to_try.append((prov, cat, ep.get("id")))
                                break

    for try_provider, try_cat, try_ep_id in providers_to_try:
        try:
            enc_id = base64.urlsafe_b64encode(try_ep_id.encode()).decode().rstrip('=')
            payload = {
                "path": "sources",
                "method": "GET",
                "query": {
                    "episodeId": enc_id,
                    "provider": try_provider,
                    "category": try_cat,
                    "anilistId": anilist_id,
                },
                "body": None,
                "version": "0.1.0",
            }
            encoded_req = encode_pipe_request(payload)

            async with httpx.AsyncClient(timeout=15.0) as client:
                for pipe_target, headers in iter_miruro_pipe_targets(encoded_req):
                    print(f"\n[KUHI API] trying provider {try_provider}: {pipe_target}")
                    try:
                        res = await client.get(pipe_target, headers=headers)

                        if res.status_code == 200:
                            result = decode_pipe_response(res.text.strip())
                            print(f"[KUHI API] success with provider {try_provider}")
                            return proxy_deep_images(result)

                        last_error = f"{try_provider} failed: {res.status_code}"
                        print(f"[KUHI API] {last_error}")
                    except Exception as e:
                        print(f"[KUHI API] Failed to connect to {pipe_target}: {e}")
        except Exception as e:
            last_error = f"{try_provider} error: {str(e)}"
            print(f"[KUHI API] {last_error}")
            continue

    if audio == "dub":
        raise HTTPException(
            status_code=404,
            detail=f"no dub streams found for episode {episode}; try type=sub",
        )
    raise HTTPException(status_code=500, detail=f"all providers failed. last error: {last_error}")


def _tmdb_normalize(value):
    import re
    import unicodedata
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    # Remove a year suffix such as "(2012)" before comparing title strings.
    value = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def _tmdb_has_explicit_season_marker(title_data):
    """Whether any AniList title explicitly identifies a season/part number."""
    import re

    season_suffix = re.compile(
        r"(?i)(?:\s+(?:season|part|cour|stage)\s*(?:\d+|final)"
        r"|\s+\d+(?:st|nd|rd|th)\s+(?:season|part|cour|stage)"
        r"|\s+final\s+season)\s*$"
    )
    return any(
        isinstance(value, str) and season_suffix.search(value.strip())
        for value in (
            title_data.get("english"),
            title_data.get("romaji"),
            title_data.get("native"),
        )
    )


def _tmdb_title_aliases(title_data):
    import re

    titles = []
    for value in (title_data.get("english"), title_data.get("romaji"), title_data.get("native")):
        if value and value not in titles:
            titles.append(value)

    aliases = list(titles)

    # AniList often stores sequel seasons as "My Hero Academia Season 2",
    # "Boku no Hero Academia 2", or a native title ending in a digit. Search
    # TMDB with the clean parent title too, then resolve the requested season
    # against the parent's actual TMDB season list.
    season_suffix = re.compile(
        r"(?i)(?:\s+(?:season|part|cour|stage)\s*(?:\d+|final)"
        r"|\s+\d+(?:st|nd|rd|th)\s+(?:season|part|cour|stage)"
        r"|\s+final\s+season)\s*$"
    )
    has_explicit_season = any(
        season_suffix.search(value.strip()) for value in titles if isinstance(value, str)
    )
    if has_explicit_season:
        for value in titles:
            cleaned = season_suffix.sub("", value.strip()).strip()
            # The romanized/native AniList title may carry only the trailing
            # season digit while the English title supplies the explicit label.
            cleaned = re.sub(r"\s*\d+$", "", cleaned).strip()
            normalized_cleaned = _tmdb_normalize(cleaned)
            if (len(normalized_cleaned) >= 5 or (not normalized_cleaned and len(cleaned) >= 5)) and cleaned not in aliases:
                aliases.append(cleaned)

    for value in titles:
        # Subtitle separators can produce useful alternate search names.
        # Avoid splitting on a plain spaced hyphen: anime titles often use it
        # for stage/episode labels, which creates weak aliases like "3rd Stage".
        for separator in (":", "：", "—", "–"):
            if separator in value:
                tail = value.rsplit(separator, 1)[-1].strip()
                if len(tail) >= 5 and not re.fullmatch(
                    r"(?i)(?:\d+(?:st|nd|rd|th)\s+)?(?:stage|season|part|cour|episode)\s*\d*",
                    tail,
                ) and tail not in aliases:
                    aliases.append(tail)
    return titles, aliases


def _tmdb_score(item, aliases, year):
    from difflib import SequenceMatcher

    name = item.get("title") or item.get("name") or ""
    original = item.get("original_title") or item.get("original_name") or ""
    names = [_tmdb_normalize(name), _tmdb_normalize(original)]

    best = 0.0
    exact_title = False
    contained_title = False

    for alias in aliases:
        normalized_alias = _tmdb_normalize(alias)
        if not normalized_alias:
            continue

        for normalized_name in names:
            if not normalized_name:
                continue

            if normalized_alias == normalized_name:
                exact_title = True
                best = max(best, 1.0)
                continue

            # AniList sequel/arc titles may contain their parent TMDB series
            # title (for example, "Steel Ball Run JoJo's Bizarre Adventure").
            # Only accept containment in this direction: the TMDB title must be
            # the shorter parent inside the full AniList title. The reverse
            # direction incorrectly treats a spin-off title such as
            # "My Hero Academia: Vigilantes" as the main "My Hero Academia"
            # series when the exact main-series result is missing from search.
            shorter = min(len(normalized_alias), len(normalized_name))
            longer = max(len(normalized_alias), len(normalized_name))
            if shorter >= 7 and normalized_name in normalized_alias:
                contained_title = True
                best = max(best, 0.94 + (0.04 * shorter / longer))
                continue

            best = max(best, SequenceMatcher(None, normalized_alias, normalized_name).ratio())

    date = item.get("release_date") or item.get("first_air_date") or ""
    item_year = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None
    year_delta = abs(item_year - year) if year and item_year else None

    # Premiere year is only a tie-breaker. It must not cancel an exact or
    # strong contained title match when AniList dates a sequel/arc separately.
    if not exact_title and not contained_title and year_delta is not None:
        if year_delta == 0:
            best += 0.02
        elif year_delta == 1:
            best += 0.01

    title_match = "exact" if exact_title else ("contained" if contained_title else "fuzzy")
    return best, item_year, exact_title, contained_title, year_delta, title_match


async def _tmdb_candidates(client, api_key, kind, aliases, year):
    cache_key = (kind, tuple(_tmdb_normalize(value) for value in aliases), year)
    cached = _TMDB_CANDIDATE_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and cached[0] > now:
        return [dict(item) for item in cached[1]]

    async def search_title(title):
        params = {"api_key": api_key, "query": title, "include_adult": "false"}
        try:
            response = await client.get(
                f"https://api.themoviedb.org/3/search/{kind}",
                params=params,
            )
            response.raise_for_status()
            return response.json().get("results", [])
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"TMDB search failed with status {exc.response.status_code}",
            )
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="could not reach TMDB")

    # Search all title aliases together. Serial requests multiplied the API
    # timeout by the number of aliases and could exceed the browser's deadline.
    result_sets = await asyncio.gather(*(search_title(title) for title in aliases))
    candidates = {}
    for results in result_sets:
        for item in results:
            tmdb_id = item.get("id")
            if not tmdb_id:
                continue
            score, item_year, exact_title, contained_title, year_delta, title_match = _tmdb_score(item, aliases, year)
            existing = candidates.get(tmdb_id)
            candidate = {
                "tmdbId": tmdb_id,
                "title": item.get("title") or item.get("name"),
                "originalTitle": item.get("original_title") or item.get("original_name"),
                "year": item_year,
                "score": round(score, 4),
                "exactTitle": exact_title,
                "containedTitle": contained_title,
                "titleMatch": title_match,
                "yearDelta": year_delta,
                "overview": item.get("overview"),
                "posterPath": item.get("poster_path"),
            }
            if not existing or candidate["score"] > existing["score"]:
                candidates[tmdb_id] = candidate

    ranked = sorted(candidates.values(), key=lambda item: item["score"], reverse=True)
    _TMDB_CANDIDATE_CACHE[cache_key] = (
        time.monotonic() + _TMDB_CANDIDATE_CACHE_TTL,
        [dict(item) for item in ranked],
    )
    return ranked


def _load_tmdb_overrides():
    """Optional verified mappings, configured as JSON in ANIME_TMDB_OVERRIDES."""
    import json
    import os
    raw = os.getenv("ANIME_TMDB_OVERRIDES", "{}")
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        print("[TMDB] ANIME_TMDB_OVERRIDES is invalid JSON; ignoring overrides")
        return {}


def _tmdb_season_hints(title_data, candidate):
    """Build distinctive title fragments that can identify a TMDB season."""
    import re

    raw_titles = [
        title_data.get("english"),
        title_data.get("romaji"),
        title_data.get("native"),
    ]
    parent_titles = [
        candidate.get("title"),
        candidate.get("originalTitle"),
    ]
    hints = set()

    for raw_title in raw_titles:
        if not raw_title:
            continue

        normalized_title = _tmdb_normalize(raw_title)
        if len(normalized_title) >= 5:
            hints.add(normalized_title)

        # Remove catalog suffixes such as "2nd - 3rd STAGE" before
        # comparing the story-arc title to TMDB season names.
        title_without_stage = re.sub(
            r"(?i)\s+\d+(?:st|nd|rd|th)\s*(?:[-–—]\s*\d+(?:st|nd|rd|th)\s*)?(?:stage|season|part|cour)\s*\d*\s*$",
            "",
            raw_title,
        ).strip()
        clean_normalized = _tmdb_normalize(title_without_stage)
        if len(clean_normalized) >= 5:
            hints.add(clean_normalized)

        # Colon/dash-separated subtitles often name the season or story arc.
        for part in re.split(r"[:：—–]", title_without_stage):
            normalized_part = _tmdb_normalize(part)
            if len(normalized_part) >= 5:
                hints.add(normalized_part)

        # Some anime titles append the parent franchise name without a
        # separator, e.g. "Steel Ball Run JoJo's Bizarre Adventure".
        for parent_title in parent_titles:
            normalized_parent = _tmdb_normalize(parent_title)
            for source_title in (normalized_title, clean_normalized):
                if normalized_parent and normalized_parent in source_title:
                    remainder = source_title.replace(normalized_parent, "")
                    if len(remainder) >= 5:
                        hints.add(remainder)

        # Support common numbered season/part labels, including both
        # "Season 2" and "2nd Season" title styles.
        for match in re.finditer(
            r"(?i)\b(?:season|part|cour|stage)\s*(\d+)|\b(\d+)(?:st|nd|rd|th)\s+(?:season|part|cour|stage)\b",
            raw_title,
        ):
            number = match.group(1) or match.group(2)
            hints.add("season" + number)
            hints.add("part" + number)

        # My Hero Academia's eighth entry is commonly titled "Final Season"
        # rather than "8th Season". TMDB may list it simply as "Season 8",
        # so add that equivalent hint only for the MHA franchise. Do not apply
        # it to the separate Vigilantes spin-off.
        is_mha_title = (
            "myheroacademia" in normalized_title
            or "bokunoheroacademia" in normalized_title
        )
        is_vigilantes = "vigilantes" in normalized_title
        if is_mha_title and not is_vigilantes and re.search(
            r"(?i)\bfinal\s+season\b", raw_title
        ):
            hints.add("season8")
            hints.add("finalseason")

    return sorted(hints, key=len, reverse=True)


def _tmdb_score_season(season, hints):
    from difflib import SequenceMatcher

    season_name = season.get("name") or ""
    original_name = season.get("original_name") or ""
    season_titles = [
        _tmdb_normalize(season_name),
        _tmdb_normalize(original_name),
    ]

    best = 0.0
    for hint in hints:
        for season_title in season_titles:
            if not hint or not season_title:
                continue
            if hint == season_title:
                best = max(best, 1.0)
            elif len(hint) >= 5 and (
                hint in season_title or season_title in hint
            ):
                shorter = min(len(hint), len(season_title))
                longer = max(len(hint), len(season_title))
                best = max(best, 0.94 + (0.05 * shorter / longer))
            else:
                best = max(best, SequenceMatcher(None, hint, season_title).ratio())

    return best


async def _tmdb_resolve_season(client, api_key, candidate, title_data):
    """Return a season only when its TMDB name strongly matches the anime title."""
    tmdb_id = candidate.get("tmdbId")
    if not tmdb_id:
        return None

    try:
        response = await client.get(
            f"https://api.themoviedb.org/3/tv/{tmdb_id}",
            params={"api_key": api_key},
        )
        response.raise_for_status()
        seasons = response.json().get("seasons", [])
    except httpx.HTTPError:
        return None

    hints = _tmdb_season_hints(title_data, candidate)
    ranked = []
    for item in seasons:
        season_number = item.get("season_number")
        if season_number is None or int(season_number) <= 0:
            continue

        score = _tmdb_score_season(item, hints)
        if score >= 0.93:
            ranked.append({
                "seasonNumber": int(season_number),
                "name": item.get("name"),
                "airDate": item.get("air_date"),
                "episodeCount": item.get("episode_count"),
                "score": round(score, 4),
            })

    ranked.sort(key=lambda item: item["score"], reverse=True)
    if not ranked:
        return None

    if len(ranked) > 1 and ranked[0]["score"] - ranked[1]["score"] < 0.06:
        return None

    return ranked[0]


async def _tmdb_exact_match_mapping(client, api_key, candidate, title_data):
    """Exact parent title plus verified requested season, or no match."""
    if _tmdb_has_explicit_season_marker(title_data):
        resolved = await _tmdb_resolve_season(
            client, api_key, candidate, title_data
        )
        if not resolved:
            return None
        return candidate.get("tmdbId"), resolved["seasonNumber"]

    return candidate.get("tmdbId"), 1


@router.get("/tmdb-match/{anilist_id}")
async def get_tmdb_matches(anilist_id: int):
    """Diagnostic search: returns ranked TMDB candidates and their available seasons."""
    import os
    api_key = os.getenv("TMDB_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="TMDB_API_KEY is not configured")

    gql = """
    query ($id: Int) {
        Media(id: $id, type: ANIME) {
            id idMal format
            title { romaji english native }
            startDate { year }
            relations {
                edges {
                    relationType(version: 2)
                    node { id idMal format title { romaji english native } startDate { year } }
                }
            }
        }
    }
    """
    data = await anilist_query(gql, {"id": anilist_id})
    media = data.get("Media")
    if not media:
        raise HTTPException(status_code=404, detail="anime not found")

    title_data = media.get("title") or {}
    titles, aliases = _tmdb_title_aliases(title_data)
    year = (media.get("startDate") or {}).get("year")
    kind = "movie" if media.get("format") == "MOVIE" else "tv"
    async with httpx.AsyncClient(timeout=15.0) as client:
        candidates = await _tmdb_candidates(client, api_key, kind, aliases, year)
        if kind == "tv":
            for candidate in candidates[:8]:
                try:
                    detail = await client.get(
                        f"https://api.themoviedb.org/3/tv/{candidate['tmdbId']}",
                        params={"api_key": api_key},
                    )
                    detail.raise_for_status()
                    candidate["seasons"] = [
                        {
                            "seasonNumber": season.get("season_number"),
                            "name": season.get("name"),
                            "airDate": season.get("air_date"),
                            "episodeCount": season.get("episode_count"),
                        }
                        for season in detail.json().get("seasons", [])
                    ]
                except httpx.HTTPError:
                    candidate["seasons"] = []

    relations = []
    for edge in (media.get("relations") or {}).get("edges", []):
        node = edge.get("node") or {}
        relations.append({
            "relation": edge.get("relationType"),
            "id": node.get("id"),
            "idMal": node.get("idMal"),
            "format": node.get("format"),
            "title": node.get("title"),
            "year": (node.get("startDate") or {}).get("year"),
        })

    return {
        "anilistId": media["id"],
        "malId": media.get("idMal"),
        "anilistTitle": title_data,
        "titlesSearched": aliases,
        "format": media.get("format"),
        "year": year,
        "mediaType": kind,
        "relations": relations,
        "candidates": candidates[:10],
        "configuredOverride": _load_tmdb_overrides().get(str(anilist_id)),
        "note": "For franchise entries, playback requires a TMDB season-name match; it will not fall back to Season 1 of a parent series.",
    }


@router.get("/playback/{anilist_id}")
async def get_embed_playback(
    anilist_id: int,
    episode: int = Query(1, ge=1, le=1000),
):
    """Return embed candidates using verified overrides or an unambiguous ranked TMDB match."""
    import os
    api_key = os.getenv("TMDB_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="TMDB_API_KEY is not configured")

    gql = """
    query ($id: Int) {
        Media(id: $id, type: ANIME) {
            id idMal format
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
    titles, aliases = _tmdb_title_aliases(title_data)
    year = (media.get("startDate") or {}).get("year")
    is_movie = media.get("format") == "MOVIE"
    kind = "movie" if is_movie else "tv"

    override = _load_tmdb_overrides().get(str(anilist_id))
    tmdb_id = None
    season = 1
    episode_offset = 0
    if isinstance(override, dict) and override.get("tmdbId"):
        try:
            tmdb_id = int(override["tmdbId"])
            season = int(override.get("season", 1))
            episode_offset = int(override.get("episodeOffset", 0))
            if season < 0 or episode_offset < 0:
                raise ValueError()
        except (TypeError, ValueError):
            raise HTTPException(status_code=500, detail=f"Invalid TMDB override configured for AniList ID {anilist_id}")

    candidates = []
    if not tmdb_id:
        async with httpx.AsyncClient(timeout=15.0) as client:
            candidates = await _tmdb_candidates(client, api_key, kind, aliases, year)

        # Exact title identity can select the series directly.
        # Parent/franchise containment is NOT enough: it must also identify
        # the correct named TMDB season (e.g. Steel Ball Run on the JoJo show).
        if candidates:
            exact_matches = [item for item in candidates if item.get("exactTitle")]
            selected_exact = None
            if len(exact_matches) == 1:
                selected_exact = exact_matches[0]
            elif len(exact_matches) > 1:
                ranked_exact = sorted(
                    exact_matches,
                    key=lambda item: item.get("yearDelta") if item.get("yearDelta") is not None else 999,
                )
                first_delta = ranked_exact[0].get("yearDelta")
                second_delta = ranked_exact[1].get("yearDelta")
                if first_delta is not None and (second_delta is None or second_delta - first_delta >= 2):
                    selected_exact = ranked_exact[0]

            if selected_exact:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    exact_mapping = await _tmdb_exact_match_mapping(
                        client, api_key, selected_exact, title_data
                    )
                if exact_mapping:
                    tmdb_id, season = exact_mapping
                elif _tmdb_has_explicit_season_marker(title_data):
                    # Do not let a season-specific AniList entry fall through
                    # to fuzzy matching and accidentally become Season 1.
                    candidates = [
                        item for item in candidates
                        if item.get("tmdbId") != selected_exact.get("tmdbId")
                    ]

            # For sequel/arc entries, test the candidate's actual TMDB season
            # names. Never silently fall back to Season 1 of its parent show.
            if not tmdb_id:
                contained_matches = [
                    item for item in candidates
                    if item.get("containedTitle") and item["score"] >= 0.94
                ]
                season_matches = []
                async with httpx.AsyncClient(timeout=15.0) as client:
                    season_candidates = contained_matches[:8]
                    resolved_seasons = await asyncio.gather(*(
                        _tmdb_resolve_season(client, api_key, candidate, title_data)
                        for candidate in season_candidates
                    ))
                    season_matches = [
                        (candidate, matched_season)
                        for candidate, matched_season in zip(season_candidates, resolved_seasons)
                        if matched_season
                    ]

                season_matches.sort(
                    key=lambda pair: pair[1]["score"],
                    reverse=True,
                )
                if len(season_matches) == 1:
                    tmdb_id = season_matches[0][0]["tmdbId"]
                    season = season_matches[0][1]["seasonNumber"]
                elif len(season_matches) > 1:
                    best_candidate, best_season = season_matches[0]
                    second_candidate, second_season = season_matches[1]
                    if best_season["score"] - second_season["score"] >= 0.06:
                        tmdb_id = best_candidate["tmdbId"]
                        season = best_season["seasonNumber"]

            # Fuzzy matches are allowed only when the candidate is not merely
            # a parent franchise title. That parent case requires season proof.
            if not tmdb_id:
                fuzzy_candidates = [
                    item for item in candidates
                    if not item.get("containedTitle")
                ]
                if fuzzy_candidates:
                    top = fuzzy_candidates[0]
                    second_score = fuzzy_candidates[1]["score"] if len(fuzzy_candidates) > 1 else 0
                    clearly_best = top["score"] >= 0.90 and (top["score"] - second_score) >= 0.08
                    single_strong = len(fuzzy_candidates) == 1 and top["score"] >= 0.93
                    if clearly_best or single_strong:
                        tmdb_id = top["tmdbId"]
                        season = 1

    if not tmdb_id:
        print(
            "[TMDB MATCH] no confident match for AniList",
            anilist_id,
            "titles=", titles,
            "candidates=",
            [
                {
                    "id": item.get("tmdbId"),
                    "title": item.get("title"),
                    "score": item.get("score"),
                    "match": item.get("titleMatch"),
                    "yearDelta": item.get("yearDelta"),
                }
                for item in candidates[:5]
            ],
        )
        raise HTTPException(
            status_code=409,
            detail={
                "message": "TMDB could not confidently identify the correct show and season; playback was not started.",
                "candidates": candidates[:8],
                "anilistTitles": titles,
                "year": year,
                "anilistId": anilist_id,
            },
        )

    selected_candidate = next(
        (item for item in candidates if item.get("tmdbId") == tmdb_id),
        None,
    )
    tmdb_title = (
        selected_candidate.get("title") or selected_candidate.get("originalTitle")
        if selected_candidate else None
    )

    mapped_episode = 1 if is_movie else episode + episode_offset
    if is_movie:
        embeds = [
            {"name": "VidSrc", "url": f"https://vidsrc.mov/embed/movie/{tmdb_id}"},
            {"name": "VidCore", "url": f"https://vidcore.org/embed/movie/{tmdb_id}"},
            {"name": "2Embed", "url": f"https://www.2embed.cc/embed/{tmdb_id}"},
            {"name": "VidLink", "url": f"https://vidlink.pro/movie/{tmdb_id}"},
        ]
    else:
        embeds = [
            {"name": "VidSrc", "url": f"https://vidsrc.mov/embed/tv/{tmdb_id}/{season}/{mapped_episode}"},
            {"name": "VidCore", "url": f"https://vidcore.org/embed/tv/{tmdb_id}/{season}/{mapped_episode}"},
            {"name": "VidCore (series route fallback)", "url": f"https://vidcore.org/embed/series/{tmdb_id}/{season}/{mapped_episode}"},
            {"name": "2Embed", "url": f"https://www.2embed.cc/embedtv/{tmdb_id}&s={season}&e={mapped_episode}"},
            {"name": "VidLink", "url": f"https://vidlink.pro/tv/{tmdb_id}/{season}/{mapped_episode}"},
        ]

    return {
        "anilistId": anilist_id,
        "malId": media.get("idMal"),
        "tmdbId": tmdb_id,
        "tmdbTitle": tmdb_title,
        "season": season,
        "episode": mapped_episode,
        "mediaType": kind,
        "mappingSource": "verified-override" if override else "automatic",
        "sources": embeds,
    }
