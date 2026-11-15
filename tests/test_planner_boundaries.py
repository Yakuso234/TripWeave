"""Planner failure and scheduling boundaries, using simulated HTTP only."""

import json

import httpx2
import pytest
from pydantic import ValidationError
from test_real_planner import configured, handler, poi

import tripweave.real_planner as planner
from tripweave.domain import Itinerary, Requirements, Stop
from tripweave.maps import MapClient, MapError


@pytest.mark.parametrize(
    ("origin", "place", "expected"),
    [
        ({"citycode": "", "city": "上海市"}, {"citycode": "", "city": "上海市"}, True),
        ({"citycode": "021", "city": "上海市"}, {"citycode": "", "city": "上海市"}, True),
        ({"citycode": "021", "city": "上海市"}, {"citycode": "010", "city": "上海市"}, False),
        ({"citycode": "", "city": "上海市"}, {"citycode": "", "city": "杭州市"}, False),
        ({"citycode": "", "city": ""}, {"citycode": "", "city": ""}, False),
        ({"citycode": " ", "city": " "}, {"citycode": " ", "city": " "}, False),
    ],
)
def test_city_identity_uses_provider_fields_without_guessing(origin, place, expected):
    assert planner.same_city(origin, place) is expected


async def test_collaborative_basic_pois_with_cityname_but_no_citycode_can_be_required():
    def provider(request):
        response = handler(request)
        if request.url.path.endswith("/place/text"):
            payload = response.json()
            for place in payload["pois"]:
                place["cityname"] = "上海市"
            return httpx2.Response(200, json=payload)
        return response

    req = Requirements(date="2026-11-21", selected_attractions=[{"id": "p7", "name": "接口景点7"}])
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(provider)) as client:
        maps = MapClient(client, "test-map-key")
        data = await planner.prepare(req, maps, [])
        assert [p["id"] for p in data["must_visit"]] == ["p7"]
        assert data["origin"]["citycode"] == ""
        assert maps.calls == 3  # No extra detail lookup to recover a missing city code.


async def test_collaborative_selection_is_resolved_by_id_and_required():
    def provider(request):
        response = handler(request)
        if request.url.path.endswith("/place/text"):
            payload = response.json()
            for place in payload["pois"]:
                place["citycode"] = "021"
            return httpx2.Response(200, json=payload)
        return response

    req = Requirements(
        date="2026-11-07",
        must_visit=["接口景点6"],
        selected_attractions=[{"id": "p7", "name": "接口景点7"}],
    )
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(provider)) as client:
        maps = MapClient(client, "test-map-key")
        data = await planner.prepare(req, maps, [])
        assert [place["id"] for place in data["must_visit"]] == ["p6", "p7"]
        stop = Stop(day=1, place_id="p0", start_time="10:00", end_time="11:00", reason="游览")
        issues = await planner.validate(req, Itinerary(summary="测试", stops=[stop]), data, maps)
        assert [issue["code"] for issue in issues] == ["must_visit", "must_visit"]


def test_model_stop_requires_explicit_day():
    stop = {
        "place_id": "p7",
        "start_time": "10:00",
        "end_time": "11:00",
        "reason": "test outing",
    }
    with pytest.raises(ValidationError, match="day"):
        Stop.model_validate(stop)
    assert Stop.model_validate({**stop, "day": 2}).day == 2


@pytest.mark.parametrize("fixed_booking", [False, True])
async def test_duplicate_visit_identifies_edit_day_and_preserves_fixed_booking(fixed_booking):
    req = Requirements(
        date="2026-11-07",
        days=2,
        booking_query="上海博物馆" if fixed_booking else "",
        booking_day=2,
    )
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        maps = MapClient(client, "test-map-key")
        data = await planner.prepare(req, maps, [])
        place_id = "b" if fixed_booking else "p0"
        plan = Itinerary(
            summary="测试",
            hotel_id="h",
            stops=[
                Stop(day=1, place_id=place_id, start_time="10:00", end_time="11:00", reason="游览"),
                Stop(
                    day=1,
                    place_id="h",
                    kind="hotel",
                    start_time="18:00",
                    end_time="18:15",
                    reason="回酒店",
                ),
                Stop(
                    day=2,
                    place_id=place_id,
                    kind="booking" if fixed_booking else "activity",
                    start_time=req.booking_start,
                    end_time=req.booking_end,
                    reason="游览",
                ),
            ],
        )
        issues = await planner.validate(req, plan, data, maps)
    assert [issue["code"] for issue in issues] == ["duplicate"]
    issue = issues[0]
    assert issue["place_id"] == place_id
    if fixed_booking:
        assert issue["day"] == 1 and issue["keep_day"] == 2
        assert issue["stop_number"] == issue["keep_stop_number"] == 1
        assert issue["keep_kind"] == "booking"
    else:
        assert issue["related_days"] == [1, 2]
        assert [(item["day"], item["stop_number"]) for item in issue["occurrences"]] == [
            (1, 1),
            (2, 1),
        ]
        assert "keep_day" not in issue and "day" not in issue


