"""Three-role negotiation through real SDK/LangGraph with HTTP mocks only."""

import json
from copy import deepcopy

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_planning_research import fact, sources
from test_planning_research import final as research_final
from test_planning_workflow import gap, itinerary, map_response, requirements, tool_names
from test_real_planner import configured
from test_research_planner_context import completion

import tripweave.app as application
from tripweave.planning_review import PlannerReviewResponse, ReviewReport
from tripweave.real_planner import run_plan


def suggestion(identifier="pace-1", **changes):
    return {
        "suggestion_id": identifier,
        "positions": [{"day": 1, "stop_number": 1, "place_id": "p0"}],
        "concern": "游玩开始较早，用户希望慢游。",
        "requirement_basis": "用户本次要求轻松慢游。",
        "proposed_change": "可将首站推迟半小时，保留足够交通缓冲。",
        **changes,
    }


def report(*items):
    return {"summary": "根据当前用户要求复核草稿。", "suggestions": list(items)}


def decision(identifier="pace-1", action="decline", reason="原案仅游玩一小时，已留足休息。"):
    return {"suggestion_id": identifier, "action": action, "reason": reason}


async def negotiate(*, reports=None, response=None, req=None, limit=6, repair=None):
    """Explicitly respond to each role; do not use the old approve-review wrapper."""
    calls, reviews = [], []
    reports = reports if reports is not None else [report()]

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        names = tool_names(body)
        if "review_report" in names:
            assert names == {"review_report"}  # Reviewer cannot search or execute tools.
            reviews.append(body)
            answer = reports[min(len(reviews) - 1, len(reports) - 1)]
            return completion("review_report", answer, len(calls))
        if "respond_to_review" in names:
            context = json.loads(body["messages"][-1]["content"])
            assert context["task"] == "respond_to_review"
            system = "\n".join(
                message.get("content", "")
                for message in body["messages"]
                if message["role"] == "system"
            )
            assert "使用respond_to_review输出合同" in system
            assert "只输出计划或资料请求" not in system
            return completion("respond_to_review", response, len(calls))
        if "revise_days" in names:
            return completion(
                "revise_days", {"stops": (repair or itinerary())["stops"]}, len(calls)
            )
        return completion(
            "final_result", repair if repair and len(calls) > 1 else itinerary(), len(calls)
        )

    result = await run_plan(
        req or requirements(notes="用户希望轻松慢游。"),
        "deepseek",
        configured().model_copy(update={"max_requests": limit}),
        transport=httpx2.MockTransport(provider),
    )
    return result, calls, reviews


async def test_clean_review_is_required_before_ready_and_recorded_as_max_role():
    result, calls, reviews = await negotiate()
    assert result["outcome"] == result["status"] == "ready", result
    assert result["review"]["status"] == "approved"
    assert result["review"]["initial"] == report()
    assert "final" not in result["review"]
    assert len(calls) == 2 and len(reviews) == 1
    assert result["usage"] == {"requests": 2, "input_tokens": 84, "output_tokens": 40}
    assert [row["role"] for row in result["usage_observation"]["model_calls"]] == [
        "planner",
        "reviewer",
    ]
    assert reviews[0]["reasoning_effort"] == "max"
    assert reviews[0]["thinking"] == {"type": "enabled"}
    assert reviews[0]["max_completion_tokens"] == 6000
    assert result["thinking_configuration"]["reviewer"] == {"enabled": True, "effort": "max"}
    context = json.loads(reviews[0]["messages"][-1]["content"])
    assert context["draft"] == itinerary()
    assert context["program_issues"] == []
    assert context["requirements"]["notes"] == "用户希望轻松慢游。"
    assert context["verified_routes"]
    assert result["api_cost_estimate"]["models"]["status"] != "unavailable"


async def test_planner_revision_is_checked_then_reviewed_with_full_context():
    revised = itinerary()
    revised["stops"][0].update(
        start_time="10:30", end_time="11:30", reason="采纳慢游建议，推迟首站。"
    )
    response = {
        "decisions": [decision(action="revise", reason="推迟半小时更符合慢游偏好。")],
        "revised_itinerary": revised,
    }
    result, calls, reviews = await negotiate(
        reports=[report(suggestion()), report()], response=response
    )
    assert result["outcome"] == "ready", result
    assert result["itinerary"] == revised
    assert result["review"]["planner_response"] == response
    assert len(calls) == 4 and len(reviews) == 2
    assert [row["role"] for row in result["usage_observation"]["model_calls"]] == [
        "planner",
        "reviewer",
        "planner",
        "reviewer",
    ]
    final = json.loads(reviews[-1]["messages"][-1]["content"])
    assert final["draft"] == revised
    assert final["review"]["planner_decisions"] == response["decisions"]
    assert "original_draft" not in final["review"]
    assert "revised_itinerary" not in final["review"]
    assert json.loads(reviews[0]["messages"][-1]["content"])["draft"] == itinerary()
    assert final["program_issues"] == []
    assert sum(event["event"] == "validation" for event in result["trace"]) == 2


