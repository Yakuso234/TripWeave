"""Collaborative recommendations use simulated provider responses only."""

import json

import httpx2
import pytest
from test_real_planner import configured

import tripweave.attraction_picker as picker
from tripweave.domain import Requirements
from tripweave.settings import Settings


def attraction(index):
    return {
        "id": f"p{index}",
        "name": f"景点{index}",
        "type": "风景名胜",
        "address": "测试地址",
        "area": "测试区",
        "rating": 4.8 if index == 0 else None,
        "photos": [f"https://photos.example/{index}/{photo}.jpg" for photo in range(3)],
    }


async def test_discovery_combines_custom_interests_and_popular_places_without_duplicates():
    class Maps:
        def __init__(self):
            self.queries = []

        async def search(self, keyword, city, types, limit, *, detailed):
            self.queries.append((keyword, city, types, limit, detailed))
            # Different theme searches can return the same place. Keep its identity once.
            return [attraction(0)] + [attraction(100 * len(self.queries) + i) for i in range(19)]

    maps = Maps()
    req = Requirements(
        date="2026-11-10",
        city="上海",
        interests="历史人文、自然公园",
        custom_interests="现代艺术",
    )
    result = await picker.discover_candidates(req, maps)
    keywords = {query[0] for query in maps.queries}
    assert {"现代艺术", "博物馆", "公园", "著名景点"} <= keywords
    assert all(query[1] == "上海" and query[4] for query in maps.queries)
    ids = [place["id"] for place in result]
    assert len(ids) == len(set(ids)) <= 60
    # A large theme group cannot crowd the general popular-attraction query out of the pool.
    assert "p400" in ids


async def test_ranking_uses_preferences_and_real_ratings_but_rejects_unknown_and_duplicate_ids():
    req = Requirements(
        date="2026-11-10",
        interests="历史人文",
        custom_interests="古代建筑",
        pace="relaxed",
        custom_pace="中午休息",
        notes="同行者更喜欢建筑，减少登高",
    )
    places = [attraction(i) for i in range(3)]

    def respond(request):
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        context = json.loads(body["messages"][1]["content"])
        assert context["interests"] == req.interests
        assert context["custom_interests"] == req.custom_interests
        assert context["custom_pace"] == req.custom_pace
        assert context["additional_requirements"] == req.notes
        assert context["places"][0]["rating"] == 4.8
        assert context["places"][1]["rating"] is None
        assert all("photos" not in place for place in context["places"])
        result = {
            "attractions": [
                {"place_id": "p2", "reason": "适合建筑兴趣", "experience_group": "p2"},
                {"place_id": "invented", "reason": "模型编造地点", "experience_group": "p0"},
                {"place_id": "p2", "reason": "重复选择", "experience_group": "p2"},
                {"place_id": "p0", "reason": "城市代表景点", "experience_group": "p0"},
                {"place_id": "p1", "reason": "主体景点的组成部分", "experience_group": " p2 "},
            ]
        }
        return httpx2.Response(
            200, json={"choices": [{"message": {"content": json.dumps(result)}}]}
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        ordered, notice = await picker.rank_candidates(req, places, configured(), client)
    assert notice is None
    assert [place["id"] for place in ordered] == ["p2", "p0"]
    assert ordered[0]["recommendation_reason"] == "适合建筑兴趣"
    assert ordered[0]["photos"] == places[2]["photos"]
    assert set(ordered[0]["experience_members"]) == {"p1", "p2"}
    assert "recommendation_reason" not in places[2]


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(503),
        httpx2.Response(200, json={"choices": []}),
        httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "attractions": [
                                        {
                                            "place_id": "invented",
                                            "reason": "未知地点",
                                            "experience_group": "p0",
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        ),
    ],
)
async def test_ranking_failure_is_explicit_without_returning_raw_map_candidates(response):
    places = [attraction(0), attraction(1)]
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda request: response)
    ) as client:
        with pytest.raises(picker.AttractionRankingError):
            await picker.rank_candidates(
                Requirements(date="2026-11-10"), places, configured(), client
            )


@pytest.mark.parametrize("experience_group", [None, "", " \t ", "unknown-subject"])
async def test_ranking_rejects_missing_blank_or_unverifiable_experience_group(experience_group):
    item = {"place_id": "p0", "reason": "具有代表性的景点"}
    if experience_group is not None:
        item["experience_group"] = experience_group

    def respond(request):
        content = json.dumps({"attractions": [item]})
        return httpx2.Response(200, json={"choices": [{"message": {"content": content}}]})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        with pytest.raises(picker.AttractionRankingError):
            await picker.rank_candidates(
                Requirements(date="2026-11-10"), [attraction(0)], configured(), client
            )


@pytest.mark.parametrize(
    "settings",
    [
        Settings(_env_file=None, api_key=""),
        Settings(_env_file=None, api_key="test-only", base_url="https://other.example"),
    ],
)
async def test_ranking_requires_supported_model_configuration(settings):
    def unexpected_request(request):
        raise AssertionError("A missing or unsupported provider must not be called")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(unexpected_request)) as client:
        with pytest.raises(picker.AttractionRankingError):
            await picker.rank_candidates(
                Requirements(date="2026-11-10"), [attraction(0)], settings, client
            )


