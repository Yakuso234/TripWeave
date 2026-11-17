"""Production graph acceptance with real SDK and synthetic supplier HTTP only."""

import json
from copy import deepcopy

import httpx2
import pytest
from draft_helpers import packet, reply, route_queries
from test_planning_research import fact, sources
from test_planning_workflow import gap, itinerary, map_response, requirements, tool_names
from test_real_planner import configured

from tripweave.real_planner import run_plan


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
