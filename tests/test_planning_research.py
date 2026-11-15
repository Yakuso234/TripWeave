"""The research Agent runs against SDK-level HTTP mocks, never live APIs."""

import asyncio
import json

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import RunUsage
from test_real_planner import configured

from tripweave.domain import Requirements
from tripweave.planning_research import research_missing_information

QUOTE = "2026-11-21开放时间09:00至17:00，16:00停止入场。"
PLACES = {
    "p0": {"id": "p0", "name": "测试馆人民广场馆", "address": "测试地址"},
    "p1": {"id": "p1", "name": "另一处测试馆", "address": "另一区"},
}


def need(**changes):
    return {
        "place_id": "p0",
        "day": 1,
        "fields": ["opening_hours"],
        "question": "当天何时开放？",
        **changes,
    }


def fact(**changes):
    return {
        "place_id": "p0",
        "day": 1,
        "field": "opening_hours",
        "status": "found",
        "value": "09:00至17:00开放。",
        "source_ids": [1],
        "quote": QUOTE,
        "windows": [{"day": 1, "start_time": "09:00", "end_time": "17:00"}],
        **changes,
    }


def completion(calls):
    return httpx2.Response(
        200,
        json={
            "id": "research-completion",
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
                                "id": f"research-{i}",
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)},
                            }
                            for i, (name, args) in enumerate(calls)
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 42, "completion_tokens": 20, "total_tokens": 62},
        },
    )


def final(body, facts):
    tool = next(
        tool["function"]["name"]
        for tool in body["tools"]
        if tool["function"]["name"] != "search_place_information"
    )
    return completion([(tool, {"facts": facts})])


def sources(quote=QUOTE):
    return httpx2.Response(
        200,
        json={
            "results": [
                {
                    "url": "https://museum.example/visit",
                    "title": "指定日期参观说明",
                    "summary": quote,
                }
            ]
        },
    )


async def run_research(provider, needs=None, *, settings=None, usage=None):
    settings = settings or configured()
    usage = usage or RunUsage()
    trace = []
    transport = httpx2.MockTransport(provider)
    async with AsyncOpenAI(
        api_key="sdk-test-key",
        base_url="https://api.deepseek.com",
        http_client=httpx2.AsyncClient(transport=transport),
        max_retries=0,
    ) as sdk:
        model = OpenAIChatModel(
            settings.model,
            provider=OpenAIProvider(openai_client=sdk),
            profile=OpenAIModelProfile(
                openai_supports_strict_tool_definition=False, supports_forced_tool_choice=False
            ),
        )
        result = await research_missing_information(
            model,
            needs or [need()],
            Requirements(date="2026-11-21", days=2, notes="PRIVATE-NOTES-DO-NOT-SEARCH"),
            PLACES,
            settings,
            usage,
            trace,
            transport=transport,
        )
    return result, usage, trace