async def test_experience_groups_merge_transitively_but_keep_separate_museum_branches():
    places = [attraction(i) for i in range(5)]
    places[0]["name"], places[1]["name"], places[2]["name"] = "豫园", "豫园古建筑群", "豫园园林"
    places[3]["name"], places[4]["name"] = "上海博物馆人民广场馆", "上海博物馆东馆"
    items = [
        {"place_id": "p0", "reason": "偏好最匹配", "experience_group": "p1"},
        {"place_id": "p3", "reason": "人民广场馆", "experience_group": "p3"},
        {"place_id": "p1", "reason": "古建筑与园林", "experience_group": "p2"},
        {"place_id": "p2", "reason": "同一园林体验", "experience_group": "p2"},
        {"place_id": "p4", "reason": "另一个独立馆区", "experience_group": "p4"},
    ]

    def respond(request):
        content = json.dumps({"attractions": items})
        return httpx2.Response(200, json={"choices": [{"message": {"content": content}}]})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        ordered, notice = await picker.rank_candidates(
            Requirements(date="2026-11-10"), places, configured(), client
        )
    assert notice is None
    assert [place["id"] for place in ordered] == ["p0", "p3", "p4"]
    assert set(ordered[0]["experience_members"]) == {"p0", "p1", "p2"}
    assert ordered[0]["recommendation_reason"] == "偏好最匹配"
    assert set(ordered[1]["experience_members"]) == {"p3"}
    assert set(ordered[2]["experience_members"]) == {"p4"}


async def test_ranking_rejects_output_that_omits_user_selected_place_grouping():
    req = Requirements(
        date="2026-11-10", selected_attractions=[{"id": "chosen", "name": "用户已选的馆区"}]
    )

    def respond(request):
        context = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert any(place["id"] == "chosen" for place in context["places"])
        content = json.dumps(
            {"attractions": [{"place_id": "p0", "reason": "新候选", "experience_group": "p0"}]}
        )
        return httpx2.Response(200, json={"choices": [{"message": {"content": content}}]})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        with pytest.raises(picker.AttractionRankingError):
            await picker.rank_candidates(
                req,
                [attraction(0), {"id": "chosen", "name": "用户已选的馆区"}],
                configured(),
                client,
            )


async def test_batch_skips_same_experience_as_user_selected_place_without_replacing_selection(
    monkeypatch,
):
    selected = [{"id": "chosen", "name": "用户已选的馆区"}]
    pool = [attraction(i) for i in range(7)]
    for place in pool:
        place["experience_group"] = place["id"]
        place["experience_members"] = [place["id"]]
    pool[0]["experience_members"].append("chosen")
    researched = []

    async def research(req, options, settings, client):
        researched.extend(place["id"] for place in options)

    monkeypatch.setattr(picker, "research_attractions", research)
    req = Requirements(date="2026-11-10", selected_attractions=selected)
    draft = {"candidate_pool": pool, "selected_ids": ["chosen"], "selected_items": selected}
    async with httpx2.AsyncClient() as client:
        result = await picker.make_batch(req, draft, configured(), client)
    assert [place["id"] for place in result["options"]] == [f"p{i}" for i in range(1, 6)]
    assert result["remaining_count"] == 1
    assert researched == [f"p{i}" for i in range(1, 6)]
    assert result["selected_ids"] == ["chosen"]
    assert result["selected_items"] == selected


async def test_restored_seen_experience_cannot_be_reshown_under_another_poi_id(monkeypatch):
    pool = [attraction(i) for i in range(8)]
    for place in pool:
        place["experience_group"] = place["id"]
    pool[0]["experience_group"] = "already-shown-parent"
    researched = []

    async def research(req, options, settings, client):
        researched.append([place["id"] for place in options])

    monkeypatch.setattr(picker, "research_attractions", research)
    restored = json.loads(
        json.dumps(
            {
                "candidate_pool": pool,
                "selected_ids": [],
                "seen_ids": ["already-shown-parent"],
                "seen_groups": ["already-shown-parent"],
            }
        )
    )
    async with httpx2.AsyncClient() as client:
        first = await picker.make_batch(
            Requirements(date="2026-11-10"), restored, configured(), client
        )
        second = await picker.make_batch(
            Requirements(date="2026-11-10"), json.loads(json.dumps(first)), configured(), client
        )
    assert researched == [[f"p{i}" for i in range(1, 6)], ["p6", "p7"]]
    assert second["exhausted"] and second["remaining_count"] == 0
    assert "p0" not in second["seen_ids"]
    assert "already-shown-parent" in second["seen_groups"]


async def test_batches_do_not_repeat_and_only_research_newly_shown_places(monkeypatch):
    researched = []

    async def research(req, options, settings, client):
        researched.append([place["id"] for place in options])
        for place in options:
            place["web_summary"] = f"{place['name']}的主要游玩内容"
            place["audience"] = "喜欢慢游的人"

    monkeypatch.setattr(picker, "research_attractions", research)
    original_pool = [attraction(i) for i in range(12)]
    draft = {"candidate_pool": original_pool, "selected_ids": []}
    batches = []
    async with httpx2.AsyncClient() as client:
        for expected_count, remaining in [(5, 7), (5, 2), (2, 0)]:
            draft = await picker.make_batch(
                Requirements(date="2026-11-10"), draft, configured(), client
            )
            batches.append([place["id"] for place in draft["options"]])
            assert len(draft["options"]) == expected_count
            assert draft["remaining_count"] == remaining
            assert draft["exhausted"] == (remaining == 0)
            if len(batches) == 1:
                draft["selected_ids"] = ["p0"]
            assert draft["selected_ids"] == ["p0"]
            assert all(place["web_summary"] and place["audience"] for place in draft["options"])
        assert draft["batch_number"] == 3
        assert len(set().union(*map(set, batches))) == 12
        assert draft["seen_ids"] == [place["id"] for place in original_pool]
        assert all("web_summary" in place for place in draft["candidate_pool"])
        assert all("web_summary" not in place for place in original_pool)
        assert researched == batches
