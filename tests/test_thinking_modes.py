"""Verify DeepSeek thinking configuration and history with real SDK HTTP mocks."""

import json
from copy import deepcopy

import httpx2
import pytest
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
    # The SDK must preserve the provider field, not put private thinking in content.
    for message in body["messages"]:
        if message["role"] == "assistant":
            assert "reasoning_content" in message
            assert "PRIVATE-THINKING" not in (message.get("content") or "")


@pytest.mark.parametrize("planner,research", [("max", "high"), ("high", "max")])
async def test_both_roles_think_at_configured_effort_and_keep_reasoning_separate(planner, research):
    counters = {"planner": 0, "research": 0}
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        if request.url.path.endswith("/tools/search"):
            return sources()
        body = json.loads(request.content)
        role = "research" if "search_place_information" in tool_names(body) else "planner"
        counters[role] += 1
        number = counters[role]
        assert_mode(body, planner if role == "planner" else research)
        other = "planner" if role == "research" else "research"
        assert f"PRIVATE-THINKING-{other}" not in json.dumps(body["messages"])
        if number > 1:
            assistants = [m for m in body["messages"] if m["role"] == "assistant"]
            assert assistants[0]["reasoning_content"] == f"PRIVATE-THINKING-{role}-1"
        calls.append(body)
        if role == "research":
            response = (
                completion("search_place_information", {"place_id": "p0", "day": 1}, 2)
                if number == 1
                else research_final(body, [fact()])
            )
        else:
            response = (
                completion("request_information", gap(), 1)
                if number == 1
                else completion("final_result", itinerary(), 4)
            )
        return with_thinking(response, f"PRIVATE-THINKING-{role}-{number}")

    settings = configured().model_copy(
        update={"planner_effort": planner, "research_effort": research}
    )
    result = await run_plan(
        requirements(), "deepseek", settings, transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "ready", result
    assert counters == {"planner": 2, "research": 2}
    assert result["usage"]["requests"] == 4
    assert result["thinking_configuration"] == {
        "planner": {"enabled": True, "effort": planner},
        "research": {"enabled": True, "effort": research},
    }
    assert "PRIVATE-THINKING" not in json.dumps(result)
    assert [b["max_completion_tokens"] for b in calls] == [6000, 4000, 4000, 6000]


async def test_planner_tool_and_local_repair_replay_all_thinking_fields():
    calls = []
    original = itinerary()
    broken = deepcopy(original)
    broken["stops"][0]["start_time"] = "00:00"

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        assert_mode(body, "max")
        for message in body["messages"]:
            if message["role"] == "assistant":
                assert message["reasoning_content"].startswith("PRIVATE-THINKING-round-")
        calls.append(body)
        number = len(calls)
        if number == 1:
            response = completion("lookup_places", {}, number)
        elif number == 2:
            response = completion("final_result", broken, number)
        else:
            assert "revise_days" in tool_names(body)
            response = completion("revise_days", {"stops": original["stops"]}, number)
        return with_thinking(response, f"PRIVATE-THINKING-round-{number}")

    result = await run_plan(
        requirements(), "deepseek", configured(), transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "ready", result
    assert len(calls) == 3 and result["usage"]["requests"] == 3
    assert "PRIVATE-THINKING" not in json.dumps(result)


@pytest.mark.parametrize("field", ["planner_effort", "research_effort"])
def test_invalid_effort_is_rejected(field):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: "invalid"})
