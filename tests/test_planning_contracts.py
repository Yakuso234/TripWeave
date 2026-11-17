"""Deterministic constraints prove only what supplied requirements can support."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tripweave.domain import Itinerary, Stop
from tripweave.planning_contracts import (
    PlanningInformationRequest,
    PlanningNeed,
    ResearchFact,
    ResearchReport,
    TimeWindow,
    VisitConstraint,
    preflight_requirements,
    validate_research_facts,
    validate_visit_constraints,
)


def window(day=1, start="10:00", end="12:00"):
    return TimeWindow(day=day, start_time=start, end_time=end)


def visit(cid="a", pid="poi-a", windows=None, **kwargs):
    return VisitConstraint(
        constraint_id=cid,
        place_id=pid,
        windows=[window()] if windows is None else windows,
        **kwargs,
    )


def requirements(constraints=(), **changes):
    values = {
        "strict_preferences": ["start_time", "end_time"],
        "days": 2,
        "start_time": "09:00",
        "end_time": "20:30",
        "booking_query": "",
        "booking_day": 1,
        "booking_start": "14:00",
        "booking_end": "15:00",
        "visit_constraints": list(constraints),
    }
    return SimpleNamespace(**(values | changes))


def stop(day=1, pid="poi-a", start="10:00", end="11:00", kind="activity"):
    return Stop(
        day=day,
        place_id=pid,
        start_time=start,
        end_time=end,
        kind=kind,
        reason="synthetic planning contract fixture",
    )


def output(*stops):
    return Itinerary(summary="synthetic fixture", stops=list(stops))


@pytest.mark.parametrize(
    "value",
    [
        {"day": 0, "start_time": "10:00", "end_time": "11:00"},
        {"day": 1, "start_time": "24:00", "end_time": "11:00"},
        {"day": 1, "start_time": "10:00", "end_time": "10:00"},
        {"day": 1, "start_time": "23:00", "end_time": "01:00"},
    ],
)
def test_window_rejects_invalid_or_ambiguous_calendar_clock(value):
    with pytest.raises(ValidationError):
        TimeWindow.model_validate(value)


def test_required_identity_and_fixed_window_contract():
    with pytest.raises(ValidationError, match="地点ID"):
        visit(pid=None, place_query="   ")
    with pytest.raises(ValidationError, match="固定预约"):
        visit(windows=[window(), window(2)], fixed=True)
    with pytest.raises(ValidationError, match="extra_forbidden"):
        visit(unknown_policy="ignore me")
    assert visit(pid=None, place_query="  某馆  ").place_query == "某馆"


def test_need_fields_are_bounded_deduplicated_and_extra_fields_rejected():
    need = PlanningNeed(
        place_id="poi-a",
        day=1,
        fields=["opening_hours", "opening_hours", "last_admission"],
        question="Which times apply on this date?",
    )
    assert need.fields == ["opening_hours", "last_admission"]
    assert PlanningInformationRequest(needs=[need], reason="missing dated opening window")
    with pytest.raises(ValidationError):
        PlanningNeed(place_id="poi-a", day=1, fields=[], question="?")
    with pytest.raises(ValidationError):
        PlanningInformationRequest(needs=[], reason="missing")


@pytest.mark.parametrize(
    ("start", "end"),
    [("08:30", "09:30"), ("20:00", "21:00"), ("21:00", "22:00")],
)
def test_booking_conflict_is_proven_before_any_route_or_model_call(start, end):
    req = requirements(booking_query="confirmed venue", booking_start=start, booking_end=end)
    issue = preflight_requirements(req)[0]
    assert issue["category"] == "hard_conflict"
    assert issue["code"] == "booking_outside_daily_window"
    assert issue["constraint_ids"] == ["booking", "daily_window"]
    assert issue["day"] == 1
    assert issue["expected"] == {"start_time": "09:00", "end_time": "20:30"}
    assert issue["actual"]["start_time"] == start


def test_window_intersection_and_minimum_duration_not_window_containment():
    # An availability window can start before daily departure; choose its fitting part.
    fitting = visit(windows=[window(start="08:00", end="10:00")])
    assert preflight_requirements(requirements([fitting])) == []
    too_long = fitting.model_copy(update={"min_duration_minutes": 61})
    issues = preflight_requirements(requirements([too_long]))
    assert [issue["code"] for issue in issues] == ["visit_window_unavailable"]
    exact = fitting.model_copy(update={"fixed": True})
    assert preflight_requirements(requirements([exact]))[0]["category"] == "hard_conflict"


def test_one_fitting_alternative_prevents_false_hard_conflict():
    constraint = visit(windows=[window(start="08:00", end="09:00"), window(2)])
    assert preflight_requirements(requirements([constraint])) == []


def test_same_evening_time_can_be_split_over_different_days():
    alternatives = [window(1, "21:00", "22:00"), window(2, "21:00", "22:00")]
    req = requirements(
        [visit("a", "poi-a", alternatives), visit("b", "poi-b", alternatives)],
        end_time="23:00",
    )
    assert preflight_requirements(req) == []


def test_known_fixed_overlap_has_constraint_ids_and_no_automatic_relaxation():
    a = visit("a", "poi-a", [window(start="10:00", end="11:30")], fixed=True)
    b = visit("b", "poi-b", [window(start="11:00", end="12:00")], fixed=True)
    req = requirements([a, b])
    saved = deepcopy(req)
    issues = preflight_requirements(req)
    assert len(issues) == 1
    assert issues[0]["code"] == "fixed_window_overlap"
    assert issues[0]["constraint_ids"] == ["a", "b"]
    assert issues[0]["place_ids"] == ["poi-a", "poi-b"]
    assert vars(req) == vars(saved)


def test_adjoining_fixed_windows_need_route_evidence_not_false_overlap_proof():
    req = requirements(
        [
            visit("a", "poi-a", [window(start="10:00", end="11:00")], fixed=True),
            visit("b", "poi-b", [window(start="11:00", end="12:00")], fixed=True),
        ]
    )
    assert preflight_requirements(req) == []


@pytest.mark.parametrize("second", [window(), window(start="13:00", end="14:00")])
def test_same_venue_duplicate_or_separate_fixed_appointments_are_not_infeasible(second):
    req = requirements([visit("a", "poi-a", fixed=True), visit("b", "poi-a", [second], fixed=True)])
    assert preflight_requirements(req) == []


def test_different_queries_may_be_aliases_until_identity_resolution():
    req = requirements(
        [
            visit("a", None, place_query="venue full name", fixed=True),
            visit("b", None, place_query="venue short alias", fixed=True),
        ]
    )
    assert preflight_requirements(req) == []
    assert preflight_requirements(req, {"a": "same-poi", "b": {"id": "same-poi"}}) == []
    issues = preflight_requirements(req, {"a": "first-poi", "b": {"id": "other-poi"}})
    assert [issue["code"] for issue in issues] == ["fixed_window_overlap"]


def test_legacy_and_structured_duplicate_appointment_can_share_a_stop():
    req = requirements(
        [visit("a", None, [window(start="14:00", end="15:00")], place_query="某馆", fixed=True)],
        booking_query="  某馆  ",
    )
    assert preflight_requirements(req) == []


@pytest.mark.parametrize(
    "candidate",
    [
        stop(start="09:59", end="11:00"),
        stop(start="11:01", end="12:01"),
        stop(start="10:00", end="10:59"),
        stop(kind="meal"),
    ],
)
def test_full_visit_duration_and_kind_are_checked_with_detailed_actual(candidate):
    constraint = visit()
    issues = validate_visit_constraints(requirements([constraint]), output(candidate), {})
    assert len(issues) == 1
    issue = issues[0]
    assert issue["category"] == "draft_error"
    assert issue["constraint_ids"] == ["a"]
    assert issue["day"] == 1
    assert issue["actual"][0]["stop_number"] == 1
    assert issue["actual"][0]["start_time"] == candidate.start_time
    assert issue["expected"]["min_duration_minutes"] == 60


def test_flexible_window_satisfied_by_one_alternative_without_changing_other_day():
    constraint = visit(windows=[window(), window(2)])
    assert validate_visit_constraints(requirements([constraint]), output(stop(day=2)), {}) == []
    missing = validate_visit_constraints(requirements([constraint]), output(), {})[0]
    assert missing["related_days"] == [1, 2]
    assert missing["required_days"] == []
    assert "day" not in missing  # Do not require the model to rewrite both alternatives.


def test_resolved_query_uses_exact_id_not_similar_name():
    constraint = visit(pid=None, place_query="馆的完整名称")
    plan = output(stop(pid="resolved-poi"))
    assert (
        validate_visit_constraints(requirements([constraint]), plan, {"a": {"id": "resolved-poi"}})
        == []
    )
    assert validate_visit_constraints(requirements([constraint]), plan, {"a": "other-poi"})


def test_fixed_appointment_needs_booking_kind_and_exact_window():
    constraint = visit(windows=[window(2)], fixed=True)
    req = requirements([constraint])
    wrong_day = validate_visit_constraints(req, output(stop()), {})[0]
    assert wrong_day["day"] == 2 and wrong_day["required_days"] == [1, 2]
    assert validate_visit_constraints(req, output(stop(day=2, end="12:00")), {})
    assert validate_visit_constraints(req, output(stop(day=2, kind="booking")), {})
    assert (
        validate_visit_constraints(req, output(stop(day=2, kind="booking", end="12:00")), {}) == []
    )


def fact(**changes):
    values = {
        "place_id": "poi-a",
        "day": 1,
        "field": "opening_hours",
        "status": "found",
        "value": "Reported opening: 10:00–12:00",
        "quote": "10:00–12:00",
        "source_ids": [1],
    }
    return ResearchFact.model_validate(values | changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"quote": ""},
        {"source_ids": []},
        {"source_ids": [0]},
        {"windows": [window(2).model_dump()]},
        {"closed": True, "windows": [window().model_dump()]},
        {"field": "reservation_rules", "closed": False},
        {"field": "last_admission", "windows": [window().model_dump()]},
        {"field": "opening_hours", "last_admission": "11:00"},
    ],
)
def test_fact_evidence_and_typed_field_scope_are_enforced(changes):
    with pytest.raises(ValidationError):
        fact(**changes)


def test_unknown_research_remains_unknown_without_inferred_values():
    unknown = ResearchFact(place_id="poi-a", day=1, field="opening_hours", status="unknown")
    assert unknown.value == "" and unknown.windows == []
    assert unknown.closed is None and unknown.last_admission is None
    with pytest.raises(ValidationError, match="未知事实"):
        ResearchFact(**(unknown.model_dump() | {"closed": False}))
    assert ResearchReport(facts=[unknown]).facts[0].status == "unknown"


def test_prose_and_unknown_facts_do_not_become_program_opening_windows():
    plan = output(stop(start="15:00", end="16:00"))
    assert validate_research_facts(plan, [fact().model_dump()]) == []
    assert (
        validate_research_facts(
            plan, [{"place_id": "poi-a", "day": 1, "status": "unknown", "closed": True}]
        )
        == []
    )


def test_reported_windows_check_complete_stay_with_original_source_refs():
    reported = fact(windows=[window().model_dump()]).model_dump()
    reported.update(sources=[{"url": "https://venue.example/hours"}], queried_at="fixture-time")
    plan = output(stop(kind="meal"), stop(start="11:00", end="12:01"))
    before = deepcopy(reported)
    issues = validate_research_facts(plan, [reported])
    assert len(issues) == 1
    issue = issues[0]
    assert issue["category"] == "draft_error" and issue["evidence_status"] == "web_reported"
    assert issue["code"] == "reported_opening_window" and issue["stop_number"] == 2
    assert issue["source_ids"] == [1] and issue["sources"] == reported["sources"]
    assert issue["queried_at"] == "fixture-time"
    assert reported == before
    assert validate_research_facts(output(stop(end="12:00")), [reported]) == []


def test_closed_and_last_admission_are_draft_errors_not_impossibility_proofs():
    closed = fact(closed=True).model_dump()
    assert validate_research_facts(output(stop()), [closed])[0]["code"] == "reported_closed"
    latest = fact(field="last_admission", last_admission="10:00").model_dump()
    assert validate_research_facts(output(stop()), [latest]) == []
    issue = validate_research_facts(output(stop(start="10:01", end="11:01")), [latest])[0]
    assert issue["code"] == "reported_last_admission"
    assert issue["category"] == "draft_error"
    assert validate_research_facts(output(stop(day=2)), [closed, latest]) == []
    assert validate_research_facts(output(stop(pid="another-poi")), [closed, latest]) == []
