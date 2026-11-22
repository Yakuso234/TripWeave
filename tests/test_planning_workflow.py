"""Actual SDK/LangGraph orchestration with deterministic supplier HTTP responses."""

import json
from copy import deepcopy

import httpx2
import pytest
from draft_helpers import packet, phase_packet, route_queries
from fastapi.testclient import TestClient
from review_mock import with_approved_review
from test_planning_research import fact, sources
from test_real_planner import configured, handler
from test_research_planner_context import completion

import tripweave.app as application
from tripweave.domain import Requirements
from tripweave.real_planner import run_plan

PRIVATE_NOTE = "PRIVATE-NOTE-MUST-NOT-REACH-RESEARCH"


def requirements(**changes):
    return Requirements.model_validate({"date": "2026-11-21", "nights": 0, **changes})


def map_response(request):
    response = handler(request)
    data = response.json()
    for place in data.get("pois", []):
        place.update(citycode="021", cityname="上海市")
    return httpx2.Response(200, json=data)


def itinerary(days=1):
    return {
        "summary": "合成规划样本，仅用于HTTP模拟验证。",
        "hotel_id": None,
        "stops": [
            {
                "day": day,
                "place_id": f"p{day - 1}",
                "start_time": "10:00",
                "end_time": "11:00",
                "kind": "activity",
                "reason": f"第{day}天原安排，必须保留未受影响日期。",
                "title": "",
            }
            for day in range(1, days + 1)
        ],
    }


def gap(*, field="opening_hours", day=1, place_id="p0"):
    return {
        "needs": [
            {
                "place_id": place_id,
                "day": day,
                "fields": [field],
                "question": "该地点在此日期的具体入场信息是什么？",
            }
        ],
        "reason": "该事实会影响用户必去地点的可安排时段。",
    }


def tool_names(body):
    return {tool["function"]["name"] for tool in body["tools"]}


async def run(req, provider, *, limit=6):
    settings = configured().model_copy(update={"max_requests": limit})
    return await run_plan(
        req, "deepseek", settings, transport=httpx2.MockTransport(with_approved_review(provider))
    )


async def test_declared_booking_outside_daily_bounds_stops_before_any_supplier_call():
    def forbidden(request):
        raise AssertionError("a proved user requirement conflict must not call suppliers")

    result = await run(
        requirements(
            booking_query="上海博物馆",
            booking_start="21:00",
            booking_end="22:00",
            strict_preferences=["end_time"],
        ),
        forbidden,
    )
    assert result["status"] == "conflict"
    assert result["outcome"] == "needs_user_decision"
    assert result["map_calls"] == 0 and result["usage"] is None
    assert result["data_mode"] == "本地硬要求预检 · 未调用外部服务"
    assert result["itinerary"] is None and result["draft"] is None
    assert result["issues"][0]["category"] == "hard_conflict"
    assert result["issues"][0]["constraint_ids"] == ["booking", "daily_window"]


async def test_query_identity_overlap_is_proved_after_map_resolution_without_model_calls():
    map_calls = []

    def provider(request):
        assert request.url.host == "restapi.amap.com"
        map_calls.append(request.url)
        return map_response(request)

    constraints = [
        {
            "constraint_id": f"fixed-{i}",
            "place_query": f"接口景点{i}",
            "windows": [{"day": 1, "start_time": "14:00", "end_time": "15:00"}],
            "fixed": True,
        }
        for i in range(2)
    ]
    result = await run(requirements(visit_constraints=constraints), provider)
    assert result["outcome"] == "needs_user_decision" and result["usage"] is None
    assert result["map_calls"] == len(map_calls) > 0
    issue = next(i for i in result["issues"] if i["code"] == "fixed_window_overlap")
    assert issue["constraint_ids"] == ["fixed-0", "fixed-1"]
    assert issue["place_ids"] == ["p0", "p1"]