async def test_found_facts_bind_citations_and_parallel_repeated_search_reuses_one_request():
    model_calls, search_calls = [], []

    async def provider(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/tools/search"):
            search_calls.append(body)
            # The two tool calls run concurrently, so a plain completed-result cache is insufficient.
            await asyncio.sleep(0.01)
            return sources()
        model_calls.append(body)
        if len(model_calls) == 1:
            return completion([("search_place_information", {"place_id": "p0", "day": 1})] * 2)
        return final(
            body,
            [
                fact(),
                fact(
                    field="last_admission",
                    value="16:00停止入场。",
                    windows=[],
                    last_admission="16:00",
                ),
            ],
        )

    shared = RunUsage(requests=1, input_tokens=10, output_tokens=1)
    result, usage, trace = await run_research(
        provider, [need(fields=["opening_hours", "last_admission"])], usage=shared
    )
    assert result["outcome"] == "complete" and not result["issues"]
    assert result["search_calls"] == len(search_calls) == 1
    assert usage is shared and usage.requests == 3 and usage.input_tokens == 94
    assert all(item["evidence_status"] == "web_reported" for item in result["facts"])
    assert result["facts"][0]["sources"] == [
        {"title": "指定日期参观说明", "url": "https://museum.example/visit"}
    ]
    assert result["facts"][0]["date"] == "2026-11-21"
    assert result["facts"][0]["queried_at"]
    assert result["facts"][1]["last_admission"] == "16:00"
    assert "2026-11-21" in search_calls[0]["query"]
    assert "测试馆人民广场馆" in search_calls[0]["query"]
    assert "PRIVATE-NOTES" not in json.dumps(search_calls)
    assert json.loads(model_calls[0]["messages"][-1]["content"])["tasks"][0]["fields"] == [
        "opening_hours",
        "last_admission",
    ]
    assert trace[-1]["event"] == "research_complete"
    assert [event for event in trace if event["event"] == "research_search_attempt"] == [
        {"event": "research_search_attempt", "place_id": "p0", "day": 1}
    ]


@pytest.mark.parametrize(
    "changed",
    [
        {"source_ids": [2]},
        {"quote": "这是模型编写的未出现文字。"},
        {"windows": [{"day": 1, "start_time": "10:00", "end_time": "17:00"}]},
        {"windows": []},
    ],
)
async def test_unbound_sources_or_unquoted_time_never_complete_a_critical_time_gap(changed):
    calls = []

    def provider(request):
        if request.url.path.endswith("/tools/search"):
            return sources()
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return completion([("search_place_information", {"place_id": "p0", "day": 1})])
        return final(body, [fact(**changed)])

    result, _, _ = await run_research(provider)
    assert result["outcome"] == "needs_information"
    assert result["issues"][0]["missing_fields"] == ["opening_hours"]
    assert not result["facts"][0]["windows"]
    if "source_ids" in changed or "quote" in changed:
        assert result["facts"][0]["status"] == "unknown"
        assert result["facts"][0]["sources"] == []
    else:
        assert result["facts"][0]["status"] == "found"
        assert result["facts"][0]["evidence_status"] == "web_reported"


@pytest.mark.parametrize("report_mode", ["missing", "duplicate", "unrequested", "no_search"])
async def test_missing_conflicting_or_unsourced_report_cannot_inject_a_fact(report_mode):
    calls = []

    def provider(request):
        if request.url.path.endswith("/tools/search"):
            return sources()
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1 and report_mode != "no_search":
            return completion([("search_place_information", {"place_id": "p0", "day": 1})])
        reports = {
            "missing": [],
            "duplicate": [
                fact(),
                {"place_id": "p0", "day": 1, "field": "opening_hours", "status": "unknown"},
            ],
            "unrequested": [fact(place_id="p1")],
            "no_search": [fact()],
        }
        return final(body, reports[report_mode])

    result, _, _ = await run_research(provider)
    assert result["outcome"] == "needs_information"
    assert len(result["facts"]) == 1
    assert result["facts"][0]["place_id"] == "p0"
    assert result["facts"][0]["status"] == "unknown"


async def test_source_ids_are_scoped_to_each_place_and_date():
    calls = []

    def provider(request):
        if request.url.path.endswith("/tools/search"):
            query = json.loads(request.content)["query"]
            return sources("另馆的资料不包含这些开放时刻。" if "另一处测试馆" in query else QUOTE)
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return completion(
                [("search_place_information", {"place_id": pid, "day": 1}) for pid in ("p0", "p1")]
            )
        return final(body, [fact(), fact(place_id="p1")])

    result, _, _ = await run_research(provider, [need(), need(place_id="p1")])
    assert result["search_calls"] == 2
    assert [item["status"] for item in result["facts"]] == ["found", "unknown"]


async def test_quote_explicitly_about_another_date_is_not_bound_to_the_requested_date():
    calls = []
    wrong_date_quote = QUOTE.replace("2026-11-21", "2026-11-20")

    def provider(request):
        if request.url.path.endswith("/tools/search"):
            return sources(wrong_date_quote)
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return completion([("search_place_information", {"place_id": "p0", "day": 1})])
        return final(body, [fact(quote=wrong_date_quote)])

    result, _, _ = await run_research(provider)
    assert result["outcome"] == "needs_information"
    assert result["facts"][0]["date"] == "2026-11-21"
    assert result["facts"][0]["status"] == "unknown"


async def test_cancelled_search_leaves_an_attempt_without_private_query_or_credentials():
    trace = []
    search_started = asyncio.Event()

    async def provider(request):
        if request.url.path.endswith("/tools/search"):
            search_started.set()
            await asyncio.Future()
        return completion([("search_place_information", {"place_id": "p0", "day": 1})])

    transport = httpx2.MockTransport(provider)
    async with AsyncOpenAI(
        api_key="sdk-test-key",
        base_url="https://api.deepseek.com",
        http_client=httpx2.AsyncClient(transport=transport),
        max_retries=0,
    ) as sdk:
        model = OpenAIChatModel(
            "deepseek-flash",
            provider=OpenAIProvider(openai_client=sdk),
            profile=OpenAIModelProfile(
                openai_supports_strict_tool_definition=False,
                supports_forced_tool_choice=False,
            ),
        )
        task = asyncio.create_task(
            research_missing_information(
                model,
                [need()],
                Requirements(date="2026-11-21"),
                PLACES,
                configured(),
                RunUsage(),
                trace,
                transport=transport,
            )
        )
        await asyncio.wait_for(search_started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert [event for event in trace if event["event"] == "research_search_attempt"] == [
        {"event": "research_search_attempt", "place_id": "p0", "day": 1}
    ]


@pytest.mark.parametrize("kind", ["empty", "http", "format", "bad_sources"])
async def test_empty_search_and_provider_failures_have_distinct_outcomes(kind):
    calls = []

    def provider(request):
        if request.url.path.endswith("/tools/search"):
            return {
                "empty": httpx2.Response(200, json={"results": []}),
                "http": httpx2.Response(429, text="SENSITIVE-PROVIDER-BODY"),
                "format": httpx2.Response(200, text="not JSON"),
                "bad_sources": httpx2.Response(
                    200,
                    json={"results": [{"url": "javascript:bad", "title": "bad", "summary": "bad"}]},
                ),
            }[kind]
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return completion([("search_place_information", {"place_id": "p0", "day": 1})])
        return final(body, [])

    result, _, _ = await run_research(provider)
    assert result["outcome"] == ("needs_information" if kind == "empty" else "service_error")
    assert result["search_calls"] == 1
    assert result["facts"][0]["status"] == "unknown"
    assert "SENSITIVE-PROVIDER-BODY" not in json.dumps(result)


@pytest.mark.parametrize("attempt", [{"place_id": "p1", "day": 1}, {"place_id": "p0", "day": 2}])
async def test_search_tool_rejects_known_but_unrequested_place_or_date(attempt):
    calls, searches = [], []

    def provider(request):
        if request.url.path.endswith("/tools/search"):
            searches.append(json.loads(request.content)["query"])
            return sources()
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return completion([("search_place_information", attempt)])
        if len(calls) == 2:
            assert "只能查询本次补查任务" in body["messages"][-1]["content"]
            return completion([("search_place_information", {"place_id": "p0", "day": 1})])
        return final(body, [fact()])

    result, usage, _ = await run_research(provider)
    assert result["outcome"] == "complete"
    assert result["search_calls"] == len(searches) == 1
    assert usage.requests == 3
    assert "2026-11-21" in searches[0]


async def test_budget_exhaustion_is_incomplete_without_resetting_shared_usage():
    def forbidden(request):
        raise AssertionError("No provider request is allowed once the shared budget is exhausted")

    result, usage, _ = await run_research(forbidden, usage=RunUsage(requests=6))
    assert result["outcome"] == "incomplete"
    assert usage.requests == 6 and result["search_calls"] == 0
    assert "预算" in result["error"]


@pytest.mark.parametrize(
    "changes", [{"base_url": "https://compatible.example/v1"}, {"api_key": None}]
)
async def test_unavailable_official_search_does_not_send_credentials_or_call_model(changes):
    def forbidden(request):
        raise AssertionError("Do not send any credentials or model request")

    result, usage, _ = await run_research(
        forbidden, settings=configured().model_copy(update=changes)
    )
    assert result["outcome"] == "needs_information"
    assert usage.requests == 0 and result["search_calls"] == 0
