import unittest

from src.endpoints import (
    _tmdb_resolve_season,
    _tmdb_score,
    _tmdb_season_hints,
    _tmdb_title_aliases,
)


MHA_CANDIDATE = {
    "tmdbId": "mock-mha-series",
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
    def json(self):
        return {"seasons": TMDB_SEASONS}

    def raise_for_status(self):
        return None


class FakeTMDBClient:
    async def get(self, url, params=None):
        return FakeResponse()


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
        resolved = await _tmdb_resolve_season(
            FakeTMDBClient(),
            "test-key",
            MHA_CANDIDATE,
            {"english": title, "romaji": title, "native": None},
        )

        self.assertIn("season8", hints)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["seasonNumber"], 8)

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