async def test_fixed_appointments_with_insufficient_travel_gap_stop_before_model():
    requests = []

    def provider(request):
        assert request.url.host == "restapi.amap.com"
        requests.append(request.url)
        return map_response(request)

    constraints = [
        {
            "constraint_id": "first",
            "place_query": "接口景点0",
            "windows": [{"day": 1, "start_time": "14:00", "end_time": "15:00"}],
            "fixed": True,
        },
        {
            "constraint_id": "second",
            "place_query": "接口景点1",
            "windows": [{"day": 1, "start_time": "15:02", "end_time": "16:02"}],
            "fixed": True,
        },
    ]
    result = await run(requirements(visit_constraints=constraints), provider)
    assert result["outcome"] == "needs_user_decision", result
    assert result["usage"] is None and result["research_rounds"] == 0
    assert result["map_calls"] == len(requests) > 0
    issue = next(i for i in result["issues"] if i["code"] == "fixed_connection_unreachable")
    assert issue["category"] == "hard_conflict"
    assert issue["constraint_ids"] == ["first", "second"]
    assert issue["place_ids"] == ["p0", "p1"]
    assert issue["expected"] == {"earliest_departure": "15:00", "travel_minutes": 5}
    assert issue["actual"] == {"fixed_start": "15:02"}
    assert issue["source"] and issue["queried_at"]


async def test_known_draft_error_revises_only_affected_day_and_checks_whole_plan():
    original = itinerary(days=2)
    broken = deepcopy(original)
    broken["stops"][0]["start_time"] = "00:00"
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        assert tool_names(body) == {"draft_proposal"}
        return completion(
            "draft_proposal",
            phase_packet(
                json.loads(body["messages"][-1]["content"]),
                original if len(calls) >= 3 else broken,
                queries=None if len(calls) == 1 else [],
            ),
            len(calls),
        )

    result = await run(requirements(days=2, max_activities=1), provider)
    assert result["outcome"] == "ready", result
    assert result["usage"]["requests"] == len(calls) + 1 == 4
    revision = json.loads(calls[2]["messages"][-1]["content"])
    assert revision["revision_days"] == [1]
    assert revision["draft"]["stops"][1] == original["stops"][1]
    assert revision["validation_issues"][0]["code"] == "connection"
    assert result["itinerary"] == original
    assert result["research_rounds"] == result["research_search_calls"] == 0


async def test_pacing_violation_returns_actual_limit_and_segments_for_local_revision():
    original = itinerary(days=2)
    broken = deepcopy(original)
    broken["stops"][0]["end_time"] = "12:00"
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        return completion(
            "draft_proposal",
            phase_packet(
                json.loads(body["messages"][-1]["content"]),
                original if len(calls) >= 3 else broken,
                queries=None if len(calls) == 1 else [],
            ),
            len(calls),
        )

    result = await run(
        requirements(
            days=2, max_sightseeing_minutes=90, strict_preferences=["max_sightseeing_minutes"]
        ),
        provider,
    )
    assert result["outcome"] == "ready", result
    revision = json.loads(calls[2]["messages"][-1]["content"])
    assert revision["revision_days"] == [1]
    assert revision["draft"]["stops"][1] == original["stops"][1]
    issue = revision["validation_issues"][0]
    assert (issue["code"], issue["actual"], issue["limit"]) == ("max_sightseeing_minutes", 120, 90)
    assert issue["occurrences"][0]["stop_number"] == 1
    assert issue["occurrences"][0]["end_time"] == "12:00"
    assert result["itinerary"] == original
    assert [row["sightseeing_minutes"] for row in result["daily_metrics"]] == [60, 60]


async def test_planner_research_return_uses_independent_context_and_shared_usage():
    events, models, searches = [], [], []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        if request.url.path.endswith("/tools/search"):
            events.append("search_http")
            searches.append(body)
            return sources()
        models.append(body)
        number = len(models)
        if number == 1:
            events.append("planner_gap")
            return completion("draft_proposal", packet(itinerary(), needs=gap()["needs"]), number)
        if "research_report" in tool_names(body):
            events.append("research_report")
            assert "search_place_information" not in tool_names(body)
            return completion("research_report", {"facts": [fact()]}, number)
        events.append("planner_resume")
        return completion("draft_proposal", packet(itinerary(), queries=[]), number)

    result = await run(requirements(notes=PRIVATE_NOTE), provider)
    assert result["outcome"] == "ready", result
    assert result["usage"] == {"requests": 4, "input_tokens": 42 * 4, "output_tokens": 20 * 4}
    assert result["research_rounds"] == result["research_search_calls"] == len(searches) == 1
    assert events == ["planner_gap", "search_http", "research_report", "planner_resume"]
    research_messages = json.dumps(models[1]["messages"])
    assert PRIVATE_NOTE not in research_messages and "draft_stops" not in research_messages
    research_prompt = json.loads(models[1]["messages"][-1]["content"])
    assert set(research_prompt) == {
        "city",
        "tasks",
        "prior_facts",
        "prior_diagnostics",
        "followup_instruction",
    }
    assert research_prompt["prior_facts"] == research_prompt["prior_diagnostics"] == []
    assert research_prompt["tasks"][0]["date"] == "2026-11-21"
    assert research_prompt["tasks"][0]["sources"]
    assert PRIVATE_NOTE not in json.dumps(searches)
    resumed = json.loads(models[2]["messages"][-1]["content"])
    assert resumed["draft"] == itinerary()
    assert resumed["planning_facts"][0]["evidence_status"] == "web_reported"
    assert resumed["planning_facts"][0]["sources"][0]["url"] == "https://museum.example/visit"
    assert result["research_facts"] == resumed["planning_facts"]


