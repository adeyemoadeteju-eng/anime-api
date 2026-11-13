import pytest
from fastapi import HTTPException

from src.dub_finder import (
    _normalize_region,
    _select_tmdb_candidate,
    _shape_watch_providers,
)


def test_region_defaults_to_canada_and_normalizes_case():
    assert _normalize_region("ca") == "CA"
    assert _normalize_region(" US ") == "US"


@pytest.mark.parametrize("value", ["", "C", "CAN", "1A", "CA1", "C-A"])
def test_region_rejects_invalid_codes(value):
    with pytest.raises(HTTPException) as exc:
        _normalize_region(value)
    assert exc.value.status_code == 400


def test_candidate_prefers_unique_exact_match():
    candidates = [
        {"tmdbId": 12, "exactTitle": False, "containedTitle": False, "score": 0.91},
        {"tmdbId": 34, "exactTitle": True, "containedTitle": False, "score": 1.0},
    ]
    assert _select_tmdb_candidate(candidates)["tmdbId"] == 34


def test_candidate_rejects_parent_franchise_only_match():
    candidates = [
        {"tmdbId": 12, "exactTitle": False, "containedTitle": True, "score": 0.98},
    ]
    assert _select_tmdb_candidate(candidates) is None


def test_candidate_requires_clear_fuzzy_winner():
    clear = [
        {"tmdbId": 12, "exactTitle": False, "containedTitle": False, "score": 0.96},
        {"tmdbId": 34, "exactTitle": False, "containedTitle": False, "score": 0.82},
    ]
    ambiguous = [
        {"tmdbId": 12, "exactTitle": False, "containedTitle": False, "score": 0.96},
        {"tmdbId": 34, "exactTitle": False, "containedTitle": False, "score": 0.93},
    ]
    assert _select_tmdb_candidate(clear)["tmdbId"] == 12
    assert _select_tmdb_candidate(ambiguous) is None


def test_provider_categories_are_merged_without_duplicates():
    country = {
        "link": "https://www.justwatch.com/ca/tv-show/example",
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
    assert [item["id"] for item in providers] == [9, 8]
    service_a = next(item for item in providers if item["id"] == 8)
    assert service_a["availabilityTypes"] == ["Subscription", "Rent"]
    assert service_a["logoUrl"] == "https://image.tmdb.org/t/p/w92/a.png"


def test_empty_country_has_no_providers():
    assert _shape_watch_providers(None) == []
