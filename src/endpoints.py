from fastapi import APIRouter, Query, HTTPException
from typing import Optional
import httpx
import base64

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


@router.get("/tmdb-match/{anilist_id}")
async def get_tmdb_matches(anilist_id: int):
    """Find possible TMDB records for an AniList anime without guessing a match."""
    import os

    api_key = os.getenv("TMDB_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="TMDB_API_KEY is not configured")

    gql = """
    query ($id: Int) {
        Media(id: $id, type: ANIME) {
            id
            idMal
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
    titles = []
    for value in (title_data.get("english"), title_data.get("romaji")):
        if value and value not in titles:
            titles.append(value)
    if not titles:
        raise HTTPException(status_code=404, detail="anime has no searchable title")

    media_format = media.get("format")
    endpoint = "movie" if media_format == "MOVIE" else "tv"
    year = (media.get("startDate") or {}).get("year")
    matches = []
    seen_ids = set()

    async with httpx.AsyncClient(timeout=12.0) as client:
        for title in titles:
            params = {"api_key": api_key, "query": title, "include_adult": "false"}
            if year:
                params["year" if endpoint == "movie" else "first_air_date_year"] = year
            try:
                response = await client.get(
                    f"https://api.themoviedb.org/3/search/{endpoint}",
                    params=params,
                )
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPStatusError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"TMDB search failed with status {exc.response.status_code}",
                )
            except httpx.HTTPError:
                raise HTTPException(status_code=502, detail="could not reach TMDB")

            for item in payload.get("results", []):
                tmdb_id = item.get("id")
                if not tmdb_id or tmdb_id in seen_ids:
                    continue
                seen_ids.add(tmdb_id)
                matches.append({
                    "tmdbId": tmdb_id,
                    "title": item.get("title") or item.get("name"),
                    "originalTitle": item.get("original_title") or item.get("original_name"),
                    "releaseDate": item.get("release_date") or item.get("first_air_date"),
                    "overview": item.get("overview"),
                    "posterPath": item.get("poster_path"),
                    "mediaType": endpoint,
                })

    return {
        "anilistId": media["id"],
        "malId": media.get("idMal"),
        "anilistTitle": title_data,
        "format": media_format,
        "year": year,
        "matches": matches[:10],
        "requiresSelection": len(matches) > 0,
        "note": "Choose the matching TMDB record; results are candidates, not a guaranteed ID match.",
    }


@router.get("/playback/{anilist_id}")
async def get_embed_playback(
    anilist_id: int,
    episode: int = Query(1, ge=1, le=1000),
):
    """Return HTTPS embed candidates after a conservative AniList-to-TMDB match."""
    import os
    import re
    import unicodedata

    api_key = os.getenv("TMDB_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="TMDB_API_KEY is not configured")

    gql = """
    query ($id: Int) {
        Media(id: $id, type: ANIME) {
            id
            idMal
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
    titles = []
    for value in (title_data.get("english"), title_data.get("romaji"), title_data.get("native")):
        if value and value not in titles:
            titles.append(value)
    # Anime databases often include the franchise name before a colon or dash.
    # Search the distinctive subtitle too (e.g. "Steel Ball Run").
    aliases = list(titles)
    for value in titles:
        for separator in (":", "：", "—", "–", " - "):
            if separator in value:
                tail = value.rsplit(separator, 1)[-1].strip()
                if len(tail) >= 5 and tail not in aliases:
                    aliases.append(tail)
    if not aliases:
        raise HTTPException(status_code=404, detail="anime has no searchable title")

    def normalize(value):
        value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
        return re.sub(r"[^a-z0-9]+", "", value)

    year = (media.get("startDate") or {}).get("year")
    is_movie = media.get("format") == "MOVIE"
    kind = "movie" if is_movie else "tv"
    matches = {}
    async with httpx.AsyncClient(timeout=12.0) as client:
        for title in aliases:
            # First use the known release year; only broaden the year if no safe
            # result is found after all exact-year title searches.
            params = {"api_key": api_key, "query": title, "include_adult": "false"}
            if year:
                params["year" if is_movie else "first_air_date_year"] = year
            try:
                response = await client.get(f"https://api.themoviedb.org/3/search/{kind}", params=params)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise HTTPException(status_code=502, detail=f"TMDB search failed with status {exc.response.status_code}")
            except httpx.HTTPError:
                raise HTTPException(status_code=502, detail="could not reach TMDB")
            for item in response.json().get("results", []):
                name = item.get("title") or item.get("name") or ""
                original = item.get("original_title") or item.get("original_name") or ""
                date = item.get("release_date") or item.get("first_air_date") or ""
                item_year = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None
                normalized_names = (normalize(name), normalize(original))
                exact_title = any(
                    normalize(candidate) in normalized_names
                    or (len(normalize(candidate)) >= 7 and any(normalize(candidate) in n for n in normalized_names))
                    for candidate in aliases
                )
                exact_year = not year or item_year == year
                if exact_title and exact_year and item.get("id"):
                    matches[item["id"]] = {
                        "tmdbId": item["id"],
                        "title": name,
                        "year": item_year,
                        "overview": item.get("overview"),
                    }

    # If AniList's year is off by one (common for delayed premieres), retry
    # only when the exact-year pass found nothing, and still require a title match.
    if not matches and year:
        async with httpx.AsyncClient(timeout=12.0) as client:
            for title in aliases:
                params = {"api_key": api_key, "query": title, "include_adult": "false"}
                try:
                    response = await client.get(f"https://api.themoviedb.org/3/search/{kind}", params=params)
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise HTTPException(status_code=502, detail=f"TMDB search failed with status {exc.response.status_code}")
                except httpx.HTTPError:
                    raise HTTPException(status_code=502, detail="could not reach TMDB")
                for item in response.json().get("results", []):
                    name = item.get("title") or item.get("name") or ""
                    original = item.get("original_title") or item.get("original_name") or ""
                    date = item.get("release_date") or item.get("first_air_date") or ""
                    item_year = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None
                    normalized_names = (normalize(name), normalize(original))
                    title_match = any(
                        normalize(candidate) in normalized_names
                        or (len(normalize(candidate)) >= 7 and any(normalize(candidate) in n for n in normalized_names))
                        for candidate in aliases
                    )
                    year_close = item_year is not None and abs(item_year - year) <= 1
                    if title_match and year_close and item.get("id"):
                        matches[item["id"]] = {
                            "tmdbId": item["id"],
                            "title": name,
                            "year": item_year,
                            "overview": item.get("overview"),
                        }

    if len(matches) != 1:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "TMDB match is not unique; refusing to play the wrong title.",
                "candidates": list(matches.values())[:8],
                "anilistTitles": titles,
                "year": year,
            },
        )

    tmdb_id = next(iter(matches))
    if is_movie:
        embeds = [
            {"name": "VidSrc", "url": f"https://vidsrc.mov/embed/movie/{tmdb_id}"},
            {"name": "VidCore", "url": f"https://vidcore.org/embed/movie/{tmdb_id}"},
            {"name": "2Embed", "url": f"https://www.2embed.cc/embed/{tmdb_id}"},
            {"name": "VidLink", "url": f"https://vidlink.pro/movie/{tmdb_id}"},
        ]
    else:
        embeds = [
            {"name": "VidSrc", "url": f"https://vidsrc.mov/embed/tv/{tmdb_id}/1/{episode}"},
            {"name": "VidCore", "url": f"https://vidcore.org/embed/tv/{tmdb_id}/1/{episode}"},
            {"name": "VidCore (series route fallback)", "url": f"https://vidcore.org/embed/series/{tmdb_id}/1/{episode}"},
            {"name": "2Embed", "url": f"https://www.2embed.cc/embedtv/{tmdb_id}&s=1&e={episode}"},
            {"name": "VidLink", "url": f"https://vidlink.pro/tv/{tmdb_id}/1/{episode}"},
        ]

    return {
        "anilistId": anilist_id,
        "malId": media.get("idMal"),
        "tmdbId": tmdb_id,
        "episode": 1 if is_movie else episode,
        "mediaType": kind,
        "sources": embeds,
    }
