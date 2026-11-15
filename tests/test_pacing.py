"""Pacing semantics and integration, entirely offline with explicit synthetic places."""

import copy

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_real_planner import configured, handler

from tripweave.app import create_app
from tripweave.domain import Itinerary, Requirements, Stop, TravelPreferences
from tripweave.pacing import daily_metrics, pacing_issues
from tripweave.real_planner import run_plan, validate
from tripweave.storage import Store


def req(**values):
    return Requirements(date="2026-11-21", start_time="09:00", end_time="21:00", **values)


def stop(place, start, end, kind="activity", day=1):
    return Stop(day=day, place_id=place, start_time=start, end_time=end, kind=kind, reason="测试")


def split_visit():
    return Itinerary(
        summary="合成分段游玩",
        stops=[
            stop("p", "10:00", "11:00"),
            stop("r", "12:00", "13:00", "meal"),
            stop("p", "14:00", "15:00"),
            stop("p", "19:00", "20:00"),
        ],
    )


class Maps:
    def __init__(self):
        self.places = {key: {"id": key, "name": key} for key in ("o", "p", "q", "r", "h")}

    async def route(self, origin, target, city, mode):
        return {"duration_minutes": 0 if origin["id"] == target["id"] else 10}


def data(maps, **extra):
    return {"origin": maps.places["o"], "hotels": [maps.places["h"]], "booking": None, **extra}


async def test_morning_afternoon_evening_same_place_counts_once_but_all_time_accumulates():
    requirements = req(
        max_attractions=1,
        max_sightseeing_minutes=180,
        max_sightseeing_ratio=25,
        max_transport_minutes=30,
    )
    plan, maps = split_visit(), Maps()
    before = plan.model_dump()
    assert await validate(requirements, plan, data(maps), maps) == []
    row = daily_metrics(requirements, plan, [], {1: 30})[0]
    assert row["attraction_count"] == 1
    assert row["sightseeing_minutes"] == 180
    assert row["available_minutes"] == 720
    assert row["sightseeing_ratio"] == 25
    assert [s["stop_number"] for s in row["sightseeing_stops"]] == [1, 3, 4]
    assert plan.model_dump() == before  # Validation never changes the experience.


async def test_each_exceeded_metric_has_day_actual_limit_and_precise_segments():
    requirements = req(
        max_attractions=0,
        max_sightseeing_minutes=179,
        max_sightseeing_ratio=24.99,
        max_transport_minutes=29,
    )
    maps = Maps()
    issues = await validate(requirements, split_visit(), data(maps), maps)
    assert {i["code"] for i in issues} == {
        "max_attractions",
        "max_sightseeing_minutes",
        "max_sightseeing_ratio",
        "max_transport_minutes",
    }
    for issue in issues:
        assert issue["day"] == 1 and issue["actual"] > issue["limit"]
        assert issue["available_minutes"] == 720
    assert issues[0]["occurrences"][2]["start_time"] == "19:00"


def test_ratio_denominator_does_not_shrink_with_early_finish_or_expand_with_blanks():
    plan = Itinerary(summary="测试", stops=[stop("p", "10:00", "11:00")])
    row = daily_metrics(req(), plan, [], {1: 10})[0]
    assert row["sightseeing_ratio"] == pytest.approx(100 / 12)
    assert row["available_minutes"] == 720
    assert row["sightseeing_minutes"] == 60


async def test_transport_includes_first_leg_meal_legs_and_hotel_return_and_next_day_origin():
    requirements = req(days=2, max_transport_minutes=29)
    plan = Itinerary(
        summary="测试",
        hotel_id="h",
        stops=[
            stop("p", "10:00", "11:00"),
            stop("r", "12:00", "13:00", "meal"),
            stop("h", "20:00", "20:15", "hotel"),
            stop("p", "10:00", "11:00", day=2),
        ],
    )
    maps, calls = Maps(), []
    route = maps.route

    async def recorded(origin, target, city, mode):
        calls.append((origin["id"], target["id"]))
        return await route(origin, target, city, mode)

    maps.route = recorded
    issues = await validate(requirements, plan, data(maps), maps)
    issue = next(i for i in issues if i["code"] == "max_transport_minutes")
    assert issue["day"] == 1 and issue["actual"] == 30
    assert calls == [("o", "p"), ("p", "r"), ("r", "h"), ("h", "p")]
    assert any(i["code"] == "duplicate" for i in issues)  # Cross-day protection retained.


