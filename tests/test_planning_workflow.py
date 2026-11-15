"""Actual SDK/LangGraph orchestration with deterministic supplier HTTP responses."""

import json
from copy import deepcopy

import httpx2
import pytest
from fastapi.testclient import TestClient
from test_planning_research import fact, sources
from test_planning_research import final as research_final
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
    return await run_plan(req, "deepseek", settings, transport=httpx2.MockTransport(provider))


async def test_declared_booking_outside_daily_bounds_stops_before_any_supplier_call():
    def forbidden(request):
        raise AssertionError("a proved user requirement conflict must not call suppliers")

    result = await run(
        requirements(booking_query="上海博物馆", booking_start="21:00", booking_end="22:00"),
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
        if len(calls) == 1:
            return completion("final_result", broken, 1)
        assert "revise_days" in tool_names(body)
        return completion("revise_days", {"stops": [original["stops"][0]]}, 2)

    result = await run(requirements(days=2, max_activities=1), provider)
    assert result["status"] == result["outcome"] == "ready", result
    assert result["usage"]["requests"] == len(calls) == 2
    revision = json.loads(calls[1]["messages"][-1]["content"])
    assert revision["revision_days"] == [1]
    assert revision["reference_stops"][0]["day"] == 2
    assert revision["validation_issues"][0]["code"] == "connection"
    assert revision["validation_issues"][0]["category"] == "draft_error"
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
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return completion("final_result", broken, 1)
        return completion("revise_days", {"stops": [original["stops"][0]]}, 2)

    result = await run(requirements(days=2, max_sightseeing_minutes=90), provider)
    assert result["status"] == result["outcome"] == "ready", result
    revision = json.loads(calls[1]["messages"][-1]["content"])
    assert revision["revision_days"] == [1]
    assert revision["reference_stops"] == [
        {
            key: original["stops"][1][key]
            for key in ("day", "place_id", "start_time", "end_time", "kind")
        }
    ]
    issue = revision["validation_issues"][0]
    assert (issue["code"], issue["actual"], issue["limit"]) == ("max_sightseeing_minutes", 120, 90)
    assert issue["occurrences"][0]["stop_number"] == 1
    assert issue["occurrences"][0]["end_time"] == "12:00"
    assert result["itinerary"] == original
    assert [row["sightseeing_minutes"] for row in result["daily_metrics"]] == [60, 60]
    assert result["usage"]["requests"] == 2


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
            return completion("request_information", gap(), number)
        if number == 2:
            events.append("research_tool")
            assert "search_place_information" in tool_names(body)
            return completion("search_place_information", {"place_id": "p0", "day": 1}, number)
        if number == 3:
            events.append("research_report")
            return research_final(body, [fact()])
        assert number == 4
        events.append("planner_resume")
        assert "search_place_information" not in tool_names(body)
        return completion("final_result", itinerary(), number)

    result = await run(requirements(notes=PRIVATE_NOTE), provider)
    assert result["outcome"] == "ready", result
    assert result["usage"]["requests"] == 4
    assert result["usage"]["input_tokens"] == 42 * 4
    assert result["usage"]["output_tokens"] == 20 * 4
    assert result["research_rounds"] == result["research_search_calls"] == len(searches) == 1
    assert events == [
        "planner_gap",
        "research_tool",
        "search_http",
        "research_report",
        "planner_resume",
    ]
    research_messages = json.dumps([body["messages"] for body in models[1:3]])
    assert PRIVATE_NOTE not in research_messages
    assert "draft_stops" not in research_messages and "previous_plan" not in research_messages
    research_prompt = json.loads(models[1]["messages"][-1]["content"])
    assert set(research_prompt) == {"city", "tasks"}
    assert research_prompt["tasks"][0]["date"] == "2026-11-21"
    assert PRIVATE_NOTE not in json.dumps(searches)
    resumed = json.loads(models[3]["messages"][-1]["content"])
    assert resumed["planning_facts"][0]["place_id"] == "p0"
    assert resumed["planning_facts"][0]["evidence_status"] == "web_reported"
    assert resumed["planning_facts"][0]["sources"][0]["url"] == "https://museum.example/visit"
    assert result["research_facts"] == resumed["planning_facts"]


async def test_fact_return_allows_one_full_revision_then_restores_local_repairs():
    original = itinerary(days=2)
    original["stops"][0]["reason"] = "PRIVATE-DRAFT-REASON-MUST-NOT-REACH-RESEARCH"
    first_draft = deepcopy(original)
    first_draft["stops"][0]["start_time"] = "00:00"
    after_research = deepcopy(original)
    after_research["stops"][1]["start_time"] = "00:00"
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        if request.url.path.endswith("/tools/search"):
            return sources()
        calls.append(body)
        number = len(calls)
        if number == 1:
            return completion("final_result", first_draft, number)
        if number == 2:
            return completion("request_information", gap(), number)
        if number == 3:
            return completion("search_place_information", {"place_id": "p0", "day": 1}, number)
        if number == 4:
            return research_final(body, [fact()])
        if number == 5:
            assert "final_result" in tool_names(body)
            return completion("final_result", after_research, number)
        assert number == 6
        assert "revise_days" in tool_names(body)
        return completion("revise_days", {"stops": [original["stops"][1]]}, number)

    result = await run(requirements(days=2, notes=PRIVATE_NOTE), provider)
    assert result["outcome"] == "ready", result
    assert result["usage"]["requests"] == len(calls) == 6
    assert result["itinerary"] == original
    research_input = json.dumps([body["messages"] for body in calls[2:4]])
    assert "PRIVATE-DRAFT-REASON" not in research_input and PRIVATE_NOTE not in research_input
    after_facts = json.loads(calls[4]["messages"][-1]["content"])
    assert after_facts["draft"] == first_draft
    assert after_facts["planning_facts"][0]["field"] == "opening_hours"
    assert "revision_days" not in after_facts
    next_revision = json.loads(calls[5]["messages"][-1]["content"])
    assert next_revision["revision_days"] == [2]
    assert next_revision["reference_stops"][0]["day"] == 1
    assert result["research_rounds"] == result["research_search_calls"] == 1


@pytest.mark.parametrize("search_mode", ["unknown", "no_results", "service_error"])
async def test_unresolved_or_failed_search_does_not_restart_planning(search_mode):
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
        if len(models) == 1:
            return completion("request_information", gap(), 1)
        if len(models) == 2:
            return completion("search_place_information", {"place_id": "p0", "day": 1}, 2)
        assert len(models) == 3
        return research_final(
            body, [{"place_id": "p0", "day": 1, "field": "opening_hours", "status": "unknown"}]
        )

    result = await run(requirements(), provider)
    expected = "service_error" if search_mode == "service_error" else "needs_information"
    assert result["outcome"] == expected, result
    assert result["status"] == "conflict" and result["itinerary"] is None
    assert result["usage"]["requests"] == len(models) == 3
    assert result["research_rounds"] == len(searches) == 1
    assert result["research_facts"][0]["status"] == "unknown"
    assert result["issues"][0]["missing_fields"] == ["opening_hours"]
    assert bool(result["error"]) == (search_mode == "service_error")


async def test_second_information_request_stops_after_single_research_round():
    calls, searches = [], []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        if request.url.path.endswith("/tools/search"):
            searches.append(body)
            return sources()
        calls.append(body)
        number = len(calls)
        if number == 1:
            return completion("request_information", gap(), number)
        if number == 2:
            return completion("search_place_information", {"place_id": "p0", "day": 1}, number)
        if number == 3:
            return research_final(body, [fact()])
        assert number == 4
        return completion("request_information", gap(field="last_admission"), number)

    result = await run(requirements(), provider)
    assert result["outcome"] == "needs_information", result
    assert result["research_rounds"] == len(searches) == 1
    assert result["usage"]["requests"] == len(calls) == 4
    assert result["research_facts"][0]["field"] == "opening_hours"
    assert result["issues"][0]["missing_fields"] == ["last_admission"]


async def test_shared_budget_exhaustion_retains_failed_draft_and_original_problem():
    broken = itinerary()
    broken["stops"][0]["start_time"] = "00:00"
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return completion("final_result", broken, 1)
        assert len(calls) == 2
        return completion("lookup_places", {}, 2)

    result = await run(requirements(), provider, limit=2)
    assert result["outcome"] == "incomplete", result
    assert result["usage"]["requests"] == len(calls) == 2
    assert result["draft"] == broken and result["itinerary"] is None
    assert result["issues"][0]["code"] == "connection"
    assert all(issue["category"] != "hard_conflict" for issue in result["issues"])
    assert "不代表需求无解" in result["error"]


async def test_research_and_planning_cannot_each_reset_the_request_budget():
    calls, searches = [], []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        if request.url.path.endswith("/tools/search"):
            searches.append(body)
            return sources()
        calls.append(body)
        number = len(calls)
        if number == 1:
            return completion("request_information", gap(), number)
        if number == 2:
            return completion("search_place_information", {"place_id": "p0", "day": 1}, number)
        assert number == 3  # A fresh planner budget would incorrectly make a fourth request.
        return research_final(body, [fact()])

    result = await run(requirements(), provider, limit=3)
    assert result["outcome"] == "incomplete", result
    assert result["usage"]["requests"] == len(calls) == 3
    assert result["research_rounds"] == len(searches) == 1
    assert result["research_facts"][0]["status"] == "found"
    assert result["itinerary"] is None


async def test_move_to_unresearched_day_keeps_a_declared_critical_gap_open():
    calls, searches = [], []
    moved = itinerary(days=2)
    moved["stops"][0]["place_id"] = "p1"
    moved["stops"][1]["place_id"] = "p0"

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        if request.url.path.endswith("/tools/search"):
            searches.append(body)
            return sources()
        calls.append(body)
        number = len(calls)
        if number == 1:
            return completion("request_information", gap(), number)
        if number == 2:
            return completion("search_place_information", {"place_id": "p0", "day": 1}, number)
        if number == 3:
            return research_final(body, [fact()])
        assert number == 4
        return completion("final_result", moved, number)

    result = await run(requirements(days=2), provider)
    assert result["outcome"] == "needs_information", result
    assert result["status"] == "conflict" and result["itinerary"] is None
    assert result["draft"] == moved
    assert result["research_rounds"] == len(searches) == 1
    assert result["usage"]["requests"] == len(calls) == 4
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
        assert len(calls) == 1
        return completion("final_result", plan, 1)

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
        assert len(calls) == 1
        return completion("final_result", plan, 1)

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
    plan = itinerary()
    corrected = deepcopy(plan["stops"][0])
    corrected.update(start_time="11:00", end_time="12:00")
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        return (
            completion("final_result", plan, 1)
            if len(calls) == 1
            else completion("revise_days", {"stops": [corrected]}, 2)
        )

    result = await run(requirements(visit_constraints=[constraint]), provider)
    assert result["outcome"] == "ready", result
    initial = json.loads(calls[0]["messages"][-1]["content"])
    assert initial["requirements"]["visit_constraints"][0]["constraint_id"] == "user-window-a"
    assert initial["queried_data"]["constrained_places"]["user-window-a"]["id"] == "p0"
    revision = json.loads(calls[1]["messages"][-1]["content"])
    issue = next(i for i in revision["validation_issues"] if i["code"] == "visit_constraint")
    assert issue["constraint_ids"] == ["user-window-a"] and issue["day"] == 1
    assert issue["actual"][0]["start_time"] == "10:00"
    assert result["itinerary"]["stops"] == [corrected]


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
