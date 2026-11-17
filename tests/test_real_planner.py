"""Network contract tests use simulated HTTP responses, never real provider results."""

import json
from copy import deepcopy

import httpx2
import pytest
from draft_helpers import packet, reply, route_queries
from review_mock import with_approved_review

from tripweave.domain import Requirements
from tripweave.maps import MapError
from tripweave.real_planner import run_plan
from tripweave.settings import Settings


def poi(pid, name, kind="风景名胜", x=0):
    return {
        "id": pid,
        "name": name,
        "location": f"{121.47 + x / 1000},31.23",
        "address": "测试地址",
        "type": kind,
    }


def handler(request):
    query = request.url.params
    path = request.url.path
    if path.endswith("/place/text"):
        if query.get("types") == "100000":
            places = [poi("h", "接口返回的酒店", "住宿服务;宾馆酒店")]
        elif query.get("keywords") == "人民广场":
            places = [poi("o", "人民广场")]
        elif query.get("keywords") == "上海博物馆":
            places = [poi("b", "上海博物馆")]
        else:
            places = [poi("p" + str(i), "接口景点" + str(i), x=i + 1) for i in range(8)]
        return httpx2.Response(200, json={"status": "1", "pois": places})
    if path.endswith("/place/around"):
        return httpx2.Response(
            200, json={"status": "1", "pois": [poi("r", "接口餐厅", "餐饮服务")]}
        )
    if "/direction/" in path:
        return httpx2.Response(
            200, json={"status": "1", "route": {"paths": [{"duration": "300", "distance": "500"}]}}
        )
    raise AssertionError("unexpected request " + path)


def configured():
    # Keep constrained regression fixtures at six rounds; /api/config tests the new default.
    return Settings(
        _env_file=None,
        api_key="test-key-not-real",
        amap_api_key="test-map-key",
        run_timeout=30,
        max_requests=6,
    )


@pytest.mark.parametrize("pois", [[], [poi("square", "上海人民广场")]])
async def test_unresolved_booking_is_returned_without_calling_deepseek(pois):
    def provider(request):
        assert request.url.path.endswith("/place/text")
        if request.url.params.get("keywords") == "上海博物馆（人民广场馆）":
            return httpx2.Response(200, json={"status": "1", "pois": pois})
        assert request.url.params.get("keywords") == "人民广场"
        return handler(request)

    result = await run_plan(
        Requirements(date="2026-11-07", booking_query="上海博物馆（人民广场馆）"),
        "deepseek",
        configured(),
        transport=httpx2.MockTransport(with_approved_review(provider)),
    )
    assert result["status"] == "conflict" and result["itinerary"] is None
    assert "高德未找到匹配的预约地点「上海博物馆（人民广场馆）」" in result["error"]
    assert result["usage"] is None and result["map_calls"] == 2


@pytest.mark.asyncio
async def test_basic_plans_real_adapter_results_and_reacts_to_input():
    transport = httpx2.MockTransport(handler)
    req = Requirements(date="2026-11-07", days=2, booking_query="上海博物馆")
    first = await run_plan(req, "basic", configured(), transport=transport)
    assert first["status"] == "ready", first
    assert first["hotel"]["name"] == "接口返回的酒店"
    assert first["hotel"]["nightly_rate"] is None
    assert first["costs"]["total"] is None
    assert {s["day"] for s in first["itinerary"]["stops"]} == {1, 2}
    changed = await run_plan(
        req.model_copy(update={"start_time": "10:00"}), "basic", configured(), transport=transport
    )
    assert (
        first["itinerary"]["stops"][0]["start_time"]
        != changed["itinerary"]["stops"][0]["start_time"]
    )
    assert any(
        s["kind"] == "booking" and s["start_time"] == "14:00" for s in changed["itinerary"]["stops"]
    )
    conflict = await run_plan(
        req.model_copy(update={"start_time": "14:30"}), "basic", configured(), transport=transport
    )
    assert conflict["status"] == "conflict"
    assert any(i["code"] == "connection" for i in conflict["issues"])


