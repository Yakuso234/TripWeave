"""Thinking stays configured while draft-first stages exchange explicit data only."""

import json
from copy import deepcopy

import httpx2
import pytest
from draft_helpers import packet
from pydantic import ValidationError
from test_planning_research import fact, sources
from test_planning_research import final as research_final
from test_planning_workflow import gap, itinerary, map_response, requirements, tool_names
from test_real_planner import configured
from test_research_planner_context import completion

from tripweave.real_planner import run_plan
from tripweave.settings import Settings


def with_thinking(response, marker):
    payload = response.json()
    payload["choices"][0]["message"]["reasoning_content"] = marker
    return httpx2.Response(200, json=payload)


def assert_mode(body, effort):
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == effort
    for message in body["messages"]:
        if message["role"] == "assistant":
            assert "reasoning_content" in message
            assert "PRIVATE-THINKING" not in (message.get("content") or "")


@pytest.mark.parametrize(
    "planner,research,reviewer", [("max", "high", "low"), ("high", "max", "max")]
)
async def test_three_roles_think_at_configured_effort_and_only_explicit_context_crosses_stages(
    planner, research, reviewer
):
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
            "reviewer"
            if names == {"review_report"}
            else "research"
            if names == {"research_report"}
            else "planner"
        )
        counters[role] += 1
        number = counters[role]
        assert names == {
            {
                "planner": "draft_proposal",
                "research": "research_report",
                "reviewer": "review_report",
            }[role]
        }
        assert_mode(body, {"planner": planner, "research": research, "reviewer": reviewer}[role])
        assert "PRIVATE-THINKING" not in json.dumps(body["messages"])
        calls.append(body)
        if role == "reviewer":
            response = completion(
                "review_report", {"summary": "模拟认可", "suggestions": []}, len(calls)
            )
        elif role == "research":
            context = json.loads(body["messages"][-1]["content"])
            assert context["tasks"][0]["sources"]
            response = research_final(body, [fact()])
        else:
            context = json.loads(body["messages"][-1]["content"])
            if number == 1:
                assert context["task"] == "first_draft"
                response = completion(
                    "draft_proposal", packet(itinerary(), needs=gap()["needs"]), len(calls)
                )
            else:
                assert context["task"] == "revision"
                assert context["draft"] == itinerary()
                assert context["planning_facts"][0]["evidence_status"] == "web_reported"
                response = completion("draft_proposal", packet(itinerary(), queries=[]), len(calls))
        return with_thinking(response, f"PRIVATE-THINKING-{role}-{number}")

    settings = configured().model_copy(
        update={"planner_effort": planner, "research_effort": research, "reviewer_effort": reviewer}
    )
    result = await run_plan(
        requirements(), "deepseek", settings, transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "ready", result
    assert counters == {"planner": 2, "research": 1, "reviewer": 1}
    assert result["usage"]["requests"] == 4
    assert result["thinking_configuration"] == {
        "planner": {"enabled": True, "effort": planner},
        "research": {"enabled": True, "effort": research},
        "reviewer": {"enabled": True, "effort": reviewer},
    }
    assert "PRIVATE-THINKING" not in json.dumps(result)
    assert [b["max_tokens"] for b in calls] == [6000, 4000, 6000, 6000]
    assert all("max_completion_tokens" not in b for b in calls)
    assert [r["phase"] for r in result["usage_observation"]["model_calls"]] == [
        "first_draft",
        "research",
        "revision",
        "review",
    ]


async def test_planner_repair_uses_current_draft_and_validation_feedback_not_private_history():
    calls = []
    original = itinerary()
    broken = deepcopy(original)
    broken["stops"][0]["start_time"] = "00:00"
    planner_calls = 0

    def provider(request):
        nonlocal planner_calls
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        assert_mode(body, "high")
        assert "PRIVATE-THINKING" not in json.dumps(body["messages"])
        names = tool_names(body)
        if names == {"review_report"}:
            reply = completion(
                "review_report", {"summary": "模拟认可", "suggestions": []}, len(calls)
            )
        else:
            assert names == {"draft_proposal"}
            planner_calls += 1
            context = json.loads(body["messages"][-1]["content"])
            if planner_calls == 1:
                answer = packet(original)
            elif planner_calls == 2:
                assert context["draft"] == original
                assert context["evidence_results"]
                answer = packet(broken, queries=[])
            else:
                assert context["draft"] == broken
                assert context["validation_issues"][0]["code"] == "connection"
                assert context["revision_days"] == [1]
                answer = packet(original, queries=[])
            reply = completion("draft_proposal", answer, len(calls))
        return with_thinking(reply, f"PRIVATE-THINKING-stage-{len(calls)}")

    result = await run_plan(
        requirements(), "deepseek", configured(), transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "ready", result
    assert len(calls) == result["usage"]["requests"] == 4
    assert result["usage_observation"]["model_http_attempts"] == 4
    assert [r["role"] for r in result["usage_observation"]["model_calls"]] == [
        "planner",
        "planner",
        "planner",
        "reviewer",
    ]
    assert "PRIVATE-THINKING" not in json.dumps(result)


@pytest.mark.parametrize("field", ["planner_effort", "research_effort", "reviewer_effort"])
def test_invalid_effort_is_rejected(field):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: "invalid"})
