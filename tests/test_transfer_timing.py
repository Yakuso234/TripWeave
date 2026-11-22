"""Recorded rest/departure times participate in feasibility. No live suppliers."""

from copy import deepcopy

import pytest
from pydantic import ValidationError
from test_pacing import Maps, data, req, stop

from tripweave.domain import Itinerary, TransferTiming
from tripweave.real_planner import validate
from tripweave.storage import Store


class Routes(Maps):
    async def route(self, origin, target, city, mode):
        return {"duration_minutes": 36 if origin["id"] != target["id"] else 0}


def timing(**changes):
    return TransferTiming.model_validate(
        {
            "departure_time": "17:40",
            "arrival_time": "18:16",
            "buffer_minutes": 10,
            "rest_before": [{"start_time": "15:20", "end_time": "17:40"}],
            **changes,
        }
    )


def plan(t=None, *, next_start="18:00"):
    first, second = stop("p", "14:00", "15:20"), stop("q", next_start, "19:15", "meal")
    second.transfer = t
    return Itinerary(summary="测试休息之后是否还能赶上。", stops=[first, second])


async def issues(output, request=None):
    maps = Routes()
    return await validate(request or req(), output, data(maps), maps)


async def test_large_gap_cannot_hide_late_departure_after_rest():
    assert await issues(plan()) == []  # Legacy record only establishes a possible gap.
    errors = await issues(plan(timing()))
    error = next(i for i in errors if i["code"] == "connection")
    assert (error["day"], error["stop_number"]) == (1, 2)
    assert error["departure_time"] == "17:40" and error["travel_minutes"] == 36
    assert error["buffer_minutes"] == 10 and error["earliest_start_time"] == "18:16"
    assert error["shortfall_minutes"] == 16


async def test_declared_travel_end_cannot_understate_map_duration():
    errors = await issues(plan(timing(arrival_time="18:00")))
    error = next(i for i in errors if i["code"] == "transfer_duration")
    assert error["field"] == "transfer.arrival_time"
    assert error["available_minutes"] == 20 and error["travel_minutes"] == 36
    assert error["shortfall_minutes"] == 16


async def test_feasible_rest_travel_and_buffer_pass_without_changing_arrangement():
    output = plan(timing(), next_start="18:30")
    original = deepcopy(output.model_dump())
    assert await issues(output) == []
    assert output.model_dump() == original


async def test_departure_cannot_ignore_a_rest_that_has_not_ended():
    errors = await issues(plan(timing(departure_time="15:20", arrival_time="15:56")))
    error = next(i for i in errors if i["code"] == "rest_timing")
    assert error["field"] == "transfer.rest_before[0]"


@pytest.mark.parametrize(
    "rests",
    [
        [{"start_time": "18:00", "end_time": "18:20"}],
        [{"start_time": "18:16", "end_time": "19:00"}],
    ],
)
async def test_destination_rest_cannot_overlap_travel_or_activity(rests):
    errors = await issues(plan(timing(rest_after=rests), next_start="18:30"))
    assert any(i["code"] == "rest_timing" for i in errors)


async def test_destination_rest_remains_hard_but_suggested_buffer_does_not_block():
    t = timing(rest_after=[{"start_time": "18:16", "end_time": "18:25"}])
    errors = await issues(plan(t, next_start="18:30"))
    assert errors == []
    assert await issues(plan(t, next_start="18:40")) == []


async def test_overlapping_rest_periods_are_not_double_booked():
    errors = await issues(
        plan(
            timing(
                rest_before=[
                    {"start_time": "15:20", "end_time": "16:00"},
                    {"start_time": "15:50", "end_time": "17:40"},
                ]
            ),
            next_start="18:30",
        )
    )
    assert any(i["code"] == "rest_timing" for i in errors)


async def test_departure_before_previous_activity_ends_is_rejected():
    errors = await issues(
        plan(timing(departure_time="15:10", arrival_time="15:46", rest_before=[]))
    )
    assert any(i["code"] == "departure_before_previous_end" for i in errors)


async def test_explicit_first_departure_respects_strict_user_boundary():
    first = stop("p", "10:00", "11:00")
    first.transfer = timing(departure_time="08:50", arrival_time="09:26", rest_before=[])
    output = Itinerary(summary="合成首段", stops=[first])
    assert await issues(output) == []
    assert any(
        i["code"] == "departure_before_previous_end"
        for i in await issues(output, req(strict_preferences=["start_time"]))
    )


async def test_rest_before_first_departure_does_not_violate_departure_only_lock():
    first = stop("p", "10:00", "11:00")
    first.transfer = timing(
        departure_time="09:00",
        arrival_time="09:36",
        rest_before=[{"start_time": "08:30", "end_time": "09:00"}],
    )
    assert (
        await issues(
            Itinerary(summary="出发前休息", stops=[first]), req(strict_preferences=["start_time"])
        )
        == []
    )


@pytest.mark.parametrize(
    "change",
    [
        {"buffer_minutes": -1},
        {"departure_time": "25:00"},
        {"rest_before": [{"start_time": "17:40", "end_time": "15:20"}]},
        {"url": "https://example.test/tool"},
    ],
)
def test_invalid_time_and_unexpected_transfer_fields_are_rejected(change):
    with pytest.raises(ValidationError):
        timing(**change)


