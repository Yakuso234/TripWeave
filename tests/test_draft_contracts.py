"""Draft-first query contracts and pure checks, without provider calls."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from tripweave.domain import Itinerary, Requirements, Stop
from tripweave.draft_contracts import (
    QUERY_ADAPTER,
    DraftProposal,
    PlaceQuery,
    RestaurantQuery,
    RouteQuery,
    canonical_query,
    check_draft_identity,
    check_query_requests,
    packet_context,
)


def requirements(**changes):
    return Requirements.model_validate({"date": "2026-11-21", "nights": 0, **changes})


def draft(**changes):
    return Itinerary.model_validate(
        {
            "summary": "暂定安排，开放和路线尚待核实。",
            "stops": [
                {
                    "day": 1,
                    "place_id": "museum",
                    "start_time": "10:00",
                    "end_time": "12:00",
                    "kind": "activity",
                    "reason": "暂定参观时段，不表示已核实开放。",
                }
            ],
            **changes,
        }
    )


def route(**changes):
    return RouteQuery.model_validate(
        {
            "kind": "route",
            "origin_id": "origin",
            "destination_id": "museum",
            "transport": "mixed",
            "reason": "核对首段路程是否能赶上拟定入馆时间。",
            **changes,
        }
    )


def proposal(**changes):
    return {
        "draft": draft().model_dump(),
        "query_requests": [],
        "information_needs": [],
        "assumptions": [],
        **changes,
    }


@pytest.mark.parametrize("missing", ["draft", "query_requests", "information_needs", "assumptions"])
def test_proposal_requires_explicit_draft_and_all_lists(missing):
    body = proposal()
    body.pop(missing)
    with pytest.raises(ValidationError):
        DraftProposal.model_validate(body)


@pytest.mark.parametrize(
    "query_payload",
    [
        {
            "kind": "route",
            "origin_id": "origin",
            "destination_id": "museum",
            "transport": "mixed",
            "reason": "验证首段",
        },
        {"kind": "nearby_restaurants", "place_id": "museum", "reason": "补充午餐候选"},
        {"kind": "place_search", "query": "用户明确提出的公园", "reason": "原方案修订要求新增地点"},
    ],
)
def test_query_discriminator_selects_only_supported_request_type(query_payload):
    parsed = DraftProposal.model_validate(proposal(query_requests=[query_payload]))
    assert parsed.query_requests[0].kind == query_payload["kind"]


@pytest.mark.parametrize(
    "query_payload",
    [
        {
            "kind": "route",
            "origin_id": "origin",
            "destination_id": "museum",
            "transport": "mixed",
            "reason": "验证",
            "url": "https://other.example/api",
        },
        {
            "kind": "route",
            "origin_id": "origin",
            "destination_id": "museum",
            "transport": "flying",
            "reason": "验证",
        },
        {"kind": "place_search", "query": "测试地点", "city": "另一城市", "reason": "扩展查询"},
        {"kind": "nearby_restaurants", "place_id": "museum", "reason": "   "},
        {"kind": "execute", "reason": "任意调用"},
    ],
)
def test_query_rejects_extra_authority_unknown_kind_or_missing_reason(query_payload):
    with pytest.raises(ValidationError):
        QUERY_ADAPTER.validate_python(query_payload)


def test_request_batch_and_assumptions_are_bounded():
    with pytest.raises(ValidationError):
        DraftProposal.model_validate(proposal(query_requests=[route().model_dump()] * 25))
    with pytest.raises(ValidationError):
        DraftProposal.model_validate(proposal(assumptions=["暂定"] * 11))
    assert (
        len(
            DraftProposal.model_validate(
                proposal(query_requests=[route().model_dump()] * 24)
            ).query_requests
        )
        == 24
    )


def test_route_identity_keeps_direction_and_transport_but_ignores_reason():
    assert canonical_query(route()) == canonical_query(route(reason="换个说法仍是同一路段"))
    assert canonical_query(route()) != canonical_query(
        route(origin_id="museum", destination_id="origin")
    )
    assert canonical_query(route()) != canonical_query(route(transport="walking"))
    assert canonical_query(route().model_dump()) == ("route", "origin", "museum", "mixed")


def test_place_query_key_normalizes_format_without_aliasing_different_places():
    assert canonical_query(
        PlaceQuery(kind="place_search", query="上海博物馆（人民广场馆）", reason="用户提出")
    ) == canonical_query(
        PlaceQuery(kind="place_search", query=" 上海博物馆(人民广场馆) ", reason="另一次")
    )
    assert canonical_query(
        RestaurantQuery(kind="nearby_restaurants", place_id="museum", reason="午餐")
    ) != canonical_query(
        RestaurantQuery(kind="nearby_restaurants", place_id="hotel", reason="晚餐")
    )


def test_query_checks_ids_and_does_not_silently_change_user_transport():
    req = requirements(transport="walking")
    issues = check_query_requests(req, [route(origin_id="invented")], {"museum": {}})
    assert {i["code"] for i in issues} == {"query_identity", "query_transport"}
    assert issues[0]["unknown_ids"] == ["invented"]
    assert issues[1]["expected"] == "walking" and issues[1]["actual"] == "mixed"
    assert check_query_requests(req, [route(transport="walking")], ["origin", "museum"]) == []


def test_restaurant_queries_require_known_anchor():
    issues = check_query_requests(
        requirements(),
        [RestaurantQuery(kind="nearby_restaurants", place_id="new", reason="午餐")],
        {"museum": {}},
    )
    assert issues[0]["code"] == "query_identity"


def test_place_search_is_bound_to_explicit_requirements_or_revision_names():
    req = requirements(must_visit=["用户指定公园"])
    one = PlaceQuery(kind="place_search", query="用户指定公园", reason="核对用户明确地点")
    other = PlaceQuery(kind="place_search", query="模型自行推荐的公园", reason="看起来也不错")
    assert check_query_requests(req, [one], {}) == []
    assert check_query_requests(req, [other], {})[0]["code"] == "query_scope"
    assert check_query_requests(req, [other], {}, allowed_place_queries=[other.query]) == []


def test_context_projection_retains_every_identity_and_input_order_without_mutation():
    places = [
        {
            "id": f"p{i}",
            "name": f"候选{i}",
            "location": "121,31",
            "source": "高德",
            "photos": ["image"],
            "phone": "phone",
            "raw_response": {"huge": "payload"},
        }
        for i in range(30)
    ]
    value = {
        "requirements": {"selected_attractions": [{"id": "p29", "name": "候选29"}]},
        "attractions": places,
        "must_visit": [places[-1]],
        "hotels": [{"id": "manual", "name": "手选酒店"}],
        "facts": [
            {
                "place_id": "p29",
                "quote": "公开原文",
                "sources": [
                    {
                        "title": "参观说明",
                        "url": "https://museum.example/visit",
                        "excerpt": "不用重复带摘要",
                    }
                ],
            }
        ],
    }
    before = deepcopy(value)
    output = packet_context(value)
    assert value == before
    assert [p["id"] for p in output["attractions"]] == [f"p{i}" for i in range(30)]
    assert output["must_visit"][0]["id"] == "p29"
    assert output["hotels"][0]["id"] == "manual"
    assert output["requirements"]["selected_attractions"] == [{"id": "p29", "name": "候选29"}]
    assert output["facts"][0]["quote"] == "公开原文"
    assert "excerpt" not in output["facts"][0]["sources"][0]
    assert all(
        "photos" not in p and "phone" not in p and "raw_response" not in p
        for p in output["attractions"]
    )


def test_early_identity_check_accepts_provisional_meal_anchor_without_opening_facts():
    req = requirements()
    plan = draft()
    plan.stops.append(
        Stop(
            day=1,
            place_id="museum",
            kind="meal",
            start_time="12:00",
            end_time="13:00",
            title="附近自选正餐",
            reason="具体餐厅与供应待核实。",
        )
    )
    assert check_draft_identity(req, plan, {"museum": {"id": "museum"}}, []) == []


def test_early_identity_check_rejects_unknown_place_day_hotel_and_reversed_time_without_moving_stops():
    req = requirements(days=2, nights=1, hotel_id="manual")
    plan = draft(
        hotel_id="other",
        stops=[
            {
                "day": 3,
                "place_id": "invented",
                "kind": "activity",
                "start_time": "12:00",
                "end_time": "11:00",
                "reason": "bad",
            }
        ],
    )
    before = plan.model_dump()
    issues = check_draft_identity(req, plan, {"museum": {}}, [{"id": "manual"}])
    assert {i["code"] for i in issues} == {"hotel", "hotel_choice", "day", "identity", "time"}
    assert plan.model_dump() == before
    assert next(i for i in issues if i["code"] == "identity")["day"] == 3


def test_selected_hotel_and_hotel_stop_identity_are_checked():
    req = requirements(days=2, nights=1, hotel_id="manual")
    plan = draft(
        hotel_id="manual",
        stops=[
            {
                "day": 1,
                "place_id": "other",
                "kind": "hotel",
                "start_time": "19:00",
                "end_time": "20:00",
                "reason": "return",
            }
        ],
    )
    issues = check_draft_identity(req, plan, {"manual": {}, "other": {}}, {"manual": {}})
    assert [i["code"] for i in issues] == ["hotel_identity"]
