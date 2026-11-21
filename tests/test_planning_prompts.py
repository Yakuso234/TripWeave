"""Assert stage isolation in the actual SDK request, without live suppliers."""

import json

import httpx2
import pytest
from draft_helpers import packet, reply, route_queries
from test_planning_workflow import itinerary, map_response, requirements, tool_names
from test_real_planner import configured

from tripweave.planning_prompts import planner_instructions
from tripweave.real_planner import run_plan


@pytest.mark.parametrize("base_url", ["https://api.deepseek.com", "https://api.deepseek.com/v1"])
async def test_actual_requests_use_first_draft_then_evidence_revision_instructions(base_url):
    phases = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        system = "\n".join(m.get("content", "") for m in body["messages"] if m["role"] == "system")
        if tool_names(body) == {"review_report"}:
            assert "全天交通明显超过期望并伴有多段实际缓冲偏紧" in system
            assert "普通交通缓冲不足5分钟仅作轻量建议" in system
            return reply("review_report", {"summary": "合成通过", "suggestions": []})
        context = json.loads(body["messages"][-1]["content"])
        phases.append(context["task"])
        assert "需求分为三类" in system and "strict_preferences" in system
        assert "respond_to_review" not in system
        if context["task"] == "first_draft":
            assert "本阶段只提出一份供证据核验的完整初稿" in system
            assert "revision_days" not in system
            assert "提交前逐天自查计数" not in system
            assert "可少排可选景点，不必填满每天" in system
            assert "未知路线可暂估并列查询" in context["instruction"]
        else:
            assert context["task"] == "revision" and context["draft"]
            assert "本阶段只提出" not in system
            assert "提交前逐天自查计数" in system and "revision_days" in system
            assert "把较晚的非必要等待前移给更紧的路段" in system
        assert body["reasoning_effort"] == "high"
        assert body["thinking"] == {"type": "enabled"}
        assert body["max_tokens"] == 6000
        assert "max_completion_tokens" not in body
        draft = itinerary()
        return reply("draft_proposal", packet(draft, queries=route_queries(draft)))

    result = await run_plan(
        requirements(),
        "deepseek",
        configured().model_copy(update={"base_url": base_url}),
        transport=httpx2.MockTransport(provider),
    )
    assert result["outcome"] == "ready", result
    assert phases == ["first_draft", "revision"]


def test_review_response_keeps_authority_rules_and_uses_its_own_contract():
    instructions = planner_instructions("review_response")
    assert "使用respond_to_review输出合同" in instructions
    assert "修订阶段输出DraftProposal" not in instructions
    assert "提交前逐天自查计数" in instructions
    assert "本阶段只提出" not in instructions
    assert "需求分为三类" in instructions
    assert "保留必去景点、必要停留、正餐和明确休息" in instructions