async def test_fact_return_allows_one_full_revision_then_restores_local_repairs():
    original = itinerary(days=2)
    original["stops"][0]["reason"] = "PRIVATE-DRAFT-REASON-MUST-NOT-REACH-RESEARCH"
    first = deepcopy(original)
    first["stops"][0]["start_time"] = "00:00"
    after = deepcopy(original)
    after["stops"][1]["start_time"] = "00:00"
    calls = []
    planners = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        if request.url.path.endswith("/tools/search"):
            return sources()
        body = json.loads(request.content)
        calls.append(body)
        if "research_report" in tool_names(body):
            return completion("research_report", {"facts": [fact()]}, len(calls))
        planners.append(body)
        if len(planners) == 1:
            value = packet(first, needs=gap()["needs"])
        elif len(planners) == 2:
            value = packet(after, queries=[])
        else:
            value = phase_packet(json.loads(body["messages"][-1]["content"]), original, queries=[])
        return completion("draft_proposal", value, len(calls))

    result = await run(requirements(days=2, notes=PRIVATE_NOTE), provider)
    assert result["outcome"] == "ready", result
    assert result["usage"]["requests"] == 5
    assert result["itinerary"] == original
    research_input = json.dumps(calls[1]["messages"])
    assert "PRIVATE-DRAFT-REASON" not in research_input and PRIVATE_NOTE not in research_input
    after_facts = json.loads(planners[1]["messages"][-1]["content"])
    assert after_facts["draft"] == first and "revision_days" not in after_facts
    assert after_facts["planning_facts"][0]["field"] == "opening_hours"
    next_revision = json.loads(planners[2]["messages"][-1]["content"])
    assert next_revision["revision_days"] == [2]
    assert next_revision["draft"]["stops"][0] == after["stops"][0]
    assert result["research_rounds"] == result["research_search_calls"] == 1


@pytest.mark.parametrize("search_mode", ["unknown", "no_results", "service_error"])
async def test_unknown_returns_to_planner_once_but_service_failure_stops(search_mode):
    models, searches = [], []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        if request.url.path.endswith("/tools/search"):
            searches.append(body)
            if search_mode == "service_error":
                return httpx2.Response(403, json={"error": "fixture access denied"})
            return (
                sources()
                if search_mode == "unknown"
                else httpx2.Response(200, json={"results": []})
            )
        models.append(body)
        if "research_report" in tool_names(body):
            return completion(
                "research_report",
                {
                    "facts": [
                        {"place_id": "p0", "day": 1, "field": "opening_hours", "status": "unknown"}
                    ]
                },
                len(models),
            )
        return completion(
            "draft_proposal",
            packet(
                itinerary(),
                queries=None if len(models) == 1 else [],
                needs=gap()["needs"] if len(models) == 1 else [],
            ),
            len(models),
        )

    result = await run(requirements(), provider)
    assert result["outcome"] == (
        "service_error" if search_mode == "service_error" else "needs_information"
    ), result
    assert result["status"] == "conflict" and result["itinerary"] is None
    assert (
        result["usage"]["requests"]
        == len(models)
        == {"unknown": 3, "no_results": 4, "service_error": 2}[search_mode]
    )
    assert result["draft"] == itinerary()
    assert result["research_rounds"] == 1
    assert len(searches) == (2 if search_mode == "no_results" else 1)
    assert result["research_facts"][0]["status"] == "unknown"
    assert result["issues"][0]["missing_fields"] == ["opening_hours"]
    assert bool(result["error"]) == (search_mode == "service_error")