async def test_explained_decline_can_be_accepted_without_changing_any_stop():
    response = {"decisions": [decision()]}
    result, calls, reviews = await negotiate(
        reports=[report(suggestion()), report()], response=response
    )
    assert result["outcome"] == "ready", result
    assert result["itinerary"] == itinerary()
    assert result["review"]["planner_response"]["decisions"] == response["decisions"]
    assert result["review"]["final"] == report()
    assert len(calls) == 4 and len(reviews) == 2


async def test_unresolved_disagreement_retains_both_positions_and_draft():
    unresolved = suggestion(concern="仍建议晚出发，用户未说希望早到。")
    result, calls, _ = await negotiate(
        reports=[report(suggestion()), report(unresolved)],
        response={"decisions": [decision()]},
    )
    assert result["status"] == "conflict" and result["outcome"] == "needs_user_decision"
    assert result["itinerary"] is None and result["draft"] == itinerary()
    assert result["error"] is None
    assert result["review"]["status"] == "disputed"
    assert result["review"]["initial"]["suggestions"] == [suggestion()]
    assert result["review"]["planner_response"]["decisions"] == [decision()]
    assert result["review"]["final"]["suggestions"] == [unresolved]
    assert result["issues"] == [
        {
            "category": "review_disagreement",
            "code": "review_disagreement",
            "message": unresolved["concern"],
            "day": 1,
            "stop_number": 1,
            "place_id": "p0",
            "suggestion_id": "pace-1",
        }
    ]
    assert len(calls) == 4  # No unbounded self-debate after the final disagreement.


async def test_planner_may_accept_some_suggestions_and_decline_others():
    revised = itinerary()
    revised["stops"][0].update(start_time="10:30", end_time="11:30")
    response = {
        "decisions": [decision(action="revise"), decision("meal-2")],
        "revised_itinerary": revised,
    }
    result, calls, _ = await negotiate(
        reports=[
            report(suggestion(), suggestion("meal-2", concern="上午是否需增加正餐？")),
            report(),
        ],
        response=response,
    )
    assert result["outcome"] == "ready", result
    assert result["itinerary"] == revised
    assert result["review"]["planner_response"] == response
    assert len(calls) == 4


@pytest.mark.parametrize("limit,stage", [(2, "planner_response"), (3, "final_review")])
async def test_budget_exhaustion_is_incomplete_never_invented_disagreement(limit, stage):
    result, calls, _ = await negotiate(
        reports=[report(suggestion()), report()],
        response={"decisions": [decision()]},
        limit=limit,
    )
    assert result["outcome"] == "incomplete", result
    assert result["status"] == "conflict" and result["itinerary"] is None
    assert result["draft"] == itinerary()
    assert result["review"]["status"] == "incomplete"
    assert result["review"]["pending_stage"] == stage
    assert result["issues"] == []
    assert len(calls) == result["usage"]["requests"] == limit


@pytest.mark.parametrize(
    "position",
    [
        {"day": 2, "stop_number": 1, "place_id": "p0"},
        {"day": 1, "stop_number": 2, "place_id": "p0"},
        {"day": 1, "stop_number": 1, "place_id": "invented"},
    ],
)
async def test_invalid_review_location_is_not_shown_as_user_conflict(position):
    result, calls, _ = await negotiate(reports=[report(suggestion(positions=[position]))])
    assert result["outcome"] == "incomplete", result
    assert result["itinerary"] is None and result["draft"] == itinerary()
    assert result["review"]["status"] != "disputed"
    assert "不存在的日期、站序或地点" in result["error"]
    assert len(calls) == 2


