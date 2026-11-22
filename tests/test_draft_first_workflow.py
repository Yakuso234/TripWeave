"""Production graph acceptance with real SDK and synthetic supplier HTTP only."""

import json
from copy import deepcopy

import httpx2
import pytest
from draft_helpers import packet, reply, route_queries
from test_planning_research import fact, sources
from test_planning_workflow import gap, itinerary, map_response, requirements, tool_names
from test_real_planner import configured, poi

from tripweave.real_planner import run_plan


async def test_automatic_hotel_comparisons_keep_verified_routes_across_batches():
    draft = itinerary(days=2)
    draft["hotel_id"] = "h0"
    draft["stops"].insert(
        1,
        {
            "day": 1,
            "place_id": "h0",
            "kind": "hotel",
            "title": "",
            "start_time": "17:00",
            "end_time": "18:00",
            "reason": "过夜当天返回酒店",
        },
    )
    alternative = deepcopy(draft)
    alternative["hotel_id"] = alternative["stops"][1]["place_id"] = "h2"
    alternative["summary"] = "合成行程，比较后使用备选住宿。"
    planner_calls, map_routes = [], []
    hotel_positions = {
        f"{121.47 + x / 1000},31.23": duration for x, duration in [(10, 12), (11, 16), (12, 5)]
    }

    def comparison(hotel_id):
        return [
            {
                "kind": "route",
                "origin_id": origin_id,
                "destination_id": destination_id,
                "transport": "mixed",
                "reason": "比较备选酒店跨两天的衔接",
            }
            for origin_id, destination_id in (("p0", hotel_id), (hotel_id, "p1"))
        ]

    def provider(request):
        path = request.url.path
        if request.url.host != "api.deepseek.com":
            if request.url.params.get("types") == "100000":
                return httpx2.Response(
                    200,
                    json={
                        "status": "1",
                        "pois": [
                            {
                                **poi(f"h{i}", f"酒店{i}", "住宿服务;宾馆酒店", x=10 + i),
                                "citycode": "021",
                                "cityname": "上海市",
                            }
                            for i in range(3)
                        ],
                    },
                )
            if "/direction/" in path:
                a, b = request.url.params["origin"], request.url.params["destination"]
                map_routes.append((a, b))
                duration = hotel_positions.get(a, hotel_positions.get(b, 5))
                return httpx2.Response(
                    200,
                    json={
                        "status": "1",
                        "route": {"paths": [{"duration": str(duration * 60), "distance": "500"}]},
                    },
                )
            return map_response(request)
        body = json.loads(request.content)
        prompt = json.loads(body["messages"][-1]["content"])
        if tool_names(body) == {"review_report"}:
            assert prompt["draft"]["hotel_id"] == "h2"
            return reply("review_report", {"summary": "合成校验通过。", "suggestions": []})
        planner_calls.append(prompt)
        step = len(planner_calls)
        if step == 1:
            return reply("draft_proposal", packet(draft))
        routes = {
            (route["origin_id"], route["destination_id"]): route["duration_minutes"]
            for route in prompt["hotel_route_evidence"]
        }
        assert routes["p0", "h0"] == routes["h0", "p1"] == 12
        if step == 2:
            return reply("draft_proposal", packet(draft, queries=comparison("h1")))
        assert routes["p0", "h1"] == routes["h1", "p1"] == 16
        if step == 3:
            return reply("draft_proposal", packet(draft, queries=comparison("h2")))
        assert step == 4 and routes["p0", "h2"] == routes["h2", "p1"] == 5
        return reply("draft_proposal", packet(alternative, queries=[]))

    result = await run_plan(
        requirements(days=2, nights=1, pace="relaxed"),
        "deepseek",
        configured(),
        transport=httpx2.MockTransport(provider),
    )
    assert result["outcome"] == "ready", result
    assert result["review"]["status"] == "approved" and result["itinerary"] == alternative
    assert len(map_routes) == len(set(map_routes)) == 7


async def test_first_model_response_is_saved_before_any_declared_map_query_and_has_no_tools():
    calls, saved, directions = [], [], []

    async def persist(snapshot):
        saved.append(deepcopy(snapshot))

    def provider(request):
        if request.url.host != "api.deepseek.com":
            if "/direction/" in request.url.path:
                assert saved and saved[0]["phase"] == "first_draft"
                directions.append(request)
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        if tool_names(body) == {"review_report"}:
            return reply("review_report", {"summary": "合成复核通过。", "suggestions": []})
        assert tool_names(body) == {"draft_proposal"}
        assert not any(m["role"] == "assistant" for m in body["messages"])
        return reply("draft_proposal", packet(itinerary(), queries=None if len(calls) == 1 else []))

    result = await run_plan(
        requirements(),
        "deepseek",
        configured(),
        transport=httpx2.MockTransport(provider),
        on_draft=persist,
    )
    assert result["outcome"] == "ready", result
    assert len(calls) == 3 and len(directions) == 1
    assert saved[0]["draft"] == itinerary()
    assert saved[0]["query_requests"][0]["origin_id"] == "o"
    assert result["query_log"][0]["model_round"] == 1
    assert result["query_log"][0]["query"]["destination_id"] == "p0"
    assert [c["phase"] for c in result["usage_observation"]["model_calls"]] == [
        "first_draft",
        "revision",
        "review",
    ]