async def test_second_information_request_can_research_and_reach_approved_plan():
    calls, searches = [], []
    planners = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        if request.url.path.endswith("/tools/search"):
            searches.append(request)
            return sources()
        body = json.loads(request.content)
        calls.append(body)
        if "review_report" in tool_names(body):
            return completion("review_report", {"summary": "认可", "suggestions": []}, len(calls))
        if "research_report" in tool_names(body):
            task = json.loads(body["messages"][-1]["content"])["tasks"][0]
            answer = (
                fact()
                if task["fields"] == ["opening_hours"]
                else fact(field="last_admission", windows=[], last_admission="16:00")
            )
            if task["fields"] == ["last_admission"]:
                assert (
                    json.loads(body["messages"][-1]["content"])["prior_facts"][0]["field"]
                    == "opening_hours"
                )
            return completion("research_report", {"facts": [answer]}, len(calls))
        planners.append(body)
        needs = (
            gap(field="opening_hours" if len(planners) == 1 else "last_admission")["needs"]
            if len(planners) <= 2
            else []
        )
        return completion(
            "draft_proposal",
            packet(itinerary(), queries=None if len(planners) == 1 else [], needs=needs),
            len(calls),
        )

    result = await run(requirements(), provider)
    assert result["outcome"] == "ready", result
    assert result["research_rounds"] == len(searches) == 2
    assert result["usage"]["requests"] == 6 and len(calls) == 5  # wrapper supplies review
    assert {f["field"] for f in result["research_facts"]} == {"opening_hours", "last_admission"}
    assert result["issues"] == [] and result["review"]["status"] == "approved"


async def test_research_limit_includes_recovery_and_filters_mixed_requests():
    searches, planners, research_tasks = [], [], []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        if request.url.path.endswith("/tools/search"):
            searches.append(json.loads(request.content))
            return sources()
        body = json.loads(request.content)
        prompt = json.loads(body["messages"][-1]["content"])
        if "research_report" in tool_names(body):
            tasks = prompt.get("tasks") or prompt["recovery_tasks"]
            fields = tasks[0].get("fields") or [tasks[0]["field"]]
            research_tasks.append(fields)
            if fields == ["last_admission"]:
                answer = fact(field="last_admission", windows=[], last_admission="16:00")
            else:
                answer = {
                    "place_id": "p0",
                    "day": 1,
                    "field": "opening_hours",
                    "status": "unknown",
                    "unknown_reason": "field_not_stated",
                    "unknown_detail": "摘要未说明该场馆当天的开放时间。",
                }
            return completion("research_report", {"facts": [answer]}, 1)
        planners.append(prompt)
        needs = gap()["needs"]
        if len(planners) == 2:
            needs[0]["fields"].append("last_admission")
            needs[0]["question"] = "是否白天开放？另外几点停止入场？"
        elif len(planners) == 3:
            needs[0]["question"] = "换一个来源再核实白天能否参观。"
        return completion(
            "draft_proposal",
            packet(itinerary(), queries=None if len(planners) == 1 else [], needs=needs),
            1,
        )

    result = await run(requirements(), provider, limit=None)
    assert result["outcome"] == "needs_information", result
    assert result["research_rounds"] == 2 and len(searches) == len(planners) == 3
    assert research_tasks == [["opening_hours"], ["opening_hours"], ["last_admission"]]
    assert "开放" not in searches[-1]["query"]
    assert [
        event["fields"] for event in result["trace"] if event["event"] == "research_search_attempt"
    ] == research_tasks
    assert planners[1]["research_limits"][0]["search_attempts"] == 2
    assert "已查过2次" in planners[1]["research_limits"][0]["message"]
    assert planners[1]["planning_facts"][0]["status"] == "unknown"
    assert result["issues"][0]["code"] == "research_no_progress"
    assert result["draft"] == itinerary() and result["itinerary"] is None
    assert any(t["event"] == "research_no_progress" for t in result["trace"])