@pytest.mark.parametrize(
    "decisions",
    [
        [decision("unknown")],
        [decision(), decision("unknown")],
    ],
)
async def test_planner_cannot_ignore_or_invent_suggestion_ids(decisions):
    result, calls, _ = await negotiate(
        reports=[report(suggestion())], response={"decisions": decisions}
    )
    assert result["outcome"] == "incomplete", result
    assert result["itinerary"] is None
    assert result["review"]["status"] != "disputed"
    assert "未逐条回应原校验意见" in result["error"]
    assert len(calls) == 3


async def test_review_cannot_authorize_omitting_users_selected_attraction():
    changed = itinerary()
    changed["stops"][0]["place_id"] = "p1"
    response = {"decisions": [decision(action="revise")], "revised_itinerary": changed}
    result, calls, reviews = await negotiate(
        req=requirements(selected_attractions=[{"id": "p0", "name": "接口景点0"}]),
        reports=[report(suggestion(proposed_change="删掉用户所选p0改去p1。")), report()],
        response=response,
        repair=changed,
    )
    assert result["outcome"] == "incomplete", result
    assert result["itinerary"] is None
    assert any(issue["code"] == "must_visit" for issue in result["issues"])
    assert len(reviews) == 1  # Program check must stop unsafe plan before final approval.
    assert len(calls) == 4


@pytest.mark.parametrize(
    "payload",
    [
        {"decisions": [decision(action="revise")]},
        {"decisions": [decision()], "revised_itinerary": itinerary()},
        {"decisions": [decision(), decision()]},
        {"decisions": [decision(reason=" ")]},
        {"decisions": [decision(action="approve")]},
    ],
)
def test_response_contract_rejects_ambiguous_edits_or_missing_reasons(payload):
    with pytest.raises(ValidationError):
        PlannerReviewResponse.model_validate(payload)


def test_review_contract_requires_unique_ids_and_actual_positions():
    for payload in [
        report(suggestion(), suggestion()),
        report(suggestion(positions=[])),
        {"summary": "遗漏了明确的校验结论字段。"},
    ]:
        with pytest.raises(ValidationError):
            ReviewReport.model_validate(payload)


