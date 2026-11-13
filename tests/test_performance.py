import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from src import extractor
from src import endpoints


class FakeResponse:
    status_code = 200

    def json(self):
        return {"data": {"Media": {"id": 42, "title": "Cached title"}}}


class FakeAniListClient:
    is_closed = False

    def __init__(self):
        self.calls = 0

    async def post(self, *args, **kwargs):
        self.calls += 1
        await asyncio.sleep(0.01)
        return FakeResponse()


class AniListQueryCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_client = extractor._HTTP_CLIENT
        self.fake_client = FakeAniListClient()
        extractor._HTTP_CLIENT = self.fake_client
        extractor._QUERY_CACHE.clear()
        extractor._QUERY_IN_FLIGHT.clear()

    async def asyncTearDown(self):
        extractor._QUERY_CACHE.clear()
        extractor._QUERY_IN_FLIGHT.clear()
        extractor._HTTP_CLIENT = self.previous_client

    async def test_repeated_metadata_query_uses_cached_result(self):
        query = "query ($id: Int) { Media(id: $id, type: ANIME) { id } }"

        first = await extractor.anilist_query(query, {"id": 42})
        second = await extractor.anilist_query(query, {"id": 42})

        self.assertEqual(self.fake_client.calls, 1)
        self.assertEqual(first, second)

    async def test_concurrent_identical_queries_share_one_request(self):
        query = "query ($id: Int) { Media(id: $id, type: ANIME) { id } }"

        first, second = await asyncio.gather(
            extractor.anilist_query(query, {"id": 42}),
            extractor.anilist_query(query, {"id": 42}),
        )

        self.assertEqual(self.fake_client.calls, 1)
        self.assertEqual(first, second)

    async def test_callers_cannot_mutate_the_cached_copy(self):
        query = "query ($id: Int) { Media(id: $id, type: ANIME) { id } }"

        first = await extractor.anilist_query(query, {"id": 42})
        first["Media"]["id"] = 999
        second = await extractor.anilist_query(query, {"id": 42})

        self.assertEqual(second["Media"]["id"], 42)
        self.assertEqual(self.fake_client.calls, 1)


class HomeEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_home_returns_both_compact_catalogue_rails(self):
        async def fake_collection(sort_type, status=None, page=1, per_page=20, fields=None):
            return {"results": [{"sort": sort_type, "status": status, "fields": fields}]}

        with patch("src.endpoints._fetch_collection", new=AsyncMock(side_effect=fake_collection)):
            result = await endpoints.get_home()

        self.assertEqual(result["popular"][0]["sort"], "POPULARITY_DESC")
        self.assertEqual(result["recent"][0]["sort"], "START_DATE_DESC")
        self.assertEqual(result["popular"][0]["fields"], endpoints.MEDIA_HOME_FIELDS)
        self.assertEqual(result["recent"][0]["fields"], endpoints.MEDIA_HOME_FIELDS)

    async def test_home_keeps_one_rail_when_the_other_fails(self):
        async def fake_collection(sort_type, status=None, page=1, per_page=20, fields=None):
            if sort_type == "POPULARITY_DESC":
                raise RuntimeError("popular endpoint unavailable")
            return {"results": [{"id": 7}]}

        with patch("src.endpoints._fetch_collection", new=AsyncMock(side_effect=fake_collection)):
            result = await endpoints.get_home()

        self.assertEqual(result["popular"], [])
        self.assertEqual(result["recent"], [{"id": 7}])
        self.assertTrue(result["partial"])


if __name__ == "__main__":
    unittest.main()
