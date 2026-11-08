"""Planner failure and scheduling boundaries, using simulated HTTP only."""

import json

import httpx2
import pytest
from test_real_planner import configured, handler, poi

import tripweave.real_planner as planner
from tripweave.domain import Requirements, Stop
from tripweave.maps import MapClient, MapError


@pytest.mark.asyncio
async def test_overnight_location_must_match_next_morning_origin():
    req = Requirements(date="2026-11-07", days=2)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        maps = MapClient(client, "test-map-key")
        data = await planner.prepare(req, maps, [])
        plan = await planner.schedule(req, data, maps)
        # Checking in earlier does not account for a later outing's return leg.
        outing = Stop(
            day=1,
            place_id="p7",
            start_time="20:00",
            end_time="20:20",
            kind="meal",
            reason="test evening outing",
        )
        plan.stops = (
            [s for s in plan.stops if s.day == 1] + [outing] + [s for s in plan.stops if s.day == 2]
        )
        issues = await planner.validate(req, plan, data, maps)
        assert any(i["code"] == "hotel_return" for i in issues)


@pytest.mark.asyncio
async def test_morning_appointment_has_lunch_and_final_day_has_no_extra_night():
    req = Requirements(date="2026-11-07", days=2, booking_start="10:30", booking_end="11:30")
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
    assert validations == [False, False]