async def test_reservation_channel_does_not_resolve_requirement_or_availability():
    searches, planners = [], []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        if request.url.path.endswith("/tools/search"):
            searches.append(json.loads(request.content)["query"])
            return httpx2.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://example.com/booking",
                            "title": "参观预约",
                            "snippet": "请从官方小程序预约。",
                            "content": "请从官方小程序预约。",
                        }
                    ]
                },
            )
        body = json.loads(request.content)
        prompt = json.loads(body["messages"][-1]["content"])
        if "research_report" in tool_names(body):
            assert prompt["tasks"][0]["fields"] == ["reservation_channel"]
            return completion(
                "research_report",
                {
                    "facts": [
                        {
                            "place_id": "p0",
                            "day": 1,
                            "field": "reservation_channel",
                            "status": "found",
                            "source_ids": [1],
                            "value": "官方小程序",
                            "quote": "请从官方小程序预约。",
                        }
                    ]
                },
                1,
            )
        planners.append(prompt)
        needs = gap(field="reservation_channel")["needs"] if len(planners) == 1 else []
        if needs:
            needs[0]["fields"].append("ticket_availability")
        return completion(
            "draft_proposal",
            packet(itinerary(), queries=None if needs else [], needs=needs),
            1,
        )

    result = await run(requirements(), provider, limit=None)
    assert len(searches) == 1 and "余票" not in searches[0]
    assert [(fact["field"], fact["status"]) for fact in planners[1]["planning_facts"]] == [
        ("reservation_channel", "found"),
    ]
    assert result["research_limits"][0]["code"] == "live_availability_unverified"
    assert result["research_limits"][0]["search_attempts"] == 0
    assert result["outcome"] == "needs_information"
    assert result["issues"][0]["missing_fields"] == ["ticket_availability"]


async def test_shared_budget_exhaustion_retains_failed_draft_and_original_problem():
    broken = itinerary()
    broken["stops"][0]["start_time"] = "00:00"
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        return completion(
            "draft_proposal", packet(broken, queries=None if len(calls) == 1 else []), len(calls)
        )

    result = await run(requirements(), provider, limit=2)
    assert result["outcome"] == "incomplete", result
    assert result["usage"]["requests"] == len(calls) == 2
    assert result["draft"] == broken and result["itinerary"] is None
    assert result["issues"][0]["code"] == "connection"
    assert all(i["category"] != "hard_conflict" for i in result["issues"])
    assert result["error"]


async def test_research_and_planning_cannot_each_reset_the_request_budget():
    calls, searches = [], []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        if request.url.path.endswith("/tools/search"):
            searches.append(request)
            return sources()
        body = json.loads(request.content)
        calls.append(body)
        if "research_report" in tool_names(body):
            return completion("research_report", {"facts": [fact()]}, len(calls))
        return completion("draft_proposal", packet(itinerary(), needs=gap()["needs"]), len(calls))

    result = await run(requirements(), provider, limit=2)
    assert result["outcome"] == "incomplete", result
    assert result["usage"]["requests"] == len(calls) == 2
    assert result["research_rounds"] == len(searches) == 1
    assert result["research_facts"][0]["status"] == "found"
    assert result["draft"] == itinerary() and result["itinerary"] is None


async def test_move_to_unresearched_day_keeps_a_declared_critical_gap_open():
    calls, searches = [], []
    planners = []
    moved = itinerary(days=2)
    moved["stops"][0]["place_id"] = "p1"
    moved["stops"][1]["place_id"] = "p0"

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        if request.url.path.endswith("/tools/search"):
            searches.append(request)
            return sources()
        body = json.loads(request.content)
        calls.append(body)
        if "research_report" in tool_names(body):
            return completion("research_report", {"facts": [fact()]}, len(calls))
        planners.append(body)
        if len(planners) == 1:
            value = packet(itinerary(days=2), needs=gap()["needs"])
        else:
            value = packet(moved, queries=route_queries(moved) if len(planners) == 2 else [])
        return completion("draft_proposal", value, len(calls))

    result = await run(requirements(days=2), provider)
    assert result["outcome"] == "needs_information", result
    assert result["draft"] == moved and result["itinerary"] is None
    assert result["research_rounds"] == len(searches) == 1
    assert result["usage"]["requests"] == len(calls) == 3
    issue = next(i for i in result["issues"] if i["code"] == "critical_fact_date_missing")
    assert issue["day"] == 2 and issue["place_id"] == "p0"
    assert issue["missing_fields"] == ["opening_hours"]