async def test_known_aliases_count_once_but_duplicate_content_still_requires_model_revision():
    maps = Maps()
    plan = Itinerary(
        summary="测试", stops=[stop("p", "10:00", "11:00"), stop("q", "14:00", "15:00")]
    )
    groups = [{"group_id": "g", "members": ["p", "q"]}]
    issues = await validate(
        req(max_attractions=1), plan, data(maps, experience_groups=groups), maps
    )
    assert [i["code"] for i in issues] == ["duplicate"]
    assert issues[0]["related_days"] == [1]


async def test_overlapping_segments_cannot_pass_by_being_the_same_place():
    maps = Maps()
    plan = Itinerary(
        summary="测试", stops=[stop("p", "10:00", "12:00"), stop("p", "11:00", "13:00")]
    )
    assert any(i["code"] == "connection" for i in await validate(req(), plan, data(maps), maps))
    assert daily_metrics(req(), plan, [], {1: 10})[0]["sightseeing_minutes"] == 180


def test_legacy_limit_migrates_without_mutation_and_explicit_new_null_wins():
    old = {"date": "2026-11-21", "max_activities": 1}
    original = copy.deepcopy(old)
    parsed = Requirements.model_validate(old)
    assert parsed.max_attractions == 1
    assert "max_activities" not in parsed.model_dump()
    assert old == original
    assert Requirements.model_validate({**old, "max_attractions": None}).max_attractions is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_attractions", 21),
        ("max_attractions", 1.5),
        ("max_sightseeing_minutes", -1),
        ("max_sightseeing_ratio", 101),
        ("max_sightseeing_ratio", float("nan")),
        ("max_transport_minutes", 1441),
    ],
)
def test_invalid_limits_rejected(key, value):
    with pytest.raises(ValidationError):
        TravelPreferences(**{key: value})


def test_optional_limits_and_explicit_zero_are_different_and_missing_route_is_unknown():
    plan = Itinerary(summary="测试", stops=[stop("p", "10:00", "11:00")])
    row = daily_metrics(req(max_attractions=None), plan, [], {1: None})[0]
    assert row["transport_minutes"] is None
    assert pacing_issues([row]) == []
    zero = daily_metrics(
        req(max_attractions=0, max_sightseeing_minutes=0, max_sightseeing_ratio=0),
        plan,
        [],
        {1: None},
    )
    assert len(pacing_issues(zero)) == 3


async def test_result_saves_statistics_using_existing_route_cache_without_extra_requests(tmp_path):
    result = await run_plan(req(), "basic", configured(), transport=httpx2.MockTransport(handler))
    assert result["status"] == "ready"
    row = result["daily_metrics"][0]
    assert row["transport_minutes"] == sum(r["duration_minutes"] for r in result["routes"])
    stops = result["itinerary"]["stops"]
    visits = [s for s in stops if s["kind"] in ("activity", "booking")]
    assert row["attraction_count"] == len({s["place_id"] for s in visits})
    assert row["available_minutes"] == 720
    database = str(tmp_path / "metrics.db")
    store = Store(database)
    store.initialize()
    trip = store.create_trip(req().model_dump(mode="json"))
    store.add_candidate(
        {
            **result,
            "id": "measured",
            "trip_id": trip["id"],
            "mode": "deepseek",
            "requirements_revision": 1,
            "base_version": 0,
        }
    )
    store.dispose()
    reopened = Store(database)
    assert (
        reopened.get_candidate(trip["id"], "measured")["daily_metrics"] == result["daily_metrics"]
    )
    reopened.dispose()


def test_sqlite_legacy_profile_read_and_new_metrics_survive_restart_without_rewriting_trip(
    tmp_path,
):
    database = str(tmp_path / "pacing.db")
    # A pre-migration SQLite profile, not a new API request already normalized by Pydantic.
    store = Store(database)
    store.initialize()
    store.save_preferences({"max_activities": 1}, 0)
    store.engine.dispose()
    with TestClient(create_app(database)) as client:
        profile = client.get("/api/preferences").json()
        assert profile["values"]["max_attractions"] == 1
        assert "max_activities" not in profile["values"]
        trip = client.post("/api/trips", json={"date": "2026-11-21", "max_activities": 2}).json()
        values = {
            **profile["values"],
            "max_attractions": None,
            "max_sightseeing_minutes": 180,
            "max_sightseeing_ratio": 25,
            "max_transport_minutes": 0,
        }
        saved = client.put("/api/preferences", json={"expected_revision": 1, "values": values})
        assert saved.status_code == 200
    with TestClient(create_app(database)) as client:
        assert client.get("/api/preferences").json() == saved.json()
        assert client.get(f"/api/trips/{trip['id']}").json()["requirements"] == trip["requirements"]
        assert trip["requirements"]["max_attractions"] == 2