@pytest.mark.asyncio
@pytest.mark.parametrize("meal_place_id", ["p7", "h"])
async def test_overnight_day_rejects_a_meal_as_final_stop(meal_place_id):
    req = Requirements(date="2026-11-07", days=2)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        maps = MapClient(client, "test-map-key")
        data = await planner.prepare(req, maps, [])
        assert data["booking"] is None
        plan = await planner.schedule(req, data, maps)
        # Checking in earlier does not account for a later outing's return leg.
        outing = Stop(
            day=1,
            place_id=meal_place_id,
            start_time="20:00",
            end_time="20:20",
            kind="meal",
            reason="test evening outing",
        )
        plan.stops = (
            [s for s in plan.stops if s.day == 1] + [outing] + [s for s in plan.stops if s.day == 2]
        )
        issues = await planner.validate(req, plan, data, maps)
        assert any(i["code"] == "hotel_return" and i["day"] == 1 for i in issues)


@pytest.mark.asyncio
async def test_morning_appointment_has_lunch_and_final_day_has_no_extra_night():
    req = Requirements(
        date="2026-11-07",
        days=2,
        booking_query="上海博物馆",
        booking_start="10:30",
        booking_end="11:30",
    )
    result = await planner.run_plan(
        req, "basic", configured(), transport=httpx2.MockTransport(handler)
    )
    assert result["status"] == "ready", result["issues"]
    stops = result["itinerary"]["stops"]
    assert any(s["day"] == 1 and s["title"].startswith("午餐") for s in stops)
    assert not any(s["day"] == 2 and s["kind"] == "hotel" for s in stops)
    for day in (1, 2):
        assert [r["stop_index"] for r in result["routes"] if r["day"] == day] == list(
            range(sum(s["day"] == day for s in stops))
        )


@pytest.mark.asyncio
async def test_lunch_route_cannot_push_meal_past_daily_end():
    def long_restaurant_route(request):
        # Use a distinct coordinate so only travel to the restaurant is slow.
        if request.url.path.endswith("/place/around"):
            return httpx2.Response(
                200, json={"status": "1", "pois": [poi("r", "test restaurant", "餐饮服务", x=100)]}
            )
        if (
            "/direction/" in request.url.path
            and request.url.params.get("destination") == "121.57,31.23"
        ):
            return httpx2.Response(
                200,
                json={
                    "status": "1",
                    "route": {"paths": [{"duration": "3600", "distance": "5000"}]},
                },
            )
        return handler(request)

    req = Requirements(date="2026-11-07", end_time="13:00", booking_query="", transport="walking")
    result = await planner.run_plan(
        req, "basic", configured(), transport=httpx2.MockTransport(long_restaurant_route)
    )
    assert result["status"] == "ready", result["issues"]
    assert all(s["end_time"] <= "13:00" for s in result["itinerary"]["stops"])
    assert not any(s["kind"] == "meal" for s in result["itinerary"]["stops"])


@pytest.mark.asyncio
async def test_deepseek_connection_failure_is_a_structured_failure():
    def disconnected(request):
        if request.url.host == "api.deepseek.com":
            raise httpx2.ConnectError("simulated connection failure", request=request)
        return handler(request)

    result = await planner.run_plan(
        Requirements(date="2026-11-07"),
        "deepseek",
        configured(),
        transport=httpx2.MockTransport(disconnected),
    )
    assert result["status"] == "conflict" and result["itinerary"] is None
    assert "ModelConnectionError" in result["error"]
    assert "test-key-not-real" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("hotel_citycode", ["010", ""])
async def test_manual_hotel_from_another_or_unknown_city_is_rejected(hotel_citycode):
    def with_detail(request):
        if request.url.path.endswith("/place/detail"):
            hotel = {
                **poi("old-hotel", "previous hotel", "住宿服务;宾馆酒店"),
                "citycode": hotel_citycode,
            }
            return httpx2.Response(200, json={"status": "1", "pois": [hotel]})
        response = handler(request)
        body = response.json()
        for place in body.get("pois", []):
            place["citycode"] = "021"
        return httpx2.Response(200, json=body)

    req = Requirements(date="2026-11-07", days=2, hotel_id="old-hotel")
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(with_detail)) as client:
        with pytest.raises(MapError, match="城市"):
            await planner.prepare(req, MapClient(client, "test-map-key"), [])


@pytest.mark.asyncio
async def test_map_client_remains_open_during_final_model_validation(monkeypatch):
    req = Requirements(date="2026-11-07")
    seed = await planner.run_plan(
        req, "basic", configured(), transport=httpx2.MockTransport(handler)
    )
    output = seed["itinerary"]
    # Use only preloaded places so the fake model can submit in one request.
    for stop in output["stops"]:
        if stop["kind"] == "meal":
            stop["place_id"] = "o"
    original_validate = planner.validate
    validations = []

    async def check_client(req, output, data, maps):
        validations.append(maps.client.is_closed)
        return await original_validate(req, output, data, maps)

    monkeypatch.setattr(planner, "validate", check_client)

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return handler(request)
        body = json.loads(request.content)
        output_name = next(
            t["function"]["name"]
            for t in body["tools"]
            if t["function"]["name"] not in ("lookup_places", "route", "nearby_restaurants")
        )
        return httpx2.Response(
            200,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-output",
                                    "type": "function",
                                    "function": {
                                        "name": output_name,
                                        "arguments": json.dumps(output),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    result = await planner.run_plan(
        req, "deepseek", configured(), transport=httpx2.MockTransport(provider)
    )
    assert result["status"] == "ready", result["error"]
    assert validations == [False]
