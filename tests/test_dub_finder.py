import unittest

from fastapi import HTTPException

from src.dub_finder import (
    _normalize_region,
    _select_tmdb_candidate,
    _shape_watch_providers,
)


class DubFinderHelperTests(unittest.TestCase):
    def test_region_defaults_to_canada_and_normalizes_case(self):
        self.assertEqual(_normalize_region("ca"), "CA")
        self.assertEqual(_normalize_region(" US "), "US")

    def test_region_rejects_invalid_codes(self):
        for value in ("", "C", "CAN", "1A", "CA1", "C-A"):
            with self.subTest(value=value):
                with self.assertRaises(HTTPException) as caught:
                    _normalize_region(value)
                self.assertEqual(caught.exception.status_code, 400)

    def test_candidate_prefers_unique_exact_match(self):
        candidates = [
            {"tmdbId": 12, "exactTitle": False, "containedTitle": False, "score": 0.91},
            {"tmdbId": 34, "exactTitle": True, "containedTitle": False, "score": 1.0},
        ]
        self.assertEqual(_select_tmdb_candidate(candidates)["tmdbId"], 34)

    def test_candidate_rejects_parent_franchise_only_match(self):
        candidates = [
            {"tmdbId": 12, "exactTitle": False, "containedTitle": True, "score": 0.98},
        ]
        self.assertIsNone(_select_tmdb_candidate(candidates))

    def test_candidate_requires_clear_fuzzy_winner(self):
        clear = [
            {"tmdbId": 12, "exactTitle": False, "containedTitle": False, "score": 0.96},
            {"tmdbId": 34, "exactTitle": False, "containedTitle": False, "score": 0.82},
        ]
        ambiguous = [
            {"tmdbId": 12, "exactTitle": False, "containedTitle": False, "score": 0.96},
            {"tmdbId": 34, "exactTitle": False, "containedTitle": False, "score": 0.93},
        ]
        self.assertEqual(_select_tmdb_candidate(clear)["tmdbId"], 12)
        self.assertIsNone(_select_tmdb_candidate(ambiguous))

    def test_provider_categories_are_merged_without_duplicates(self):
        country = {
            "flatrate": [
                {
                    "provider_id": 8,
                    "provider_name": "Service A",
                    "logo_path": "/a.png",
                    "display_priority": 2,
                }
            ],
            "rent": [
                {
                    "provider_id": 8,
                    "provider_name": "Service A",
                    "logo_path": "/a.png",
                    "display_priority": 2,
                },
                {
                    "provider_id": 9,
                    "provider_name": "Service B",
                    "logo_path": "/b.png",
                    "display_priority": 1,
                },
            ],
        }
        providers = _shape_watch_providers(country)
        self.assertEqual([item["id"] for item in providers], [9, 8])
        service_a = next(item for item in providers if item["id"] == 8)
        self.assertEqual(
            service_a["availabilityTypes"], ["Subscription", "Rent"]
        )
        self.assertEqual(
            service_a["logoUrl"], "https://image.tmdb.org/t/p/w92/a.png"
        )

    def test_empty_country_has_no_providers(self):
        self.assertEqual(_shape_watch_providers(None), [])


if __name__ == "__main__":
    unittest.main()
