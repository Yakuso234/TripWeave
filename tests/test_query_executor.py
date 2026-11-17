"""Declared map query execution through synthetic HTTP, never a live supplier."""

import json
from contextlib import asynccontextmanager

import httpx2
import pytest

from tripweave.domain import Requirements
from tripweave.maps import MapClient
from tripweave.query_executor import QueryExecutor, query_key


def poi(pid, name, *, city="上海市", code="021", kind="风景名胜"):
    return {
        "id": pid,
        "name": name,
        "location": "121.47,31.23",
        "address": "测试地址",
        "cityname": city,
        "citycode": code,
        "type": kind,
        "photos": [{"url": "https://images.example/not-needed.jpg"}],
    }


def response(request):
    path = request.url.path
    if "/direction/" in path:
        return httpx2.Response(
            200, json={"status": "1", "route": {"paths": [{"duration": "600", "distance": "800"}]}}
        )
    if path.endswith("/place/around"):
        return httpx2.Response(
            200, json={"status": "1", "pois": [poi("r", "测试正餐候选", kind="餐饮服务")]}
        )
    if path.endswith("/place/text"):
        return httpx2.Response(
            200, json={"status": "1", "pois": [poi("new", request.url.params["keywords"])]}
        )
    raise AssertionError("unexpected map operation")


@asynccontextmanager
async def workspace(*, provider=response, req=None, max_calls=50, allowed_queries=()):
    calls = []

    def mocked(request):
        calls.append(request)
        return provider(request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(mocked)) as client:
        maps = MapClient(client, "PRIVATE-MAP-KEY", max_calls=max_calls)
        maps.normalize(poi("a", "测试馆"))
        maps.normalize(poi("b", "测试酒店", kind="住宿服务"))
        trace = []
        executor = QueryExecutor(
            req or Requirements(date="2026-11-21"), maps, trace, allowed_queries=allowed_queries
        )
        yield executor, maps, trace, calls


def route(**changes):
    return {
        "kind": "route",
        "origin_id": "a",
        "destination_id": "b",
        "transport": "mixed",
        "reason": "核实草稿中从馆到酒店的实际交通时间。",
        **changes,
    }


async def test_direction_mode_and_phase_are_bound_and_duplicate_reasons_do_not_spend_quota():
    async with workspace() as (executor, maps, trace, calls):
        result = await executor.execute(
            [route(), route(reason="相同路段再次核对。")], phase="draft_evidence", model_round=1
        )
        assert result["new_information"] is True
        first, second = result["results"]
        assert first["status"] == second["status"] == "ok"
        assert first["cache_hit"] is False and second["cache_hit"] is True
        assert first["new_information"] is True and second["new_information"] is False
        assert first["data"]["origin_id"] == "a" and first["data"]["destination_id"] == "b"
        assert first["data"]["duration_minutes"] == 10
        assert maps.calls == len(calls) == 1
        assert [event["model_round"] for event in trace] == [1, 1]
        assert all(event["phase"] == "draft_evidence" for event in trace)
        assert trace[1]["query"]["reason"] == "相同路段再次核对。"
        assert "PRIVATE-MAP-KEY" not in json.dumps(result) + json.dumps(trace)


async def test_reverse_route_is_a_new_query_and_caller_mutation_does_not_corrupt_cache():
    async with workspace() as (executor, _, _, calls):
        first = await executor.execute([route()])
        first["results"][0]["data"]["duration_minutes"] = 999
        reverse = await executor.execute([route(origin_id="b", destination_id="a")])
        repeated = await executor.execute([route()], phase="revision", model_round=3)
        assert len(calls) == 2 and reverse["new_information"] is True
        assert repeated["new_information"] is False
        assert repeated["results"][0]["data"]["duration_minutes"] == 10
        assert repeated["results"][0]["phase"] == "revision"
        assert repeated["results"][0]["model_round"] == 3