@pytest.mark.asyncio
async def test_permissions_are_explicit_and_never_leak_key():
    def denied(request):
        return httpx2.Response(200, json={"status": "0", "infocode": "10001"})

    result = await run_plan(
        Requirements(date="2026-11-07"),
        "basic",
        configured(),
        transport=httpx2.MockTransport(denied),
    )
    assert result["status"] == "conflict" and result["itinerary"] is None
    assert "10001" in result["error"] and "test-map-key" not in json.dumps(result)
    with pytest.raises(MapError):
        await run_plan(
            Requirements(date="2026-11-07"), "basic", Settings(_env_file=None, amap_api_key=None)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("repair", "request_limit", "outside_scope", "booking_case"),
    [
        (True, 6, False, None),
        (True, 6, True, None),
        (False, 6, False, None),
        (False, 3, False, None),
        (True, 6, False, "missing"),
        (True, 6, False, "wrong_day"),
    ],
)
async def test_deepseek_contract_feedback_and_usage(
    repair, request_limit, outside_scope, booking_case
):
    req = Requirements(
        date="2026-11-07", days=2, booking_query="上海博物馆" if booking_case else ""
    )
    seed = await run_plan(req, "basic", configured(), transport=httpx2.MockTransport(handler))
    assert seed["status"] == "ready", seed["issues"]
    original = deepcopy(seed["itinerary"])
    # Restaurants are not known before the first draft. Use an explicit meal
    # area anchor instead of carrying an ID discovered by the basic fixture.
    for stop in original["stops"]:
        if stop["kind"] == "meal":
            stop.update(place_id="o", title="人民广场附近自选正餐", reason="具体餐厅与供应待核实。")
    broken = deepcopy(original)
    if booking_case:
        broken["stops"] = [stop for stop in broken["stops"] if stop["kind"] != "booking"]
        if booking_case == "wrong_day":
            wrong_stop = next(
                stop for stop in broken["stops"] if stop["day"] == 2 and stop["kind"] == "activity"
            )
            wrong_stop.update(kind="booking", place_id="b")
    else:
        broken["stops"][0]["start_time"] = "00:00"
    calls, business_feedback = [], []
    revision_days = [1, 2] if booking_case == "wrong_day" else [1]

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return handler(request)
        body = json.loads(request.content)
        calls.append(body)
        assert {tool["function"]["name"] for tool in body["tools"]} == {"draft_proposal"}
        prompt = json.loads(body["messages"][-1]["content"])
        issues = prompt.get("validation_issues", [])
        codes = {issue["code"] for issue in issues}
        target_error = "booking" if booking_case else "connection"
        if target_error in codes:
            business_feedback.append(prompt)
            corrected = deepcopy(original) if repair else deepcopy(broken)
            if outside_scope:
                # Changing an unaffected day's explanation is still outside the
                # allowed repair scope; the engine must retain the old draft.
                next(stop for stop in corrected["stops"] if stop["day"] == 2)["reason"] = (
                    "不应改动的第二天"
                )
            return reply(
                "draft_proposal", packet(corrected, queries=route_queries(corrected)), len(calls)
            )
        if prompt["task"] == "first_draft":
            return reply("draft_proposal", packet(original), len(calls))
        return reply("draft_proposal", packet(broken, queries=route_queries(broken)), len(calls))

    settings = configured().model_copy(update={"max_requests": request_limit})
    output = await run_plan(
        req, "deepseek", settings, transport=httpx2.MockTransport(with_approved_review(provider))
    )
    assert calls[0]["model"] == "deepseek-flash"
    # The explicit review fixture has its own real SDK response and usage.
    approved = repair and not outside_scope
    assert output["usage"]["requests"] == len(calls) + int(approved)
    assert output["usage"]["input_tokens"] == 42 * output["usage"]["requests"]
    assert output["usage"]["requests"] <= request_limit
    assert business_feedback
    revision = business_feedback[0]
    assert revision["revision_days"] == revision_days
    assert revision["draft"] == broken
    feedback = revision["validation_issues"]
    if booking_case:
        booking = next(issue for issue in feedback if issue["code"] == "booking")
        assert booking["day"] == 1 and booking["place_id"] == "b"
        assert booking["expected_kind"] == "booking"
        assert booking["expected_start"] == "14:00" and booking["expected_end"] == "15:00"
        assert [stop["day"] for stop in booking["occurrences"]] == revision_days[1:]
    else:
        connection = next(issue for issue in feedback if issue["code"] == "connection")
        assert connection["day"] == connection["stop_number"] == 1
        assert connection["origin_id"] == "o"
        assert connection["destination_id"] == original["stops"][0]["place_id"]
        assert connection["departure_time"] == "09:00" and connection["travel_minutes"] == 5
        assert connection["scheduled_start"] == "00:00"
        assert (
            connection["earliest_arrival_day"] == 1
            and connection["earliest_arrival_time"] == "09:05"
        )
    if approved:
        assert output["status"] == "ready", (output["error"], output["trace"])
        assert output["itinerary"] == original
    else:
        assert output["status"] == "conflict" and output["itinerary"] is None
        assert output["draft"] == broken
        if outside_scope:
            assert "局部修订范围之外" in output["error"]
        else:
            assert any(issue["code"] == "connection" for issue in output["issues"])
