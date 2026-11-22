"""Picker evidence reaches the real planning graph through simulated HTTP only."""

import json

import httpx2
import pytest
from draft_helpers import packet, phase_packet
from review_mock import with_approved_review
from test_real_planner import configured, handler

from tripweave.domain import Requirements
from tripweave.evidence import freeze_evidence
from tripweave.real_planner import run_plan

SELECTED_SUMMARY = "沿常设展陈了解城市历史，适合留出休息时间慢游。"
UNSELECTED_SUMMARY = "UNSELECTED-SUMMARY-MUST-NOT-ENTER-MODEL"
RAW_EXCERPT = "RAW-WEB-EXCERPT-MUST-NOT-ENTER-MODEL"
PHOTO_URL = "https://images.example/selected-photo.jpg"


def map_response(request):
    """Supply the provider city identity required by collaborative POI checks."""
    assert request.url.host == "restapi.amap.com"
    response = handler(request)
    body = response.json()
    for place in body.get("pois", []):
        place.update(citycode="021", cityname="上海市", photos=[{"url": PHOTO_URL}])
    return httpx2.Response(200, json=body)


def requirements(**overrides):
    return Requirements(
        date="2026-11-21",
        selected_attractions=[{"id": "p0", "name": "接口景点0"}],
        max_activities=1,
        **overrides,
    )


def evidence(*, alias="p7", group_id="p0"):
    """Freeze a server draft containing both selected and unrelated rich data."""
    timestamp = "2026-11-13T10:00:00+08:00"
    draft = {
        "picker_version": 3,
        "selection_revision": 4,
        "queried_at": timestamp,
        "selected_ids": ["p0"],
        "candidate_pool": [
            {
                "id": "p0",
                "name": "接口景点0",
                "experience_group": group_id,
                "experience_members": list(dict.fromkeys([group_id, "p0", alias])),
                "web_summary": SELECTED_SUMMARY,
                "audience": "喜欢城市历史、希望慢游的人",
                "recommendation_reason": "适合本次人文兴趣",
                "photos": [PHOTO_URL],
                "web_sources": [
                    {
                        "title": "场馆常设展介绍",
                        "url": "https://museum.example/overview",
                        "excerpt": RAW_EXCERPT,
                    }
                ],
            },
            {
                "id": "p2",
                "name": "接口景点2",
                "experience_group": "p2",
                "experience_members": ["p2"],
                "web_summary": UNSELECTED_SUMMARY,
                "web_sources": [{"title": "其他景点", "url": "https://other.example/info"}],
            },
        ],
    }
    return freeze_evidence(
        draft, city="上海", source_requirements_revision=1, requirements_revision=2
    )


def completion(name, arguments, number):
    return httpx2.Response(
        200,
        json={
            "id": f"context-completion-{number}",
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
                                "id": f"context-call-{number}",
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)},
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 42, "completion_tokens": 20, "total_tokens": 62},
        },
    )


def output_tool(body):
    ordinary = {"lookup_places", "route", "nearby_restaurants", "find_place"}
    return next(
        tool["function"]["name"]
        for tool in body["tools"]
        if tool["function"]["name"] not in ordinary
    )


async def seed_plan(req):
    seed = await run_plan(req, "basic", configured(), transport=httpx2.MockTransport(map_response))
    assert seed["status"] == "ready", seed["issues"]
    plan = seed["itinerary"]
    # Avoid an unrelated restaurant lookup in tests about evidence handoff.
    for stop in plan["stops"]:
        if stop["kind"] == "meal":
            stop["place_id"] = "o"
    return plan