async def test_map_cache_can_be_new_evidence_to_executor_without_another_http_request():
    async with workspace() as (executor, maps, _, calls):
        await maps.route(maps.places["a"], maps.places["b"], "上海", "mixed")
        maps.max_calls = maps.calls
        first = await executor.execute([route()])
        again = await executor.execute([route()])
        assert len(calls) == 1
        assert first["results"][0]["cache_hit"] is True and first["new_information"] is True
        assert again["new_information"] is False


@pytest.mark.parametrize(
    "query,code",
    [
        (route(destination_id="unknown"), "unknown_place_id"),
        (route(transport="walking"), "transport_mismatch"),
        (route(reason=""), "query_reason_missing"),
        (route(city="北京"), "unexpected_query_fields"),
        (
            {"kind": "nearby_restaurants", "place_id": "unknown", "reason": "找午餐。"},
            "unknown_place_id",
        ),
        ({"kind": "delete_trip", "reason": "越权。"}, "unsupported_query"),
        ({"kind": []}, "unsupported_query"),
    ],
)
async def test_invalid_queries_have_item_errors_and_no_map_side_effect(query, code):
    async with workspace() as (executor, maps, _, calls):
        result = await executor.execute([query])
        item = result["results"][0]
        assert item["status"] == "not_executed" and item["code"] == code
        assert item["data"] is None and result["new_information"] is False
        assert maps.calls == len(calls) == 0


async def test_extra_secret_or_url_fields_never_enter_result_or_trace():
    async with workspace() as (executor, _, trace, calls):
        result = await executor.execute(
            [route(api_key="PRIVATE-EXTRA", url="https://private.example")]
        )
        assert result["results"][0]["status"] == "not_executed" and not calls
        encoded = json.dumps(result) + json.dumps(trace)
        assert "PRIVATE-EXTRA" not in encoded and "private.example" not in encoded


async def test_budget_exhaustion_is_not_empty_results_and_does_not_call_map():
    async with workspace(max_calls=0) as (executor, _, _, calls):
        result = await executor.execute(
            [route(), {"kind": "nearby_restaurants", "place_id": "a", "reason": "午餐。"}]
        )
        assert not calls and not result["new_information"]
        assert all(
            item["status"] == "not_executed" and item["code"] == "map_budget_exhausted"
            for item in result["results"]
        )
        assert all(item["data"] is None for item in result["results"])


async def test_failed_attempt_is_explicit_and_cached_instead_of_spending_quota_again():
    def denied(request):
        return httpx2.Response(
            200, json={"status": "0", "infocode": "10003", "info": "PRIVATE-PROVIDER-BODY"}
        )

    async with workspace(provider=denied) as (executor, _, trace, calls):
        first = await executor.execute([route()])
        again = await executor.execute([route()])
        item = first["results"][0]
        assert item["status"] == "error" and item["code"] == "map_query_failed"
        assert item["data"] is None and "10003" in item["error"]
        assert first["new_information"] is True
        assert again["new_information"] is False and again["results"][0]["cache_hit"]
        assert len(calls) == 1
        assert "PRIVATE-PROVIDER-BODY" not in json.dumps(trace)


async def test_restaurant_results_are_compact_and_known_for_followup_routes():
    async with workspace() as (executor, maps, _, calls):
        query = {"kind": "nearby_restaurants", "place_id": "a", "reason": "草稿午餐需要候选。"}
        result = await executor.execute([query])
        item = result["results"][0]
        assert item["status"] == "ok" and item["data"][0]["id"] == "r"
        assert "photos" not in item["data"][0] and "tips" not in item["data"][0]
        assert "r" in executor.allowed_place_ids and "r" in maps.places
        followup = await executor.execute([route(destination_id="r")])
        assert followup["results"][0]["status"] == "ok" and len(calls) == 2


async def test_known_foreign_city_id_is_not_valid_even_when_in_map_registry():
    async with workspace() as (executor, maps, _, calls):
        maps.normalize(poi("foreign", "其他城市地点", city="北京市", code="010"))
        executor.allowed_place_ids.add("foreign")
        result = await executor.execute([route(destination_id="foreign")])
        assert result["results"][0]["status"] == "not_executed" and not calls


