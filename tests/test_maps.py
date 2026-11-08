"""Provider-boundary failures use synthetic HTTP payloads, not live Amap data."""

import httpx2
import pytest

from tripweave.maps import MapClient, MapError


@pytest.mark.parametrize("payload", [[], {"status": "1", "pois": None}])
async def test_invalid_provider_shape_has_safe_error(payload):
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, json=payload))
    ) as client:
        maps = MapClient(client, "private-test-key")
        with pytest.raises(MapError) as failure:
            await maps.search("景点", "上海")
        assert "private-test-key" not in str(failure.value)


@pytest.mark.parametrize("duration", ["nan", "inf", "unknown", "-1"])
async def test_invalid_route_duration_cannot_be_scheduled(duration):
    body = {"status": "1", "route": {"paths": [{"duration": duration, "distance": "500"}]}}
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, json=body))
    ) as client:
        maps = MapClient(client, "test-key")
        with pytest.raises(MapError, match="有效耗时"):
            await maps.route(
                {"id": "a", "name": "A", "longitude": 121.4, "latitude": 31.2},
                {"id": "b", "name": "B", "longitude": 121.5, "latitude": 31.3},
                "上海",
                "mixed",
            )


async def test_missing_walk_can_use_real_transit_response_only_in_mixed_mode():
    calls = []

    def respond(request):
        calls.append(request.url.path)
        route = (
            {"paths": []}
            if request.url.path.endswith("walking")
            else {"transits": [{"duration": "900", "distance": "4000"}]}
        )
        return httpx2.Response(200, json={"status": "1", "route": route})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        maps = MapClient(client, "test-key")
        a = {"id": "a", "name": "A", "longitude": 121.4, "latitude": 31.2}
        b = {"id": "b", "name": "B", "longitude": 121.5, "latitude": 31.3}
        leg = await maps.route(a, b, "上海", "mixed")
        assert leg["duration_minutes"] == 15 and leg["mode"] == "公共交通"
        assert len(calls) == 2
        with pytest.raises(MapError, match="可用路线"):
            await maps.route(a, b, "上海", "walking")
        assert len(calls) == 3


async def test_invalid_coordinates_are_not_registered_as_evidence():
    async with httpx2.AsyncClient() as client:
        maps = MapClient(client, "test-key")
        for location in ("nan,31", "181,31", "121,91", [], None):
            assert maps.normalize({"id": "bad", "name": "bad", "location": location}) is None
        assert not maps.places
