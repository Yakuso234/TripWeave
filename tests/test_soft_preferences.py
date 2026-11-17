"""Soft objectives never waive feasibility or explicit commitments. Offline only."""

import json
from copy import deepcopy

import httpx2
import pytest
from draft_helpers import packet, reply
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_pacing import Maps, data, req, stop
from test_planning_workflow import itinerary, map_response, requirements, tool_names
from test_real_planner import configured, handler

from tripweave.app import create_app
from tripweave.domain import Itinerary, TravelPreferences
from tripweave.pacing import daily_metrics, pacing_issues
from tripweave.planning_contracts import preflight_requirements
from tripweave.real_planner import preference_deviations, run_plan, validate


async def test_four_soft_targets_report_deviations_without_rejecting_or_changing_plan():
    request = req(
        max_attractions=0,
        max_sightseeing_minutes=60,
        max_sightseeing_ratio=1,
        max_transport_minutes=5,
    )
    plan = Itinerary(summary="保留完整体验，解释目标偏离。", stops=[stop("p", "10:00", "12:00")])
    before, maps = plan.model_dump(), Maps()
    assert await validate(request, plan, data(maps), maps) == []
    warnings = pacing_issues(daily_metrics(request, plan, [], {1: 10}))
    assert len(warnings) == 4
    assert all(
        w["category"] == "preference_deviation" and w["severity"] == "warning" for w in warnings
    )
    assert plan.model_dump() == before


@pytest.mark.parametrize(
    "field",
    [
        "max_attractions",
        "max_sightseeing_minutes",
        "max_sightseeing_ratio",
        "max_transport_minutes",
    ],
)
async def test_explicitly_locked_metric_still_blocks(field):
    request = req(
        max_attractions=0,
        max_sightseeing_minutes=60,
        max_sightseeing_ratio=1,
        max_transport_minutes=5,
        strict_preferences=[field],
    )
    plan = Itinerary(summary="合成", stops=[stop("p", "10:00", "12:00")])
    maps = Maps()
    issues = await validate(request, plan, data(maps), maps)
    assert [i["code"] for i in issues] == [field]
    assert issues[0]["severity"] == "error"


@pytest.mark.parametrize(
    "field,start,end,code",
    [
        ("start_time", "08:30", "10:00", "connection"),
        ("end_time", "20:00", "22:00", "time"),
    ],
)
async def test_daily_time_targets_can_relax_but_explicit_locks_cannot(field, start, end, code):
    maps = Maps()
    plan = Itinerary(summary="合成", stops=[stop("p", start, end)])
    assert await validate(req(), plan, data(maps), maps) == []
    issues = await validate(req(strict_preferences=[field]), plan, data(maps), maps)
    assert any(i["code"] == code for i in issues)


async def test_soft_time_window_cannot_allow_overlapping_stops_or_negative_first_departure():
    maps = Maps()
    overlap = Itinerary(
        summary="合成", stops=[stop("p", "10:00", "11:00"), stop("q", "11:05", "12:00")]
    )
    assert any(i["code"] == "connection" for i in await validate(req(), overlap, data(maps), maps))
    previous_day = Itinerary(summary="合成", stops=[stop("p", "00:05", "01:00")])
    assert any(
        i["code"] == "connection" for i in await validate(req(), previous_day, data(maps), maps)
    )


def test_fixed_booking_outside_soft_window_is_not_false_user_conflict():
    request = req(booking_query="用户预约地点", booking_start="22:00", booking_end="23:00")
    assert preflight_requirements(request) == []
    assert (
        preflight_requirements(request.model_copy(update={"strict_preferences": ["end_time"]}))[0][
            "code"
        ]
        == "booking_outside_daily_window"
    )


async def test_soft_overrun_reaches_reviewer_without_extra_repair_and_keeps_text_requirements():
    calls = []
    plan = itinerary()
    notes = "不可删除博物馆；实际价格未知时不能声称满足最高金额。"

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        context = json.loads(body["messages"][-1]["content"])
        instructions = " ".join(str(m.get("content", "")) for m in body["messages"][:-1])
        assert "需求分为三类" in instructions
        assert "字段名含max" in instructions
        assert "用户在notes" in instructions
        assert context["requirements"]["notes"] == notes
        if tool_names(body) == {"review_report"}:
            assert context["program_issues"] == []
            assert context["preference_deviations"][0]["code"] == "max_sightseeing_minutes"
            return reply("review_report", {"summary": "合成评审认可具体取舍。", "suggestions": []})
        assert "上限超出时" not in instructions
        return reply("draft_proposal", packet(plan, queries=None if len(calls) == 1 else []))

    result = await run_plan(
        requirements(max_sightseeing_minutes=30, notes=notes),
        "deepseek",
        configured(),
        transport=httpx2.MockTransport(provider),
    )
    assert result["outcome"] == "ready" and len(calls) == 3
    assert result["itinerary"] == plan
    assert result["issues"] == [] and result["preference_policy"] == "soft_targets_v1"
    assert result["preference_deviations"][0]["actual"] == 60


async def test_expected_money_is_not_a_ceiling_or_a_verified_price():
    result = await run_plan(
        req(days=2, budget_total=100),
        "basic",
        configured(),
        transport=httpx2.MockTransport(handler),
    )
    assert result["outcome"] == "ready", result
    assert any(w["code"] == "expected_budget" for w in result["preference_deviations"])
    assert result["costs"]["total"] is None
    assert result["hotel"]["nightly_rate"] is None


async def test_time_target_deviations_use_only_existing_evidence_and_keep_fixed_ratio_denominator():
    request = req()  # Explicit fixture uses 09:00–21:00.
    plan = Itinerary(summary="合成", stops=[stop("p", "08:30", "22:00")])
    maps = Maps()
    maps.cache = {("o", "p", "mixed"): {"duration_minutes": 10}}
    warnings = preference_deviations(request, plan, data(maps), maps)
    assert {w["code"] for w in warnings} == {"preferred_start_time", "preferred_end_time"}
    assert warnings[0]["actual"] == "08:20"
    assert daily_metrics(request, plan, [], {1: 10})[0]["available_minutes"] == 720


def test_notes_and_locks_survive_sqlite_restart_and_do_not_mutate_old_candidate(tmp_path):
    path = str(tmp_path / "preferences.sqlite")
    with TestClient(create_app(path)) as client:
        created = client.post(
            "/api/trips",
            json={
                "date": "2026-11-21",
                "strict_preferences": ["max_transport_minutes"],
                "max_transport_minutes": 90,
                "notes": "交通绝不能超过90分钟。",
            },
        ).json()
        old = deepcopy(created["requirements"])
        profile = client.get("/api/preferences").json()
        response = client.put(
            "/api/preferences",
            json={
                "expected_revision": profile["revision"],
                "values": {
                    **profile["values"],
                    "strict_preferences": ["max_attractions"],
                    "custom_pace": "必须午休",
                },
            },
        )
        assert response.status_code == 200
    with TestClient(create_app(path)) as client:
        assert client.get(f"/api/trips/{created['id']}").json()["requirements"] == old
        assert client.get("/api/preferences").json()["values"]["strict_preferences"] == [
            "max_attractions"
        ]


def test_unknown_strict_preference_cannot_change_system_budget_or_permissions():
    with pytest.raises(ValidationError):
        TravelPreferences(strict_preferences=["disable_validation"])
