"""Observe actual SDK HTTP flow with synthetic supplier responses; no paid calls."""

import asyncio
import json

import httpx2
import pytest
from fastapi.testclient import TestClient
from test_planning_research import fact, sources
from test_planning_research import final as research_final
from test_planning_workflow import gap, itinerary, map_response, requirements, tool_names
from test_real_planner import configured
from test_research_planner_context import completion

from tripweave.app import create_app
from tripweave.model_usage import ModelUsageRecorder
from tripweave.real_planner import run_plan
from tripweave.settings import Settings
from tripweave.storage import Store


async def test_role_requests_tokens_and_limits_match_supplier_without_double_counting(tmp_path):
    roles = {"planner": 0, "research": 0, "reviewer": 0}
    bodies = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        if request.url.path.endswith("/tools/search"):
            return sources()
        body = json.loads(request.content)
        bodies.append(body)
        role = (
            "reviewer"
            if "review_report" in tool_names(body)
            else "research"
            if "search_place_information" in tool_names(body)
            else "planner"
        )
        roles[role] += 1
        n = len(bodies)
        if role == "reviewer":
            reply = completion("review_report", {"summary": "模拟认可", "suggestions": []}, n)
        elif role == "planner":
            reply = (
                completion("request_information", gap(), n)
                if roles[role] == 1
                else completion("final_result", itinerary(), n)
            )
        else:
            reply = (
                completion("search_place_information", {"place_id": "p0", "day": 1}, n)
                if roles[role] == 1
                else research_final(body, [fact()])
            )
        data = reply.json()
        data["usage"] = {
            "prompt_tokens": n * 100,
            "completion_tokens": n * 10,
            "total_tokens": n * 110,
            "prompt_tokens_details": {"cached_tokens": 20},
            "prompt_cache_miss_tokens": n * 100 - 20,
            "completion_tokens_details": {"reasoning_tokens": 3},
        }
        data["choices"][0]["message"]["reasoning_content"] = "PRIVATE-REASONING-NOT-OBSERVABLE"
        return httpx2.Response(200, json=data)

    settings = configured().model_copy(
        update={"planner_max_tokens": 7000, "research_max_tokens": 5000}
    )
    result = await run_plan(
        requirements(), "deepseek", settings, transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "ready", result
    assert result["usage"] == {"requests": 5, "input_tokens": 1500, "output_tokens": 150}
    observation = result["usage_observation"]
    assert observation["model_http_attempts"] == 5 and observation["usage_complete"]
    rows = observation["model_calls"]
    assert [r["role"] for r in rows] == ["planner", "research", "research", "planner", "reviewer"]
    assert [r["input_tokens"] for r in rows] == [100, 200, 300, 400, 500]
    assert [r["max_output_tokens"] for r in rows] == [7000, 5000, 5000, 7000, 6000]
    assert [body["max_completion_tokens"] for body in bodies] == [7000, 5000, 5000, 7000, 6000]
    assert all(r["reasoning_tokens"] == 3 and r["cache_read_tokens"] == 20 for r in rows)
    assert [r["cache_miss_tokens"] for r in rows] == [80, 180, 280, 380, 480]
    assert all(r["finish_reason"] == "tool_calls" and r["elapsed_ms"] >= 0 for r in rows)
    assert "PRIVATE-REASONING" not in json.dumps(observation)
    assert all(
        set(r)
        == {
            "number",
            "started_at",
            "model",
            "role",
            "status",
            "max_output_tokens",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_miss_tokens",
            "finish_reason",
            "http_status",
            "elapsed_ms",
        }
        for r in rows
    )

    store = Store(str(tmp_path / "observations.db"))
    assert result["api_cost_estimate"]["search"]["min_cny"] == "0.00500000"
    store.initialize()
    trip = store.create_trip(requirements().model_dump(mode="json"))
    store.add_candidate(
        {
            **result,
            "id": "observed",
            "trip_id": trip["id"],
            "mode": "deepseek",
            "base_version": 0,
            "requirements_revision": 1,
        }
    )
    store.dispose()
    reopened = Store(str(tmp_path / "observations.db"))
    assert reopened.get_candidate(trip["id"], "observed")["usage_observation"] == observation
    assert (
        reopened.get_candidate(trip["id"], "observed")["api_cost_estimate"]
        == result["api_cost_estimate"]
    )
    reopened.dispose()


async def test_missing_provider_usage_is_unknown_not_zero():
    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        data = (
            completion("review_report", {"summary": "模拟认可", "suggestions": []}, 2).json()
            if "review_report" in tool_names(body)
            else completion("final_result", itinerary(), 1).json()
        )
        data.pop("usage")
        return httpx2.Response(200, json=data)

    result = await run_plan(
        requirements(), "deepseek", configured(), transport=httpx2.MockTransport(provider)
    )
    observation = result["usage_observation"]
    assert result["outcome"] == "ready"
    assert observation["model_http_attempts"] == 2 and not observation["usage_complete"]
    row = observation["model_calls"][0]
    assert row["input_tokens"] is row["output_tokens"] is row["reasoning_tokens"] is None


async def test_http_error_attempt_is_recorded_without_error_body_or_invented_usage():
    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        return httpx2.Response(
            429, json={"error": {"message": "PRIVATE-ERROR-BODY", "type": "rate_limit"}}
        )

    result = await run_plan(
        requirements(), "deepseek", configured(), transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "service_error"
    observation = result["usage_observation"]
    assert observation["model_http_attempts"] == 1
    row = observation["model_calls"][0]
    assert row["status"] == "http_error" and row["http_status"] == 429
    assert row["input_tokens"] is row["output_tokens"] is None
    assert "PRIVATE-ERROR" not in json.dumps(observation)


async def test_timeout_retains_attempt_but_does_not_estimate_tokens():
    recorder = ModelUsageRecorder()
    req = httpx2.Request(
        "POST",
        "https://api.deepseek.com/chat/completions",
        json={"messages": [{"content": "PRIVATE-PROMPT"}], "max_tokens": 6000},
    )
    await recorder.request(req)
    result = recorder.snapshot(configured())
    assert result["model_calls"][0]["status"] == "no_complete_response"
    assert result["model_calls"][0]["input_tokens"] is None
    assert not result["usage_complete"]
    assert "PRIVATE-PROMPT" not in json.dumps(result)


async def test_raw_length_reason_cache_zero_and_invalid_counts_are_not_confused():
    recorder = ModelUsageRecorder()
    request = httpx2.Request("POST", "https://api.deepseek.com/chat/completions", json={})
    await recorder.request(request)
    await recorder.response(
        httpx2.Response(
            200,
            request=request,
            json={
                "usage": {
                    "prompt_tokens": False,
                    "completion_tokens": -1,
                    "prompt_cache_hit_tokens": 0,
                    "completion_tokens_details": {"reasoning_tokens": "not-numeric"},
                },
                "choices": [{"finish_reason": "length", "message": {"content": "PRIVATE-RESULT"}}],
            },
        )
    )
    row = recorder.snapshot(configured())["model_calls"][0]
    assert row["finish_reason"] == "length" and row["cache_read_tokens"] == 0
    assert row["input_tokens"] is row["output_tokens"] is row["reasoning_tokens"] is None
    assert "PRIVATE-RESULT" not in json.dumps(row)


async def test_role_context_does_not_leak_across_concurrent_runs():
    recorder = ModelUsageRecorder()

    async def make(role):
        with recorder.role(role):
            await asyncio.sleep(0)
            await recorder.request(
                httpx2.Request("POST", "https://api.deepseek.com/chat/completions", json={})
            )

    await asyncio.gather(make("planner"), make("research"))
    assert {r["role"] for r in recorder.calls} == {"planner", "research"}
    await recorder.request(
        httpx2.Request("POST", "https://api.deepseek.com/chat/completions", json={})
    )
    assert recorder.calls[-1]["role"] == "planner"


def test_active_config_exposes_budget_not_credentials(tmp_path):
    with TestClient(create_app(str(tmp_path / "config.db"))) as client:
        budget = client.get("/api/config").json()["planning_budget"]
    assert budget == {
        "max_model_requests": 8,
        "run_timeout_seconds": 180,
        "planner_max_output_tokens": 6000,
        "research_max_output_tokens": 4000,
        "reviewer_max_output_tokens": 6000,
        "input_token_limit": None,
    }
    with pytest.raises(ValueError):
        Settings(_env_file=None, planner_max_tokens=0)
