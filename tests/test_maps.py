"""Provider-boundary failures use synthetic HTTP payloads, not live Amap data."""

import httpx2
import pytest

from tripweave.maps import MapClient, MapError


async def test_picker_search_preserves_real_unique_photos_and_rating_without_extra_requests():
    calls = []
    photos = [
        {"url": "https://photos.example/museum.jpg"},
        {"url": "https://photos.example/museum.jpg"},
        {"url": "javascript:alert(1)"},
        {"url": "https://[invalid"},
        {"url": "https://user:password@photos.example/private.jpg"},
        {"url": "https://photos.example/bad\nimage.jpg"},
    ] + [{"url": f"https://photos.example/real-{index}.jpg"} for index in range(7)]

    def respond(request):
        calls.append(request)
        return httpx2.Response(
            200,
            json={
                "status": "1",
                "pois": [
                    {
                        "id": "museum",
                        "name": "测试博物馆",
                        "location": "121.48,31.23",
                        "photos": photos,
                        "biz_ext": {"rating": "4.7"},
                    }
                ],
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        maps = MapClient(client, "test-key")
        places = await maps.search("博物馆", "上海", detailed=True)
        assert len(calls) == 1 and calls[0].url.params["extensions"] == "all"
        assert places[0]["rating"] == 4.7
        assert places[0]["photos"] == ["https://photos.example/museum.jpg"] + [
            f"https://photos.example/real-{index}.jpg" for index in range(4)
        ]
        invalid = maps.normalize(
            {
                "id": "other",
                "name": "测试馆",
                "location": "121,31",
                "photos": [],
                "biz_ext": {"rating": "nan"},
            }
        )
        assert invalid["photos"] == [] and invalid["rating"] is None


async def test_place_resolution_tolerates_bracket_width_but_rejects_ambiguous_names():
    places = [
        {"id": "square", "name": "上海人民广场", "location": "121.47,31.23"},
        {"id": "museum", "name": "上海博物馆(人民广场馆)", "location": "121.48,31.23"},
    ]
    transport = httpx2.MockTransport(
        lambda _: httpx2.Response(200, json={"status": "1", "pois": places})
    )
    async with httpx2.AsyncClient(transport=transport) as client:
        maps = MapClient(client, "test-key")
        query = "上海博物馆（人民广场馆）"
        assert (await maps.resolve(query, "上海"))["id"] == "museum"
        places.append({**places[1], "id": "other", "location": "121.49,31.23"})
        with pytest.raises(MapError, match="多个同名"):
            await maps.resolve(query, "上海")


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


@pytest.mark.parametrize(
    ("failure_type", "message", "attempts"),
    [
        (httpx2.ConnectError, "无法建立连接", 2),
        (httpx2.ReadTimeout, "请求超时", 2),
        (None, "响应不是有效 JSON", 1),
    ],
)
async def test_network_and_json_errors_are_distinct(failure_type, message, attempts):
    key = "private-test-key"

    def respond(request):
        if failure_type:
            raise failure_type(str(request.url), request=request)
        return httpx2.Response(200, text=key)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        maps = MapClient(client, key)
        with pytest.raises(MapError, match=message) as failure:
            await maps.search("景点", "上海")
        assert "/v3/place/text" in str(failure.value)
        assert key not in str(failure.value)
        assert maps.calls == attempts