async def test_research_planner_and_reviewer_keep_private_thinking_isolated():
    counters = {"planner": 0, "research": 0, "reviewer": 0}
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        if request.url.path.endswith("/tools/search"):
            return sources()
        body = json.loads(request.content)
        names = tool_names(body)
        role = (
            "research"
            if "search_place_information" in names
            else "reviewer"
            if "review_report" in names
            else "planner"
        )
        counters[role] += 1
        count = counters[role]
        calls.append(body)
        serialized = json.dumps(body["messages"])
        for other in counters:
            if other != role:
                assert f"PRIVATE-THINKING-{other}" not in serialized
        assert body["reasoning_effort"] == ("high" if role == "research" else "max")
        if role == "research":
            reply = (
                completion("search_place_information", {"place_id": "p0", "day": 1}, len(calls))
                if count == 1
                else research_final(body, [fact()])
            )
        elif role == "reviewer":
            context = json.loads(body["messages"][-1]["content"])
            assert context["queried_data"]["planning_facts"][0]["evidence_status"] == "web_reported"
            assert context["program_issues"] == []
            reply = completion("review_report", report(), len(calls))
        else:
            reply = (
                completion("request_information", gap(), len(calls))
                if count == 1
                else completion("final_result", itinerary(), len(calls))
            )
        payload = reply.json()
        payload["choices"][0]["message"]["reasoning_content"] = f"PRIVATE-THINKING-{role}-{count}"
        return httpx2.Response(200, json=payload)

    result = await run_plan(
        requirements(), "deepseek", configured(), transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "ready", result
    assert counters == {"planner": 2, "research": 2, "reviewer": 1}
    assert result["usage"]["requests"] == 5
    assert [row["role"] for row in result["usage_observation"]["model_calls"]] == [
        "planner",
        "research",
        "research",
        "planner",
        "reviewer",
    ]
    assert "PRIVATE-THINKING" not in json.dumps(result)


def test_disputed_review_survives_sqlite_restart_and_cannot_be_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("AMAP_API_KEY", "fixture-map-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-model-key")
    review = {
        "version": 1,
        "status": "disputed",
        "initial": report(suggestion()),
        "original_draft": itinerary(),
        "planner_response": {"decisions": [decision()]},
        "final": report(suggestion()),
    }
    fixture = {
        "status": "conflict",
        "outcome": "needs_user_decision",
        "itinerary": None,
        "draft": itinerary(),
        "review": review,
        "issues": [],
        "error": None,
    }

    async def simulated(*args, **kwargs):
        return deepcopy(fixture)

    monkeypatch.setattr(application, "run_plan", simulated)
    database = str(tmp_path / "review.sqlite3")
    with TestClient(application.create_app(database)) as client:
        trip = client.post("/api/trips", json={"date": "2026-11-21"}).json()
        base = f"/api/trips/{trip['id']}"
        candidate = client.post(base + "/plan", json={"expected_requirements_revision": 1}).json()
        assert client.post(base + f"/candidates/{candidate['id']}/accept").status_code == 409
    with TestClient(application.create_app(database)) as restarted:
        restored = restarted.get(base + "/history").json()["candidates"][0]
        assert restored["review"] == review
        assert restored["draft"] == itinerary()
        assert restarted.get(base).json()["accepted_version"] == 0
        assert restarted.post(base + f"/candidates/{candidate['id']}/accept").status_code == 409


async def test_review_revision_does_not_create_fourth_candidate_after_three_drafts():
    calls, reviews = [], []
    broken = itinerary()
    broken["stops"][0]["start_time"] = "00:00"
    revised = itinerary()
    revised["stops"][0].update(start_time="10:30", end_time="11:30")

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        names = tool_names(body)
        if "review_report" in names:
            reviews.append(body)
            return completion("review_report", report(suggestion()), len(calls))
        if "respond_to_review" in names:
            return completion(
                "respond_to_review",
                {
                    "decisions": [decision(action="revise")],
                    "revised_itinerary": revised,
                },
                len(calls),
            )
        if "revise_days" in names:
            return completion(
                "revise_days",
                {
                    "stops": (broken if len(calls) == 2 else itinerary())["stops"],
                },
                len(calls),
            )
        return completion("final_result", broken, len(calls))

    result = await run_plan(
        requirements(), "deepseek", configured(), transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "incomplete", result
    assert result["review"]["pending_stage"] == "candidate_limit"
    assert result["review"]["planner_response"]["revised_itinerary"] == revised
    assert result["draft"] == itinerary()
    assert result["itinerary"] is None
    assert len(reviews) == 1 and len(calls) == 5


async def test_reviewer_unavailable_is_service_error_not_planning_disagreement():
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        if "review_report" in tool_names(body):
            return httpx2.Response(
                429, json={"error": {"message": "PRIVATE-SUPPLIER-ERROR", "type": "rate_limit"}}
            )
        return completion("final_result", itinerary(), len(calls))

    result = await run_plan(
        requirements(), "deepseek", configured(), transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "service_error", result
    assert result["itinerary"] is None and result["draft"] == itinerary()
    assert result["review"]["status"] != "disputed"
    assert "PRIVATE-SUPPLIER-ERROR" not in json.dumps(result)
    rows = result["usage_observation"]["model_calls"]
    assert len(rows) == 2 and rows[-1]["role"] == "reviewer"
    assert rows[-1]["status"] == "http_error" and rows[-1]["http_status"] == 429


async def test_reviewer_cannot_call_unexposed_search_or_execution_tool():
    calls = []
    review_calls = 0

    def provider(request):
        nonlocal review_calls
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        assert request.url.path.endswith("/chat/completions")
        body = json.loads(request.content)
        calls.append(body)
        if "review_report" in tool_names(body):
            review_calls += 1
            assert tool_names(body) == {"review_report"}
            if review_calls == 1:
                return completion(
                    "search_place_information", {"place_id": "p0", "day": 1}, len(calls)
                )
            assert any(
                "Unknown tool name" in str(message.get("content")) for message in body["messages"]
            )
            return completion("review_report", report(), len(calls))
        return completion("final_result", itinerary(), len(calls))

    result = await run_plan(
        requirements(), "deepseek", configured(), transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "ready", result
    assert review_calls == 2 and len(calls) == result["usage"]["requests"] == 3
    assert result["research_rounds"] == result["research_search_calls"] == 0


async def test_negotiation_handoffs_do_not_share_other_agents_private_thinking():
    calls = []
    reviewers = 0

    def provider(request):
        nonlocal reviewers
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        names = tool_names(body)
        role = "reviewer" if "review_report" in names else "planner"
        serialized = json.dumps(body["messages"])
        other = "reviewer" if role == "planner" else "planner"
        assert f"PRIVATE-{other}" not in serialized
        if role == "reviewer":
            reviewers += 1
            if reviewers == 2:
                assert "PRIVATE-reviewer" in serialized
            reply = completion(
                "review_report", report(suggestion()) if reviewers == 1 else report(), len(calls)
            )
        elif "respond_to_review" in names:
            context = json.loads(body["messages"][-1]["content"])
            assert context["review"]["initial"] == report(suggestion())
            assert context["draft"] == itinerary()
            reply = completion("respond_to_review", {"decisions": [decision()]}, len(calls))
        else:
            reply = completion("final_result", itinerary(), len(calls))
        payload = reply.json()
        payload["choices"][0]["message"]["reasoning_content"] = f"PRIVATE-{role}"
        return httpx2.Response(200, json=payload)

    result = await run_plan(
        requirements(), "deepseek", configured(), transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "ready", result
    assert len(calls) == 4
    assert "PRIVATE-" not in json.dumps(result)


@pytest.mark.parametrize(
    "review",
    [
        {},
        {"status": "pending"},
        {"status": "disputed"},
        {"status": "incomplete"},
        {"status": "awaiting_adjudication"},
        "approved",
        [],
    ],
)
def test_review_gate_cannot_be_bypassed_by_legacy_ready_flags(review, tmp_path, monkeypatch):
    monkeypatch.setenv("AMAP_API_KEY", "fixture-map-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-model-key")

    async def simulated(*args, **kwargs):
        return {
            "status": "ready",
            "outcome": "ready",
            "itinerary": itinerary(),
            "review": review,
            "issues": [],
            "error": None,
        }

    monkeypatch.setattr(application, "run_plan", simulated)
    with TestClient(application.create_app(str(tmp_path / "gate.sqlite3"))) as client:
        trip = client.post("/api/trips", json={"date": "2026-11-21"}).json()
        base = f"/api/trips/{trip['id']}"
        candidate = client.post(base + "/plan", json={"expected_requirements_revision": 1}).json()
        assert client.post(base + f"/candidates/{candidate['id']}/accept").status_code == 409
        assert client.get(base).json()["accepted_version"] == 0


async def test_program_repair_after_review_revision_is_the_plan_seen_by_final_reviewer():
    calls, reviews = [], []
    revision_request = "本次修订特定要求：只延后出发，保留所选场馆，慢游核验标记-4827。"
    broken = itinerary()
    broken["stops"][0]["start_time"] = "00:00"
    fixed = itinerary()
    fixed["stops"][0].update(
        start_time="10:30", end_time="11:30", reason="修正错误时刻且遵从慢游偏好。"
    )

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        names = tool_names(body)
        context = json.loads(body["messages"][-1]["content"])
        assert context["revision_request"] == revision_request
        if "review_report" in names:
            reviews.append(body)
            if len(reviews) == 2:
                assert context["draft"] == fixed
                assert context["program_issues"] == []
            return completion(
                "review_report", report(suggestion()) if len(reviews) == 1 else report(), len(calls)
            )
        if "respond_to_review" in names:
            return completion(
                "respond_to_review",
                {"decisions": [decision(action="revise")], "revised_itinerary": broken},
                len(calls),
            )
        if "revise_days" in names:
            assert context["requirements"]["notes"] == "轻松慢游。"
            assert context["draft_stops"][0]["start_time"] == "00:00"
            return completion("revise_days", {"stops": fixed["stops"]}, len(calls))
        assert context["previous_plan"]["revision_request"] == revision_request
        return completion("final_result", itinerary(), len(calls))

    result = await run_plan(
        requirements(notes="轻松慢游。"),
        "deepseek",
        configured(),
        previous_plan={"revision_request": revision_request, "places": []},
        transport=httpx2.MockTransport(provider),
    )
    assert result["outcome"] == "ready", result
    assert result["itinerary"] == fixed
    assert result["review"]["planner_response"]["revised_itinerary"] == broken
    assert len(calls) == 5 and len(reviews) == 2


async def test_reviewer_omitting_explicit_suggestions_cannot_silently_approve():
    result, calls, reviews = await negotiate(
        reports=[{"summary": "只有摘要，没有明确是否有意见。"}]
    )
    assert result["outcome"] == "incomplete", result
    assert result["itinerary"] is None and result["draft"] == itinerary()
    assert result["review"]["status"] != "approved"
    assert "UnexpectedModelBehavior" in result["error"]
    assert len(calls) == 3 and len(reviews) == 2  # One bounded SDK output correction.
    assert any("suggestions" in str(message.get("content")) for message in reviews[-1]["messages"])