async def test_prepared_code_only_places_keep_identity_but_cross_city_codes_are_rejected():
    async with workspace() as (executor, maps, _, calls):
        for place in maps.places.values():
            place["city"] = ""
        executor.city_codes.clear()
        result = await executor.execute([route()])
        assert result["results"][0]["status"] == "ok" and len(calls) == 1
        maps.places["b"]["citycode"] = "010"
        rejected = await executor.execute([route()])
        assert rejected["results"][0]["code"] == "city_mismatch"
        assert rejected["results"][0]["status"] == "not_executed" and len(calls) == 1


@pytest.mark.parametrize("source", ["must_visit", "booking", "window", "revision"])
async def test_place_search_is_limited_to_explicit_user_names_and_reuses_normalized_query(source):
    name = "新增测试馆"
    changes = {
        "must_visit": {"must_visit": [name]},
        "booking": {"booking_query": name},
        "window": {
            "visit_constraints": [
                {
                    "constraint_id": "visit",
                    "place_query": name,
                    "windows": [{"day": 1, "start_time": "10:00", "end_time": "12:00"}],
                }
            ]
        },
        "revision": {},
    }[source]
    req = Requirements.model_validate({"date": "2026-11-21", **changes})
    async with workspace(req=req, allowed_queries=[name] if source == "revision" else []) as (
        executor,
        _,
        _,
        calls,
    ):
        query = {"kind": "place_search", "query": name, "reason": "用户明确要求此地点。"}
        first = await executor.execute([query])
        again = await executor.execute([{**query, "query": " " + name + " "}])
        assert first["results"][0]["status"] == "ok"
        assert first["results"][0]["data"]["name"] == name
        assert again["results"][0]["cache_hit"] and len(calls) == 1


async def test_unrequested_place_and_url_queries_are_rejected_without_calling_map():
    async with workspace(allowed_queries=["https://private.example"]) as (executor, _, _, calls):
        result = await executor.execute(
            [
                {"kind": "place_search", "query": "任意新地点", "reason": "模型想扩展。"},
                {
                    "kind": "place_search",
                    "query": "https://private.example",
                    "reason": "模型想访问网址。",
                },
            ]
        )
        assert [item["code"] for item in result["results"]] == [
            "unrequested_place_search",
            "invalid_place_query",
        ]
        assert not calls


async def test_server_authorized_revision_names_can_be_added_without_changing_query_scope():
    async with workspace() as (executor, _, _, calls):
        executor.allow_query_names([" 用户新增馆 ", "https://private.example"])
        first = await executor.execute(
            [{"kind": "place_search", "query": "用户新增馆", "reason": "用户本次修订明确指定。"}]
        )
        second = await executor.execute(
            [{"kind": "place_search", "query": "模型额外想去的馆", "reason": "模型偏好。"}]
        )
        assert first["results"][0]["status"] == "ok"
        assert second["results"][0]["code"] == "unrequested_place_search"
        assert len(calls) == 1


async def test_exact_name_does_not_allow_foreign_city_or_a_nearby_substitute():
    def provider(request):
        return httpx2.Response(
            200,
            json={"status": "1", "pois": [poi("foreign", "新增测试馆", city="北京市", code="010")]},
        )

    async with workspace(provider=provider, allowed_queries=["新增测试馆"]) as (executor, _, _, _):
        item = (
            await executor.execute(
                [{"kind": "place_search", "query": "新增测试馆", "reason": "用户要求。"}]
            )
        )["results"][0]
        assert item["status"] == "error" and item["data"] is None
        assert "foreign" not in executor.allowed_place_ids


async def test_batch_size_guard_runs_before_any_query_and_keys_preserve_direction():
    async with workspace() as (executor, _, _, calls):
        with pytest.raises(ValueError, match="24"):
            await executor.execute([route()] * 25)
        assert not calls
    assert query_key(route()) != query_key(route(origin_id="b", destination_id="a"))
    assert query_key(route()) != query_key(route(transport="walking"))
    assert query_key(route()) == query_key(route(reason="换一个理由。"))
