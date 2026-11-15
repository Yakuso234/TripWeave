import json

import httpx2
import pytest
from test_real_planner import configured

from tripweave.attraction_research import research_attractions
from tripweave.domain import Requirements


async def test_recommendations_cite_only_real_sources_for_the_same_attraction():
    calls = []

    def respond(request):
        calls.append(request.url.path)
        body = json.loads(request.content)
        if request.url.path.endswith("/tools/search"):
            assert "私人备注" not in body["query"]
            assert "游玩特色" in body["query"] and "适合人群" in body["query"]
            assert "预约" not in body["query"]
            return httpx2.Response(
                200,
                json={
                    "results": [
                        {"title": "错误链接", "url": "javascript:alert(1)", "summary": "无效"},
                        {
                            "title": "场馆介绍",
                            "url": "https://museum.example/visit",
                            "summary": "展示城市历史，提供常设展览。",
                        },
                    ]
                },
            )
        result = {
            "recommendations": [
                {
                    "place_id": "p0",
                    "summary": "通过常设展览了解城市历史。",
                    "audience": "喜欢历史人文、希望室内慢游的人。",
                    "source_ids": [1],
                },
                {"place_id": "p1", "summary": "模型自行填的来源。", "source_ids": [2]},
                {
                    "place_id": "p4",
                    "summary": "适合看展与慢游。",
                    "audience": "喜欢展览的人",
                    "source_ids": [1],
                },
            ]
        }
        return httpx2.Response(
            200, json={"choices": [{"message": {"content": json.dumps(result)}}]}
        )

    options = [{"id": f"p{i}", "name": f"景点{i}", "address": "", "area": ""} for i in range(6)]
    req = Requirements(date="2026-11-10", notes="私人备注")
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        assert "有 3 个景点介绍或人群资料不足" in await research_attractions(
            req, options, configured(), client
        )
    assert calls.count("/v1/tools/search") == 5
    assert calls.count("/v1/chat/completions") == 1
    assert options[0]["web_summary"] == "通过常设展览了解城市历史。"
    assert options[0]["audience"] == "喜欢历史人文、希望室内慢游的人。"
    assert [source["url"] for source in options[0]["web_sources"]] == [
        "https://museum.example/visit"
    ]
    assert "web_summary" not in options[1]
    assert options[4]["web_summary"] == "适合看展与慢游。"
    assert "web_sources" not in options[5]


@pytest.mark.parametrize(
    "recommendations",
    [
        [{"place_id": "p0", "summary": " \t\n ", "audience": "喜欢历史的人", "source_ids": [1]}],
        [{"place_id": "p0", "audience": "喜欢历史的人", "source_ids": [1]}],
        [],
        [{"place_id": "unknown", "summary": "模型返回了别的景点", "source_ids": [1]}],
        [{"place_id": "p0", "summary": "没有可对应的来源", "source_ids": [2]}],
    ],
)
async def test_missing_or_blank_introduction_never_looks_like_success(recommendations):
    def respond(request):
        if request.url.path.endswith("/tools/search"):
            return httpx2.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "场馆介绍",
                            "url": "https://museum.example/intro",
                            "summary": "了解城市历史和建筑。",
                        }
                    ]
                },
            )
        content = json.dumps({"recommendations": recommendations})
        return httpx2.Response(200, json={"choices": [{"message": {"content": content}}]})

    options = [{"id": "p0", "name": "测试馆", "address": "测试地址", "area": "测试区"}]
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        notice = await research_attractions(
            Requirements(date="2026-11-10"), options, configured(), client
        )
    assert notice
    assert not options[0].get("web_summary", "").strip()
    assert options[0]["web_sources"][0]["url"] == "https://museum.example/intro"


@pytest.mark.parametrize("audience", [None, "", " \t\n "])
async def test_missing_audience_keeps_intro_but_explicitly_reports_incomplete_research(audience):
    def respond(request):
        if request.url.path.endswith("/tools/search"):
            return httpx2.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "场馆介绍",
                            "url": "https://museum.example/intro",
                            "summary": "了解城市历史和建筑。",
                        }
                    ]
                },
            )
        recommendation = {"place_id": "p0", "summary": " 了解城市历史和建筑。 ", "source_ids": [1]}
        if audience is not None:
            recommendation["audience"] = audience
        content = json.dumps({"recommendations": [recommendation]})
        return httpx2.Response(200, json={"choices": [{"message": {"content": content}}]})

    options = [{"id": "p0", "name": "测试馆", "address": "测试地址", "area": "测试区"}]
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        notice = await research_attractions(
            Requirements(date="2026-11-10"), options, configured(), client
        )
    assert "有 1 个景点介绍或人群资料不足" in notice
    assert options[0]["web_summary"] == "了解城市历史和建筑。"
    assert not options[0].get("audience", "").strip()