async def test_distinct_user_fixed_appointments_can_repeat_one_place_across_days():
    constraints = [
        {
            "constraint_id": f"appointment-{day}",
            "place_query": "接口景点0",
            "windows": [{"day": day, "start_time": "10:00", "end_time": "11:00"}],
            "fixed": True,
        }
        for day in (1, 2)
    ]
    plan = itinerary(days=2)
    for stop in plan["stops"]:
        stop.update(kind="booking", place_id="p0")
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        return completion(
            "draft_proposal", packet(plan, queries=None if len(calls) == 1 else []), len(calls)
        )

    result = await run(requirements(days=2, visit_constraints=constraints), provider)
    assert result["outcome"] == "ready", result
    assert result["itinerary"] == plan
    assert result["issues"] == []


async def test_two_user_fixed_appointments_at_same_place_on_one_day_are_kept():
    constraints = [
        {
            "constraint_id": f"appointment-{index}",
            "place_query": "接口景点0",
            "windows": [{"day": 1, "start_time": start, "end_time": end}],
            "fixed": True,
        }
        for index, (start, end) in enumerate([("10:00", "11:00"), ("14:00", "15:00")])
    ]
    plan = itinerary()
    plan["stops"][0]["kind"] = "booking"
    plan["stops"].append({**plan["stops"][0], "start_time": "14:00", "end_time": "15:00"})
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        return completion(
            "draft_proposal", packet(plan, queries=None if len(calls) == 1 else []), len(calls)
        )

    result = await run(requirements(visit_constraints=constraints), provider)
    assert result["outcome"] == "ready", result
    assert result["itinerary"] == plan
    assert result["issues"] == []


async def test_explicit_visit_window_reaches_model_and_failed_stay_is_repaired():
    constraint = {
        "constraint_id": "user-window-a",
        "place_query": "接口景点0",
        "windows": [{"day": 1, "start_time": "11:00", "end_time": "12:00"}],
        "min_duration_minutes": 60,
    }
    original = itinerary()
    corrected = deepcopy(original)
    corrected["stops"][0].update(start_time="11:00", end_time="12:00")
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        return completion(
            "draft_proposal",
            phase_packet(
                json.loads(body["messages"][-1]["content"]),
                corrected if len(calls) >= 3 else original,
                queries=None if len(calls) == 1 else [],
            ),
            len(calls),
        )

    result = await run(requirements(visit_constraints=[constraint]), provider)
    assert result["outcome"] == "ready", result
    initial = json.loads(calls[0]["messages"][-1]["content"])
    assert initial["requirements"]["visit_constraints"][0]["constraint_id"] == "user-window-a"
    assert initial["queried_data"]["constrained_places"]["user-window-a"]["id"] == "p0"
    revision = json.loads(calls[2]["messages"][-1]["content"])
    issue = next(i for i in revision["validation_issues"] if i["code"] == "visit_constraint")
    assert issue["constraint_ids"] == ["user-window-a"] and issue["day"] == 1
    assert issue["actual"][0]["start_time"] == "10:00"
    assert result["itinerary"] == corrected


def test_unaccepted_workflow_result_and_draft_facts_survive_api_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("AMAP_API_KEY", "fixture-map-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-model-key")
    fixture = {
        "status": "conflict",
        "outcome": "needs_information",
        "itinerary": None,
        "draft": itinerary(),
        "research_facts": [fact() | {"evidence_status": "web_reported"}],
        "issues": [{"category": "missing_fact", "code": "missing", "message": "待补资料"}],
        "error": None,
    }

    async def simulated_run(*args, **kwargs):
        return deepcopy(fixture)

    monkeypatch.setattr(application, "run_plan", simulated_run)
    database = str(tmp_path / "workflow.sqlite3")
    with TestClient(application.create_app(database)) as client:
        trip = client.post("/api/trips", json={"date": "2026-11-21"}).json()
        base = f"/api/trips/{trip['id']}"
        response = client.post(base + "/plan", json={"expected_requirements_revision": 1})
        assert response.status_code == 200
        candidate = response.json()
        assert candidate["outcome"] == "needs_information"
        assert client.post(base + f"/candidates/{candidate['id']}/accept").status_code == 409
    with TestClient(application.create_app(database)) as restarted:
        restored = restarted.get(base + "/history").json()["candidates"][0]
        assert restored == candidate
        assert restored["draft"] == fixture["draft"]
        assert restored["research_facts"] == fixture["research_facts"]
        assert restarted.get(base).json()["accepted_version"] == 0
        assert restarted.post(base + f"/candidates/{candidate['id']}/accept").status_code == 409