@pytest.mark.parametrize("has_previous", [False, True])
async def test_selected_research_enters_initial_context_once_without_bulk_picker_material(
    has_previous,
):
    req = requirements()
    plan = await seed_plan(req)
    snapshot = evidence()
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        assert request.url.path.endswith("/chat/completions")  # No research/search request.
        body = json.loads(request.content)
        calls.append(body)
        assert len(calls) <= 2
        return completion(
            "draft_proposal", packet(plan, queries=None if len(calls) == 1 else []), len(calls)
        )

    result = await run_plan(
        req,
        "deepseek",
        configured(),
        previous_plan={
            "itinerary": plan,
            "places": [{"photos": [PHOTO_URL], "excerpt": RAW_EXCERPT}],
            "research_evidence": {"old_summary": UNSELECTED_SUMMARY},
        }
        if has_previous
        else None,
        transport=httpx2.MockTransport(with_approved_review(provider)),
        research_context=snapshot,
    )
    assert result["status"] == "ready", (result["error"], result["issues"])
    assert result["usage"]["requests"] == len(calls) + 1 == 3
    initial = json.loads(calls[0]["messages"][-1]["content"])
    queried = initial["queried_data"]
    research_packet = queried["selected_attraction_research"]
    assert research_packet["source_requirements_revision"] == 1
    assert research_packet["requirements_revision"] == 2
    assert research_packet["selection_revision"] == 4
    assert research_packet["selected"] == [{**snapshot["selected"][0], "experience_group": "p0"}]
    assert research_packet["selected"][0]["summary"] == SELECTED_SUMMARY
    assert research_packet["selected"][0]["sources"] == [
        {"title": "场馆常设展介绍", "url": "https://museum.example/overview"}
    ]
    assert "experience_groups" not in queried and "experience_groups" not in research_packet
    encoded = json.dumps(research_packet, ensure_ascii=False)
    assert "photos" not in encoded and "excerpt" not in encoded
    for body in calls:
        sent = json.dumps(body["messages"], ensure_ascii=False)
        assert sent.count(SELECTED_SUMMARY) == 1
        assert all(value not in sent for value in (PHOTO_URL, RAW_EXCERPT, UNSELECTED_SUMMARY))
    resumed = json.loads(calls[1]["messages"][-1]["content"])
    assert resumed["evidence_results"]
    assert all(message["role"] in {"system", "user"} for message in calls[1]["messages"])
    assert sum(event["event"] == "research_context" for event in result["trace"]) == 1


@pytest.mark.parametrize("mismatch", ["place_id", "name", "city"])
async def test_research_for_a_different_selected_identity_or_city_is_not_given_to_model(mismatch):
    req = requirements()
    plan = await seed_plan(req)
    snapshot = evidence()
    if mismatch == "place_id":
        snapshot["selected"][0].update(place_id="p2", name="接口景点2")
    elif mismatch == "name":
        snapshot["selected"][0]["name"] = "同ID但不是用户确认的名称"
    else:
        snapshot["city"] = "杭州"
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        assert request.url.path.endswith("/chat/completions")
        body = json.loads(request.content)
        calls.append(body)
        return completion(
            "draft_proposal", packet(plan, queries=None if len(calls) == 1 else []), len(calls)
        )

    result = await run_plan(
        req,
        "deepseek",
        configured(),
        transport=httpx2.MockTransport(with_approved_review(provider)),
        research_context=snapshot,
    )
    assert result["status"] == "ready", result["issues"]
    assert len(calls) == 2
    queried = json.loads(calls[0]["messages"][-1]["content"])["queried_data"]
    assert "selected_attraction_research" not in queried and "experience_groups" not in queried
    assert SELECTED_SUMMARY not in json.dumps(calls, ensure_ascii=False)
    assert not any(event["event"] == "research_context" for event in result["trace"])


async def test_local_revision_reuses_research_history_without_searching_again():
    req = requirements(days=2)
    plan = await seed_plan(req)
    broken = json.loads(json.dumps(plan))
    broken["stops"][0]["start_time"] = "00:00"
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        assert request.url.path.endswith("/chat/completions")
        body = json.loads(request.content)
        calls.append(body)
        assert {t["function"]["name"] for t in body["tools"]} == {"draft_proposal"}
        return completion(
            "draft_proposal",
            phase_packet(
                json.loads(body["messages"][-1]["content"]),
                plan if len(calls) >= 3 else broken,
                queries=None if len(calls) == 1 else [],
            ),
            len(calls),
        )

    result = await run_plan(
        req,
        "deepseek",
        configured(),
        transport=httpx2.MockTransport(with_approved_review(provider)),
        research_context=evidence(),
    )
    assert result["status"] == "ready", (result["error"], result["issues"])
    assert result["itinerary"] == plan
    assert result["usage"]["requests"] == len(calls) + 1 == 4
    repair = json.loads(calls[2]["messages"][-1]["content"])
    assert repair["revision_days"] == [1]
    assert [s for s in repair["draft"]["stops"] if s["day"] == 2] == [
        s for s in plan["stops"] if s["day"] == 2
    ]
    assert [i["code"] for i in repair["validation_issues"]] == ["connection"]
    messages = json.dumps(calls[2]["messages"], ensure_ascii=False)
    assert messages.count(SELECTED_SUMMARY) == 1
    assert all(m["role"] in {"system", "user"} for m in calls[2]["messages"])
    assert sum(e["event"] == "research_context" for e in result["trace"]) == 1


