import unittest

from src.endpoints import (
    _tmdb_candidates,
    _tmdb_exact_match_mapping,
    _tmdb_resolve_season,
    _tmdb_score,
    _tmdb_season_hints,
    _tmdb_title_aliases,
    _TMDB_CANDIDATE_CACHE,
)


MHA_CANDIDATE = {
    "tmdbId": 65930,
    "title": "My Hero Academia",
    "originalTitle": "My Hero Academia",
}
TMDB_SEASONS = [
    {
        "season_number": number,
        "name": f"Season {number}",
        "original_name": f"Season {number}",
        "air_date": None,
        "episode_count": 13,
    }
    for number in range(1, 9)
]


class FakeResponse:
    def __init__(self, payload=None):
        self.payload = payload or {"seasons": TMDB_SEASONS}

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class FakeTMDBClient:
    async def get(self, url, params=None):
        return FakeResponse()


class FakeMHAClient:
    async def get(self, url, params=None):
        if "/search/tv" in url:
            query = (params or {}).get("query", "")
            if query == "My Hero Academia":
                return FakeResponse({
                    "results": [
                        {
                            "id": 65930,
                            "name": "My Hero Academia",
                            "original_name": "My Hero Academia",
                            "first_air_date": "2016-04-03",
                        },
                        {
                            "id": 280110,
                            "name": "My Hero Academia: Vigilantes",
                            "original_name": "My Hero Academia: Vigilantes",
                            "first_air_date": "2025-01-01",
                        },
                    ]
                })
            if query in ("My Hero Academia Season 2", "Boku no Hero Academia 2"):
                return FakeResponse({
                    "results": [{
                        "id": 280110,
                        "name": "My Hero Academia: Vigilantes",
                        "original_name": "My Hero Academia: Vigilantes",
                        "first_air_date": "2025-01-01",
                    }]
                })
            return FakeResponse({"results": []})

        if "/tv/65930" in url:
            return FakeResponse({"seasons": TMDB_SEASONS})
        return FakeResponse({"results": []})


class MyHeroAcademiaMatchingTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_series_title_does_not_match_vigilantes(self):
        titles = ["My Hero Academia"]
        vigilantes = {
            "name": "My Hero Academia: Vigilantes",
            "original_name": "My Hero Academia: Vigilantes",
            "first_air_date": "2025-01-01",
        }

        score, _, exact, contained, _, match_type = _tmdb_score(
            vigilantes, titles, 2016
        )

        self.assertFalse(exact)
        self.assertFalse(contained)
        self.assertNotEqual(match_type, "contained")
        self.assertLess(score, 0.90)

    async def test_main_series_season_one_is_exact(self):
        titles, aliases = _tmdb_title_aliases({
            "english": "My Hero Academia",
            "romaji": "Boku no Hero Academia",
            "native": None,
        })
        score, _, exact, contained, _, _ = _tmdb_score(
            {
                "name": "My Hero Academia",
                "original_name": "My Hero Academia",
                "first_air_date": "2016-04-03",
            },
            aliases,
            2016,
        )

        self.assertTrue(titles)
        self.assertTrue(exact)
        self.assertFalse(contained)
        self.assertGreaterEqual(score, 1.0)

    async def test_numbered_season_titles_add_parent_title_search_aliases(self):
        title_data = {
            "english": "My Hero Academia Season 2",
            "romaji": "Boku no Hero Academia 2",
            "native": "僕のヒーローアカデミア２",
        }
        _, aliases = _tmdb_title_aliases(title_data)

        self.assertIn("My Hero Academia", aliases)
        self.assertIn("Boku no Hero Academia", aliases)
        self.assertIn("僕のヒーローアカデミア", aliases)

    async def test_season_two_search_uses_parent_series_and_maps_season_two(self):
        title_data = {
            "english": "My Hero Academia Season 2",
            "romaji": "Boku no Hero Academia 2",
            "native": "僕のヒーローアカデミア２",
        }
        _, aliases = _tmdb_title_aliases(title_data)
        _TMDB_CANDIDATE_CACHE.clear()

        candidates = await _tmdb_candidates(
            FakeMHAClient(), "test-key", "tv", aliases, 2017
        )
        parent = next(
            (item for item in candidates if item["tmdbId"] == 65930),
            None,
        )
        vigilantes = next(
            (item for item in candidates if item["tmdbId"] == 280110),
            None,
        )

        self.assertIsNotNone(parent)
        self.assertTrue(parent["exactTitle"])
        self.assertIsNotNone(vigilantes)
        self.assertFalse(vigilantes["exactTitle"])

        mapping = await _tmdb_exact_match_mapping(
            FakeMHAClient(), "test-key", parent, title_data
        )
        self.assertEqual(mapping, (65930, 2))

    async def test_plain_main_series_title_maps_to_season_one(self):
        mapping = await _tmdb_exact_match_mapping(
            FakeTMDBClient(),
            "test-key",
            MHA_CANDIDATE,
            {
                "english": "My Hero Academia",
                "romaji": "Boku no Hero Academia",
                "native": None,
            },
        )
        self.assertEqual(mapping, (65930, 1))

    async def test_numbered_mha_seasons_resolve_to_matching_tmdb_season(self):
        client = FakeTMDBClient()
        for number in range(2, 8):
            suffix = (
                "2nd" if number == 2 else
                "3rd" if number == 3 else
                f"{number}th"
            )
            title = f"My Hero Academia {suffix} Season"
            with self.subTest(title=title):
                resolved = await _tmdb_resolve_season(
                    client,
                    "test-key",
                    MHA_CANDIDATE,
                    {"english": title, "romaji": title, "native": None},
                )
                self.assertIsNotNone(resolved)
                self.assertEqual(resolved["seasonNumber"], number)

    async def test_mha_final_season_resolves_to_tmdb_season_eight(self):
        title = "My Hero Academia: Final Season"
        hints = _tmdb_season_hints(
            {"english": title, "romaji": title, "native": None},
            MHA_CANDIDATE,
        )
        resolved = await _tmdb_exact_match_mapping(
            FakeTMDBClient(),
            "test-key",
            MHA_CANDIDATE,
            {"english": title, "romaji": title, "native": None},
        )

        self.assertIn("season8", hints)
        self.assertEqual(resolved, (65930, 8))

    async def test_vigilantes_does_not_resolve_to_a_main_series_season(self):
        title = "My Hero Academia: Vigilantes"
        resolved = await _tmdb_resolve_season(
            FakeTMDBClient(),
            "test-key",
            MHA_CANDIDATE,
            {"english": title, "romaji": title, "native": None},
        )

        self.assertIsNone(resolved)


if __name__ == "__main__":
    unittest.main()