async def test_budget_exhaustion_after_draft_retains_plan_and_unverified_status():
    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        phase = json.loads(body["messages"][-1]["content"])["task"]
        return reply(
            "draft_proposal", packet(itinerary(), queries=None if phase == "first_draft" else [])
        )

    result = await run_plan(
        requirements(),
        "deepseek",
        configured().model_copy(update={"max_requests": 2}),
        transport=httpx2.MockTransport(provider),
    )
    assert result["outcome"] == "incomplete" and result["itinerary"] is None
    assert result["draft"] == itinerary() and result["review"]["status"] != "approved"
    assert result["usage"]["requests"] == 2


async def test_cache_reuse_is_normal_and_does_not_trigger_false_no_progress():
    directions = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            if "/direction/" in request.url.path:
                directions.append(request)
            return map_response(request)
        body = json.loads(request.content)
        if "review_report" in tool_names(body):
            return reply("review_report", {"summary": "通过。", "suggestions": []})
        return reply("draft_proposal", packet(itinerary()))

    result = await run_plan(
        requirements(), "deepseek", configured(), transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "ready" and len(directions) == 1
    assert len(result["query_log"]) == 2 and result["query_log"][1]["cache_hit"]


@pytest.mark.parametrize("limit, outcome", [(2, "incomplete"), (6, "ready")])
async def test_same_poi_meal_anchor_needs_no_route_query_or_extra_model_round(limit, outcome):
    plan = itinerary()
    plan["stops"].append(
        {**plan["stops"][0], "start_time": "11:30", "end_time": "12:30", "kind": "meal"}
    )
    calls, directions = [], []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            if "/direction/" in request.url.path:
                directions.append(request)
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        if tool_names(body) == {"review_report"}:
            return reply("review_report", {"summary": "合成复核通过。", "suggestions": []})
        queries = [q for q in route_queries(plan) if q["origin_id"] != q["destination_id"]]
        return reply("draft_proposal", packet(plan, queries=queries if len(calls) == 1 else []))

    result = await run_plan(
        requirements(),
        "deepseek",
        configured().model_copy(update={"max_requests": limit}),
        transport=httpx2.MockTransport(provider),
    )
    assert result["outcome"] == outcome, result
    assert len(calls) == (2 if limit == 2 else 3) and len(directions) == 1
    assert result["daily_metrics"][0]["transport_minutes"] is not None
    assert not any(i["code"] == "route_queries_missing" for i in result["issues"])


async def test_identical_failed_batch_with_only_changed_reason_stops_before_global_budget():
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        calls.append(json.loads(request.content))
        q = {
            "kind": "route",
            "origin_id": "o",
            "destination_id": "unknown",
            "transport": "mixed",
            "reason": f"换一个查询理由{len(calls)}",
        }
        return reply("draft_proposal", packet(itinerary(), queries=[q]))

    result = await run_plan(
        requirements(), "deepseek", configured(), transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "incomplete" and result["draft"] == itinerary()
    assert "没有新进展" in result["error"] and len(calls) == 3
    assert all(r["status"] == "not_executed" for r in result["query_log"])


async def test_research_prefetch_extracts_in_one_model_round_and_keeps_early_draft():
    calls, snapshots, searches = [], [], []

    async def persist(snapshot):
        snapshots.append(deepcopy(snapshot))

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        if request.url.path.endswith("/tools/search"):
            assert snapshots and snapshots[0]["draft"] == itinerary()
            searches.append(request)
            return sources()
        body = json.loads(request.content)
        calls.append(body)
        names = tool_names(body)
        if names == {"research_report"}:
            task = json.loads(body["messages"][-1]["content"])["tasks"][0]
            assert task["sources"] and task["weekday"] == 6
            return reply("research_report", {"facts": [fact()]})
        if names == {"review_report"}:
            return reply("review_report", {"summary": "通过。", "suggestions": []})
        assert names == {"draft_proposal"}
        return reply(
            "draft_proposal",
            packet(
                itinerary(),
                queries=None if len(calls) == 1 else [],
                needs=gap()["needs"] if len(calls) == 1 else [],
            ),
        )

    result = await run_plan(
        requirements(),
        "deepseek",
        configured(),
        transport=httpx2.MockTransport(provider),
        on_draft=persist,
    )
    assert result["outcome"] == "ready", result
    assert len(calls) == 4 and len(searches) == 1
    assert [c["role"] for c in result["usage_observation"]["model_calls"]] == [
        "planner",
        "research",
        "planner",
        "reviewer",
    ]


async def test_changed_route_direction_cannot_satisfy_new_leg():
    plan = itinerary(days=2)
    plan["stops"][1]["day"] = 1
    plan["stops"][1].update(start_time="11:30", end_time="12:30")
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        if "review_report" in tool_names(body):
            return reply("review_report", {"summary": "通过。", "suggestions": []})
        queries = route_queries(plan)
        if len(calls) == 1:
            queries[1]["origin_id"], queries[1]["destination_id"] = "p1", "p0"
        elif len(calls) == 2:
            issue = json.loads(body["messages"][-1]["content"])["validation_issues"][0]
            assert issue["required_queries"][0]["origin_id"] == "p0"
            assert issue["required_queries"][0]["destination_id"] == "p1"
        else:
            queries = []
        return reply("draft_proposal", packet(plan, queries=queries))

    result = await run_plan(
        requirements(), "deepseek", configured(), transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "ready", result
    assert any(
        r["query"].get("origin_id") == "p0" and r["query"].get("destination_id") == "p1"
        for r in result["query_log"]
    )


async def test_outside_day_is_repaired_globally_before_any_declared_map_query():
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            if "/direction/" in request.url.path:
                assert len(calls) >= 2
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        if "review_report" in tool_names(body):
            return reply("review_report", {"summary": "通过。", "suggestions": []})
        plan = itinerary()
        if len(calls) == 1:
            plan["stops"][0]["day"] = 2
        if len(calls) == 2:
            assert "revision_days" not in json.loads(body["messages"][-1]["content"])
        return reply("draft_proposal", packet(plan, queries=None if len(calls) <= 2 else []))

    result = await run_plan(
        requirements(), "deepseek", configured(), transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "ready", result
    assert result["itinerary"]["stops"][0]["day"] == 1


async def test_unknown_research_identity_does_not_launch_search_or_hide_missing_fact():
    def provider(request):
        if request.url.host != "api.deepseek.com":
            assert "/direction/" not in request.url.path
            return map_response(request)
        assert request.url.path.endswith("/chat/completions")
        return reply("draft_proposal", packet(itinerary(), needs=gap(place_id="invented")["needs"]))

    result = await run_plan(
        requirements(), "deepseek", configured(), transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "incomplete" and result["draft"] == itinerary()
    assert result["issues"][0]["code"] == "invalid_research_scope"
    assert result["research_search_calls"] == 0


async def test_walking_evidence_covers_mixed_graph_validation_metrics_and_output_routes():
    calls, directions = [], []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            if "/direction/" in request.url.path:
                directions.append(request)
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        if tool_names(body) == {"review_report"}:
            return reply("review_report", {"summary": "合成通过", "suggestions": []})
        queries = route_queries(itinerary()) if len(calls) == 1 else []
        for q in queries:
            q["transport"] = "walking"
        return reply("draft_proposal", packet(itinerary(), queries=queries))

    result = await run_plan(
        requirements(), "deepseek", configured(), transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "ready", result
    assert len(calls) == 3 and len(directions) == 1
    assert result["daily_metrics"][0]["transport_minutes"] is not None
    assert len(result["routes"]) == 1


async def test_local_repair_allows_unaffected_day_annotation_change_but_rechecks_full_plan():
    calls = []
    plan = itinerary(days=2)
    broken = deepcopy(plan)
    broken["stops"][0]["start_time"] = "00:00"

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        if tool_names(body) == {"review_report"}:
            return reply("review_report", {"summary": "合成通过", "suggestions": []})
        context = json.loads(body["messages"][-1]["content"])
        current = deepcopy(broken)
        if any(i["code"] == "connection" for i in context.get("validation_issues", [])):
            assert context["revision_days"] == [1]
            current = deepcopy(plan)
            current["stops"][1]["reason"] = "说明更新，实际地点和时间未变"
        return reply("draft_proposal", packet(current, queries=route_queries(current)))

    result = await run_plan(
        requirements(days=2, strict_preferences=["start_time"]),
        "deepseek",
        configured(),
        transport=httpx2.MockTransport(provider),
    )
    assert result["outcome"] == "ready", result
    assert result["itinerary"]["stops"][1]["reason"] == "说明更新，实际地点和时间未变"