def test_user_reservation_information_resumes_saved_draft_through_review(tmp_path, monkeypatch):
    monkeypatch.setenv("AMAP_API_KEY", "fixture-map-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-model-key")
    model_roles, searches, routes = [], [], []
    original = itinerary()
    updated = deepcopy(original)
    updated["stops"][0]["reason"] = "先在官方小程序约到时段，再按拟定时间入场。"

    def provider(request):
        if request.url.host != "api.deepseek.com":
            if "/direction/" in request.url.path:
                routes.append(request.url.path)
            return map_response(request)
        if request.url.path.endswith("/tools/search"):
            searches.append(request.url.path)
            return sources()
        body = json.loads(request.content)
        prompt = json.loads(body["messages"][-1]["content"])
        if "research_report" in tool_names(body):
            model_roles.append("research")
            return completion(
                "research_report",
                {
                    "facts": [
                        {
                            "place_id": "p0",
                            "day": 1,
                            "field": "reservation_requirement",
                            "status": "unknown",
                        }
                    ]
                },
                1,
            )
        if "review_report" in tool_names(body):
            model_roles.append("reviewer")
            assert prompt["program_issues"] == []
            assert prompt["draft"] == updated
            assert prompt["queried_data"]["planning_facts"][0]["evidence_status"] == "user_reported"
            return completion("review_report", {"summary": "通过", "suggestions": []}, 1)
        model_roles.append(prompt["task"])
        user_facts = [
            fact
            for fact in prompt["planning_facts"]
            if fact.get("evidence_status") == "user_reported"
        ]
        if user_facts:
            assert prompt["task"] == "revision" and prompt["draft"] == original
            assert [(fact["field"], fact["value"]) for fact in user_facts] == [
                ("reservation_requirement", "当天必须提前预约"),
                ("reservation_channel", "官方微信小程序"),
            ]
            return completion("draft_proposal", packet(updated, queries=[]), 1)
        needs = (
            gap(field="reservation_requirement")["needs"] if prompt["task"] == "first_draft" else []
        )
        return completion(
            "draft_proposal", packet(original, queries=None if needs else [], needs=needs), 1
        )

    async def simulate_suppliers(*args, **kwargs):
        return await run_plan(*args, **kwargs, transport=httpx2.MockTransport(provider))

    monkeypatch.setattr(application, "run_plan", simulate_suppliers)
    with TestClient(application.create_app(str(tmp_path / "resume.sqlite3"))) as client:
        trip = client.post("/api/trips", json=requirements().model_dump(mode="json")).json()
        base = f"/api/trips/{trip['id']}"
        source = client.post(base + "/plan", json={}).json()
        assert source["outcome"] == "needs_information"
        assert len(searches) == len(routes) == 1
        payload = {
            "source_plan_id": source["id"],
            "expected_requirements_revision": 1,
            "supplied_facts": [
                {
                    "day": 1,
                    "place_id": "p0",
                    "field": "reservation_requirement",
                    "required": True,
                    "channel": "官方微信小程序",
                }
            ],
        }
        wrong_place = deepcopy(payload)
        wrong_place["supplied_facts"][0]["place_id"] = "p1"
        assert client.post(base + "/plan", json=wrong_place).status_code == 422
        response = client.post(base + "/plan", json=payload)
        assert response.status_code == 200, response.text
        candidate = response.json()
        assert candidate["outcome"] == "ready" and candidate["review"]["status"] == "approved"
        assert candidate["source_plan_id"] == source["id"] and candidate["itinerary"] == updated
        assert candidate["route_table"][0]["status"] == "map_estimate"
        saved_run = client.get(base + "/planning-runs").json()["runs"][0]
        assert saved_run["route_table"] == candidate["route_table"]
        assert model_roles == ["first_draft", "research", "revision", "revision", "reviewer"]
        assert len(searches) == len(routes) == 1
        assert client.get(base).json()["accepted_version"] == 0
        history = client.get(base + "/history").json()["candidates"]
        assert [item["id"] for item in history] == [candidate["id"], source["id"]]
        assert history[1]["outcome"] == "needs_information" and history[1]["draft"] == original
        assert client.post(base + f"/candidates/{candidate['id']}/accept").status_code == 200
        assert client.post(base + "/plan", json=payload).status_code == 409
