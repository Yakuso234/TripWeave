"""Network contract tests use simulated HTTP responses, never real provider results."""

import json

import httpx2
import pytest

from tripweave.domain import Requirements
from tripweave.maps import MapError
from tripweave.real_planner import run_plan
from tripweave.settings import Settings


def poi(pid, name, kind="风景名胜", x=0):
    return {
        "id": pid,
        "name": name,
        "location": f"{121.47 + x / 1000},31.23",
        "address": "测试地址",
        "type": kind,
    }


def handler(request):
    query = request.url.params
    path = request.url.path
    if path.endswith("/place/text"):
        if query.get("types") == "100000":
            places = [poi("h", "接口返回的酒店", "住宿服务;宾馆酒店")]
        elif query.get("keywords") == "人民广场":
            places = [poi("o", "人民广场")]
        elif query.get("keywords") == "上海博物馆":
            places = [poi("b", "上海博物馆")]
        else:
            places = [poi("p" + str(i), "接口景点" + str(i), x=i + 1) for i in range(8)]
        return httpx2.Response(200, json={"status": "1", "pois": places})
    if path.endswith("/place/around"):
        return httpx2.Response(
            200, json={"status": "1", "pois": [poi("r", "接口餐厅", "餐饮服务")]}
        )
    if "/direction/" in path:
        return httpx2.Response(
            200, json={"status": "1", "route": {"paths": [{"duration": "300", "distance": "500"}]}}
        )
    raise AssertionError("unexpected request " + path)


def configured():
    return Settings(
        _env_file=None, api_key="test-key-not-real", amap_api_key="test-map-key", run_timeout=30
    )


@pytest.mark.asyncio
async def test_basic_plans_real_adapter_results_and_reacts_to_input():
    transport = httpx2.MockTransport(handler)
    req = Requirements(date="2026-11-07", days=2)
    first = await run_plan(req, "basic", configured(), transport=transport)
    assert first["status"] == "ready", first
    assert first["hotel"]["name"] == "接口返回的酒店"
    assert first["hotel"]["nightly_rate"] is None
    assert first["costs"]["total"] is None
    assert {s["day"] for s in first["itinerary"]["stops"]} == {1, 2}
    changed = await run_plan(
        req.model_copy(update={"start_time": "10:00"}), "basic", configured(), transport=transport
    )
    assert (
        first["itinerary"]["stops"][0]["start_time"]
        != changed["itinerary"]["stops"][0]["start_time"]
    )
    assert any(
        s["kind"] == "booking" and s["start_time"] == "14:00" for s in changed["itinerary"]["stops"]
    )
    conflict = await run_plan(
        req.model_copy(update={"start_time": "14:30"}), "basic", configured(), transport=transport
    )
    assert conflict["status"] == "conflict"
    assert any(i["code"] == "connection" for i in conflict["issues"])


@pytest.mark.asyncio
async def test_permissions_are_explicit_and_never_leak_key():
    def denied(request):
        return httpx2.Response(200, json={"status": "0", "infocode": "10001"})

    result = await run_plan(
        Requirements(date="2026-11-07"),
        "basic",
        configured(),
        transport=httpx2.MockTransport(denied),
    )
    assert result["status"] == "conflict" and result["itinerary"] is None
    assert "10001" in result["error"] and "test-map-key" not in json.dumps(result)
    with pytest.raises(MapError):
        await run_plan(
            Requirements(date="2026-11-07"), "basic", Settings(_env_file=None, amap_api_key=None)
        )


@pytest.mark.asyncio
async def test_deepseek_contract_feedback_and_usage():
    req = Requirements(date="2026-11-07", days=2)
    seed = await run_plan(req, "basic", configured(), transport=httpx2.MockTransport(handler))
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return handler(request)
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            name, args = "lookup_places", {}
        elif len(calls) == 2:
            name, args = "nearby_restaurants", {"place_id": "o"}
        else:
            name = next(
                t["function"]["name"]
                for t in body["tools"]
                if t["function"]["name"] not in ("lookup_places", "route", "nearby_restaurants")
            )
            args = json.loads(json.dumps(seed["itinerary"]))
            if len(calls) == 3:
                args["stops"][0]["start_time"] = "00:00"
        return httpx2.Response(
            200,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call" + str(len(calls)),
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(args)},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 42, "completion_tokens": 20, "total_tokens": 62},
            },
        )

    output = await run_plan(req, "deepseek", configured(), transport=httpx2.MockTransport(provider))
    assert output["status"] == "ready", (output["error"], output["trace"])
    assert len(calls) == 4
    assert calls[0]["model"] == "deepseek-flash"
    assert output["usage"]["requests"] == 4
    assert output["usage"]["input_tokens"] == 168
    assert any(t["event"] == "validation" for t in output["trace"])