def overnight_plan(first_id="p0", second_id="p1", *, booking=False):
    return {
        "summary": "用于验证游玩体验去重的模拟模型输出",
        "hotel_id": "h",
        "stops": [
            {
                "day": 1,
                "place_id": first_id,
                "kind": "activity",
                "start_time": "10:00",
                "end_time": "11:00",
                "reason": "游览",
            },
            {
                "day": 1,
                "place_id": "h",
                "kind": "hotel",
                "start_time": "18:00",
                "end_time": "18:15",
                "reason": "过夜当天返回酒店",
            },
            {
                "day": 2,
                "place_id": second_id,
                "kind": "booking" if booking else "activity",
                "start_time": "14:00",
                "end_time": "15:00",
                "reason": "用户固定预约" if booking else "游览",
            },
        ],
    }


async def persistent_conflict(req, plan, snapshot):
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        assert request.url.path.endswith("/chat/completions")
        body = json.loads(request.content)
        calls.append(body)
        return completion(
            "draft_proposal", packet(plan, queries=None if len(calls) == 1 else []), len(calls)
        )

    result = await run_plan(
        req,
        "deepseek",
        configured(),
        transport=httpx2.MockTransport(with_approved_review(provider)),
        research_context=snapshot,
    )
    assert result["status"] == "conflict" and result["itinerary"] is None
    assert result["outcome"] == "incomplete"
    assert result["usage"]["requests"] == len(calls) <= 6
    assert len(calls) >= 3
    return result, calls


async def test_different_poi_ids_for_one_experience_report_both_days_and_keep_selected_identity():
    result, calls = await persistent_conflict(
        requirements(days=2), overnight_plan(), evidence(alias="p1")
    )
    assert [issue["code"] for issue in result["issues"]] == ["duplicate"]
    duplicate = result["issues"][0]
    assert duplicate["experience_group"] == "p0"
    assert duplicate["related_days"] == [1, 2]
    assert [
        (item["place_id"], item["day"], item["stop_number"]) for item in duplicate["occurrences"]
    ] == [("p0", 1, 1), ("p1", 2, 1)]
    assert "keep_day" not in duplicate
    feedback = json.loads(calls[2]["messages"][-1]["content"])
    assert feedback["revision_days"] == [1, 2]
    assert feedback["validation_issues"][0] == duplicate


async def test_alias_alone_does_not_satisfy_user_selected_original_poi():
    result, calls = await persistent_conflict(
        requirements(days=2),
        overnight_plan(first_id="p1", second_id="p2"),
        evidence(alias="p1"),
    )
    assert [issue["code"] for issue in result["issues"]] == ["must_visit"]
    assert "接口景点0" in result["issues"][0]["message"]
    feedback = json.loads(calls[2]["messages"][-1]["content"])
    assert "draft" in feedback  # A missing mandatory identity remains a global repair.
    assert "revision_days" not in feedback


async def test_same_experience_duplicate_preserves_the_matching_user_booking():
    result, calls = await persistent_conflict(
        requirements(days=2, booking_query="上海博物馆", booking_day=2),
        overnight_plan(second_id="b", booking=True),
        evidence(alias="b", group_id="b"),
    )
    assert [issue["code"] for issue in result["issues"]] == ["duplicate"]
    duplicate = result["issues"][0]
    assert duplicate["experience_group"] == "b"
    assert duplicate["day"] == duplicate["stop_number"] == 1
    assert duplicate["keep_day"] == 2 and duplicate["keep_stop_number"] == 1
    assert duplicate["keep_kind"] == "booking"
    feedback = json.loads(calls[2]["messages"][-1]["content"])
    assert feedback["revision_days"] == [1]
    assert next(stop for stop in feedback["draft"]["stops"] if stop["day"] == 2)["place_id"] == "b"