def test_timing_persists_in_sqlite_and_legacy_records_keep_their_shape(tmp_path):
    legacy = plan().model_dump(mode="json")
    assert "transfer" not in legacy["stops"][1]
    assert Itinerary.model_validate(legacy).model_dump(mode="json") == legacy
    database = str(tmp_path / "timing.db")
    store = Store(database)
    store.initialize()
    trip = store.create_trip(req().model_dump(mode="json"))
    payload = plan(timing(), next_start="18:30").model_dump(mode="json")
    store.add_candidate(
        {
            "id": "timed",
            "trip_id": trip["id"],
            "mode": "deepseek",
            "requirements_revision": 1,
            "base_version": 0,
            "status": "conflict",
            "outcome": "incomplete",
            "draft": payload,
        }
    )
    store.dispose()
    reopened = Store(database)
    assert reopened.get_candidate(trip["id"], "timed")["draft"] == payload
    reopened.dispose()


async def test_real_graph_returns_precise_transfer_error_for_model_revision():
    import json

    import httpx2
    from draft_helpers import phase_packet, reply, route_queries
    from test_planning_workflow import itinerary, map_response, requirements, tool_names
    from test_real_planner import configured

    from tripweave.real_planner import run_plan

    feedback = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        if tool_names(body) == {"review_report"}:
            return reply("review_report", {"summary": "合成通过", "suggestions": []})
        context = json.loads(body["messages"][-1]["content"])
        errors = context.get("validation_issues", [])
        current = itinerary()
        current["stops"][0]["transfer"] = {
            "departure_time": "09:58",
            "arrival_time": "10:03",
            "buffer_minutes": 0,
        }
        if any(i["code"] == "connection" for i in errors):
            feedback.extend(errors)
            current["stops"][0]["transfer"] = {
                "departure_time": "09:40",
                "arrival_time": "09:45",
                "buffer_minutes": 10,
            }
        return reply(
            "draft_proposal", phase_packet(context, current, queries=route_queries(current))
        )

    result = await run_plan(
        requirements(), "deepseek", configured(), transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "ready", result
    error = next(i for i in feedback if i["code"] == "connection")
    assert error["departure_time"] == "09:58" and error["earliest_start_time"] == "10:03"
    assert result["timing_coverage"] == {"stop_count": 1, "explicit_count": 1}


@pytest.mark.parametrize(
    "start,expected_margin", [("18:16", 0), ("18:19", 3), ("18:21", None), ("18:22", None)]
)
async def test_less_than_five_minutes_is_only_an_advisory(start, expected_margin):
    from tripweave.real_planner import buffer_advisories

    maps = Routes()
    maps.cache = {("p", "q", "mixed"): {"duration_minutes": 36}}
    output = plan(timing(buffer_minutes=60), next_start=start)
    assert await validate(req(), output, data(maps), maps) == []
    warnings = buffer_advisories(req(), output, data(maps), maps)
    if expected_margin is None:
        assert warnings == []
    else:
        assert len(warnings) == 1 and warnings[0]["severity"] == "warning"
        assert warnings[0]["actual_minutes"] == expected_margin


async def test_actual_rest_is_deducted_from_advisory_margin_and_short_route_still_fails():
    from tripweave.real_planner import buffer_advisories

    maps = Routes()
    maps.cache = {("p", "q", "mixed"): {"duration_minutes": 36}}
    output = plan(
        timing(rest_after=[{"start_time": "18:16", "end_time": "18:25"}]), next_start="18:27"
    )
    assert await issues(output) == []
    warnings = buffer_advisories(req(), output, data(maps), maps)
    assert warnings[0]["actual_minutes"] == 2
    short = plan(timing(), next_start="18:10")
    assert any(i["code"] == "connection" for i in await issues(short))
    assert buffer_advisories(req(), short, data(maps), maps) == []


async def test_legacy_stops_report_the_maximum_gap_instead_of_a_scheduled_buffer():
    from tripweave.real_planner import buffer_advisories
    from tripweave.route_evidence import route_table

    maps = Routes()
    maps.cache = {("p", "q", "mixed"): {"duration_minutes": 36}}
    output = plan(next_start="15:59")
    queried = data(maps)
    assert await validate(req(), output, queried, maps) == []
    rows = route_table(req(), output, queried["origin"]["id"], maps.cache)
    assert rows[1]["buffer_minutes"] == 3 and rows[1]["buffer_basis"] == "upper_bound"
    warnings = buffer_advisories(req(), output, queried, maps, routes=rows)
    assert warnings[0]["maximum_minutes"] == 3 and "actual_minutes" not in warnings[0]


async def test_low_buffer_never_starts_extra_planner_repair():
    import json

    import httpx2
    from draft_helpers import packet, reply, route_queries
    from test_planning_workflow import itinerary, map_response, requirements, tool_names
    from test_real_planner import configured

    from tripweave.real_planner import run_plan

    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        body = json.loads(request.content)
        calls.append(body)
        if tool_names(body) == {"review_report"}:
            return reply("review_report", {"summary": "合成通过", "suggestions": []})
        current = itinerary()
        current["stops"][0]["transfer"] = {
            "departure_time": "09:55",
            "arrival_time": "10:00",
            "buffer_minutes": 60,
        }
        return reply("draft_proposal", packet(current, queries=route_queries(current)))

    result = await run_plan(
        requirements(), "deepseek", configured(), transport=httpx2.MockTransport(provider)
    )
    assert result["outcome"] == "ready" and result["issues"] == [] and len(calls) == 3
    assert result["buffer_advisories"][0]["actual_minutes"] == 0


def test_suggested_buffer_has_no_business_upper_cap():
    assert timing(buffer_minutes=300).buffer_minutes == 300
