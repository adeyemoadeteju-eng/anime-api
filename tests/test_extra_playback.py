import os
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException
from src import endpoints, extra_playback


class ExtraPlaybackTests(unittest.IsolatedAsyncioTestCase):
    def test_animeembed_uses_audio_and_episode(self):
        url = extra_playback.build_animeembed_url(151807, 3, "dub")
        parsed = urlparse(url)
        self.assertEqual(parsed.netloc, "animeembed.com")
        self.assertEqual(parsed.path, "/embed/ani/151807/3")
        self.assertEqual(parse_qs(parsed.query)["track"], ["dub"])

    def test_aniembed_uses_documented_language_option(self):
        url = extra_playback.build_aniembed_url(151807, 3, "dub")
        parsed = urlparse(url)
        self.assertEqual(parsed.netloc, "aniembed.se")
        self.assertEqual(parsed.path, "/e/151807/3")
        query = parse_qs(parsed.query)
        self.assertEqual(query["lang"], ["dub"])
        self.assertEqual(query["autoplay"], ["0"])
        self.assertEqual(query["t"], ["0"])

    def test_vidy_uses_documented_anilist_route_and_leaves_autoplay_off(self):
        url = extra_playback.build_vidy_url(151807, 3)
        parsed = urlparse(url)
        self.assertEqual(parsed.netloc, "www.vidy.st")
        self.assertEqual(parsed.path, "/anime/151807/3")
        query = parse_qs(parsed.query)
        self.assertEqual(query["color"], ["9b87ff"])
        self.assertEqual(query["episodeSelector"], ["true"])
        self.assertEqual(query["nextEpisode"], ["true"])
        self.assertNotIn("autoplay", query)

    def test_recent_catalogue_title_match_requires_unique_id(self):
        media = {"title": {"english": "Solo Leveling", "romaji": "Ore dake Level Up na Ken"}}
        rows = [{"id": 42, "title": "Solo Leveling"}, {"id": 43, "title": "Another show"}]
        self.assertEqual(extra_playback.match_recent_series_id(media, rows), "42")
        rows.append({"id": 44, "title": "Solo-Leveling"})
        self.assertIsNone(extra_playback.match_recent_series_id(media, rows))

    def test_episode_rows_accept_array_or_object_shapes(self):
        array_payload = {"episodes": [{"number": "1", "embed_url": {"sub": "https://x.test/1"}}]}
        map_payload = {"episodes": {"2": {"embed_url": {"dub": "https://x.test/2"}}}}
        self.assertEqual(extra_playback.extract_episode_row(array_payload, 1)["number"], "1")
        self.assertEqual(int(extra_playback.extract_episode_row(map_payload, 2)["number"]), 2)

    def test_external_embed_url_requires_https(self):
        self.assertTrue(extra_playback.is_safe_embed_url("https://player.example/embed/1"))
        self.assertFalse(extra_playback.is_safe_embed_url("http://player.example/embed/1"))
        self.assertFalse(extra_playback.is_safe_embed_url("javascript:alert(1)"))

    async def test_movie_embed_urls_always_use_episode_one(self):
        with patch("src.extra_playback._anikoto_source", new=AsyncMock(return_value=None)):
            sources = await extra_playback.get_extra_playback_sources({"format": "MOVIE"}, 151807, 7, "sub")
        self.assertEqual(len(sources), 3)
        self.assertIn("/151807/1", sources[0]["url"])
        self.assertIn("/151807/1", sources[1]["url"])
        self.assertIn("/anime/151807/1", sources[2]["url"])

    async def test_anikoto_source_is_optional_when_catalogue_does_not_match(self):
        with patch("src.extra_playback._anikoto_source", new=AsyncMock(return_value=None)):
            sources = await extra_playback.get_extra_playback_sources({"format": "TV"}, 151807, 4, "dub")
        self.assertEqual([source["name"] for source in sources], ["AnimeEmbed", "AniEmbed", "Vidy"])
        self.assertTrue(all(source["audio"] == "dub" for source in sources[:2]))
        self.assertNotIn("audio", sources[2])

    async def test_playback_endpoint_returns_embeds_without_tmdb_key(self):
        media = {"id": 151807, "idMal": None, "format": "TV",
                 "title": {"english": "Test Anime", "romaji": "Test Anime"}}
        extras = [
            {"name": "AnimeEmbed", "url": "https://animeembed.com/embed/ani/151807/1?track=sub"},
            {"name": "AniEmbed", "url": "https://aniembed.se/e/151807/1?lang=sub"},
        ]
        with (
            patch.dict(os.environ, {"TMDB_API_KEY": ""}),
            patch("src.endpoints.anilist_query", new=AsyncMock(return_value={"Media": media})),
            patch("src.endpoints.get_extra_playback_sources", new=AsyncMock()) as provider_lookup,
        ):
            result = await endpoints.get_embed_playback(151807, 1, "sub")
        self.assertEqual(result["sources"], extras)
        self.assertEqual(result["mappingSource"], "AniList")


    async def test_playback_rejects_adult_media_before_provider_lookup(self):
        media = {
            "id": 151807, "idMal": None, "format": "TV", "isAdult": True,
            "genres": ["Action"], "tags": [],
            "title": {"english": "Adult Test", "romaji": "Adult Test"},
        }
        with (
            patch.dict(os.environ, {"TMDB_API_KEY": ""}),
            patch("src.endpoints.anilist_query", new=AsyncMock(return_value={"Media": media})),
            patch("src.endpoints.get_extra_playback_sources", new=AsyncMock()) as provider_lookup,
        ):
            with self.assertRaises(HTTPException) as raised:
                await endpoints.get_embed_playback(151807, 1, "sub")
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail, "Adult and hentai titles are excluded from AniOrbit.")
        provider_lookup.assert_not_awaited()

    def test_playback_maturity_check_catches_hentai_and_adult_tags(self):
        self.assertTrue(endpoints._is_adult_media({"genres": ["Action", "Hentai"]}))
        self.assertTrue(endpoints._is_adult_media({"tags": [{"name": "Adult", "isAdult": True}]}))
        self.assertTrue(endpoints._is_adult_media({"tags": [{"name": "hentai", "isAdult": False}]}))
        self.assertFalse(endpoints._is_adult_media({"genres": ["Action"], "tags": [{"name": "Adventure", "isAdult": False}]}))

    async def test_playback_rejects_unknown_audio_mode(self):
        with patch.dict(os.environ, {"TMDB_API_KEY": ""}):
            with self.assertRaises(HTTPException) as raised:
                await endpoints.get_embed_playback(151807, 1, "other")
        self.assertEqual(raised.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
