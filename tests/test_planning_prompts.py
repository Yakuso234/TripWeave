"""Assert stage isolation in the actual SDK request, without live suppliers."""

import json

import httpx2
from draft_helpers import packet, reply, route_queries
from test_planning_workflow import itinerary, map_response, requirements, tool_names
from test_real_planner import configured

from tripweave.planning_prompts import planner_instructions
from tripweave.real_planner import run_plan


async def test_actual_requests_use_first_draft_then_evidence_revision_instructions():
    phases = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        if tool_names(body) == {"review_report"}:
            return reply("review_report", {"summary": "合成通过", "suggestions": []})
        context = json.loads(body["messages"][-1]["content"])
        system = "\n".join(m.get("content", "") for m in body["messages"] if m["role"] == "system")
        phases.append(context["task"])
        assert "需求分为三类" in system and "strict_preferences" in system
        assert "respond_to_review" not in system
        if context["task"] == "first_draft":
            assert "本阶段只提出一份供证据核验的完整初稿" in system
            assert "revision_days" not in system
            assert "提交前逐天自查计数" not in system
            assert "未知路线可暂估并列查询" in context["instruction"]
        else:
            assert context["task"] == "revision" and context["draft"]
            assert "本阶段只提出" not in system
            assert "提交前逐天自查计数" in system and "revision_days" in system
        assert body["reasoning_effort"] == "max"
        assert body["thinking"] == {"type": "enabled"}
        assert body.get("max_tokens", body.get("max_completion_tokens")) == 6000
        draft = itinerary()
        return reply("draft_proposal", packet(draft, queries=route_queries(draft)))

    result = await run_plan(
        requirements(), "deepseek", configured(), transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "ready", result
    assert phases == ["first_draft", "revision"]


def test_review_response_keeps_authority_rules_and_uses_its_own_contract():
    instructions = planner_instructions("review_response")
    assert "使用respond_to_review输出合同" in instructions
    assert "提交前逐天自查计数" in instructions
    assert "本阶段只提出" not in instructions
    assert "需求分为三类" in instructions
