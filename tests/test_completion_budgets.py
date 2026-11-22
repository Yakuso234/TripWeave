"""Completion-first caps are omitted, while physical checks still require repair."""

import json

import httpx2
from draft_helpers import phase_packet, reply, route_queries
from test_planning_workflow import itinerary, map_response, requirements, tool_names
from test_real_planner import configured

from tripweave.real_planner import run_plan


async def test_uncapped_model_output_and_candidates_can_finish_after_four_bad_drafts():
    settings = configured().model_copy(
        update={
            "max_requests": None,
            "max_candidates": None,
            "planner_max_tokens": None,
            "research_max_tokens": None,
            "reviewer_max_tokens": None,
            "run_timeout": 600,
        }
    )
    planner_calls = 0
    repairs = []

    def provider(request):
        nonlocal planner_calls
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        assert "max_tokens" not in body and "max_completion_tokens" not in body
        assert body["thinking"] == {"type": "enabled"}
        if tool_names(body) == {"review_report"}:
            return reply("review_report", {"summary": "合成认可", "suggestions": []})
        planner_calls += 1
        context = json.loads(body["messages"][-1]["content"])
        repairs.extend(i for i in context["validation_issues"] if i["code"] == "connection")
        draft = itinerary()
        draft["stops"][0]["transfer"] = {
            "departure_time": f"10:0{planner_calls}",
            "arrival_time": f"10:1{planner_calls}",
        }
        if planner_calls >= 6:
            draft["stops"][0]["transfer"] = {
                "departure_time": "09:40",
                "arrival_time": "09:45",
            }
        return reply("draft_proposal", phase_packet(context, draft, queries=route_queries(draft)))

    result = await run_plan(
        requirements(), "deepseek", settings, transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "ready", result
    assert planner_calls == 6 and len(repairs) >= 4
    assert result["review"]["status"] == "approved" and result["issues"] == []
    assert all(
        c["output_limit_parameter"] is None for c in result["usage_observation"]["model_calls"]
    )
