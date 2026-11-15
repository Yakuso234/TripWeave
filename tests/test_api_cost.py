"""Deterministic fee arithmetic and boundaries, never a bill or paid provider test."""

from copy import deepcopy
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from tripweave.api_cost import TARIFF, candidate_cost_view, estimate_cost, period
from tripweave.app import create_app
from tripweave.storage import Store


def row(**changes):
    return {
        "number": 1,
        "role": "planner",
        "model": "deepseek-flash",
        "started_at": "2026-11-15T05:00:00+00:00",
        "input_tokens": 1000,
        "output_tokens": 100,
        "cache_read_tokens": 400,
        "cache_miss_tokens": 600,
        "reasoning_tokens": 80,
        **changes,
    }


def test_exact_decimal_cost_counts_reasoning_and_cache_only_once():
    cost = estimate_cost([row()], [True, False])
    assert cost["models"] == {
        "status": "estimated",
        "min_cny": "0.00100800",
        "max_cny": "0.00100800",
    }
    assert cost["search"]["min_cny"] == cost["search"]["max_cny"] == "0.00500000"
    assert cost["subtotal"]["min_cny"] == cost["subtotal"]["max_cny"] == "0.00600800"
    assert cost["model_calls"][0]["input_min_cny"] == "0.00060800"
    assert cost["model_calls"][0]["output_min_cny"] == "0.00040000"
    assert cost["tariff"]["version"] == TARIFF["version"]


@pytest.mark.parametrize(
    "timestamp,expected",
    [
        ("2026-11-16T00:59:59+00:00", "off_peak"),
        ("2026-11-16T01:00:00+00:00", "peak"),
        ("2026-11-16T04:00:00+00:00", "off_peak"),
        ("2026-11-16T06:00:00+00:00", "peak"),
        ("2026-11-16T10:00:00+00:00", "off_peak"),
        ("2026-10-01T02:00:00+00:00", "off_peak"),
        ("2027-11-15T02:00:00+00:00", None),
        ("2026-11-15T02:00:00", None),
    ],
)
def test_beijing_period_boundaries_holiday_and_missing_future_calendar(timestamp, expected):
    assert period(timestamp) == expected


def test_peak_and_pro_prices_are_applied_per_request():
    peak = row(started_at="2026-11-16T02:00:00+00:00")
    mixed = estimate_cost([peak, row(model="deepseek-v4-pro")], [])
    assert mixed["model_calls"][0]["min_cny"] == "0.00201600"
    assert mixed["model_calls"][1]["min_cny"] == "0.00411000"
    assert mixed["models"]["min_cny"] == "0.00612600"


def test_missing_split_or_time_returns_range_not_a_false_exact_amount():
    cost = estimate_cost([row(cache_read_tokens=None, cache_miss_tokens=None)], [])
    assert cost["models"] == {"status": "range", "min_cny": "0.00042000", "max_cny": "0.00140000"}
    cost = estimate_cost([row(started_at=None)], [])
    assert cost["models"]["min_cny"] == "0.00100800" and cost["models"]["max_cny"] == "0.00201600"


def test_one_cache_count_can_derive_other_without_guessing():
    assert estimate_cost([row(cache_miss_tokens=None)], [])["models"]["status"] == "estimated"
    assert estimate_cost([row(cache_read_tokens=None)], [])["models"]["min_cny"] == "0.00100800"


@pytest.mark.parametrize(
    "changes",
    [
        {"cache_read_tokens": 1001},
        {"cache_miss_tokens": 601},
        {"input_tokens": None},
        {"output_tokens": -1},
        {"model": "unknown-model"},
        {"cache_read_tokens": True},
    ],
)
def test_unknown_or_inconsistent_data_cannot_be_priced_as_zero(changes):
    cost = estimate_cost([row(**changes)], [])
    assert cost["models"]["status"] == cost["subtotal"]["status"] == "unavailable"
    assert "min_cny" not in cost["subtotal"]


def test_search_empty_failure_and_unknown_billing_are_separate():
    cost = estimate_cost([], [False, False, None, True])
    assert cost["search"]["confirmed_billable"] == 1 and cost["search"]["unknown_billable"] == 1
    assert cost["search"]["min_cny"] == "0.00500000" and cost["search"]["max_cny"] == "0.01000000"
    assert cost["subtotal"]["status"] == "range"
    assert estimate_cost([], [])["subtotal"]["min_cny"] == "0.00000000"
    assert estimate_cost([row()], [], official=False)["models"]["status"] == "unavailable"


def test_price_and_calculated_result_are_snapshots_not_mutable_global_references():
    cost = estimate_cost([row()], [])
    before = deepcopy(cost)
    cost["tariff"]["models"]["deepseek-flash"]["off_peak"][0] = "999"
    assert estimate_cost([row()], []) == before


def test_legacy_preview_is_conditional_current_price_range_and_does_not_rewrite_sqlite(tmp_path):
    database = str(tmp_path / "old.db")
    store = Store(database)
    store.initialize()
    trip = store.create_trip({"city": "上海"})
    saved = {
        "id": "old",
        "trip_id": trip["id"],
        "status": "conflict",
        "mode": "deepseek",
        "requirements_revision": 1,
        "base_version": 0,
        "model": "deepseek-flash",
        "usage": {"requests": 3, "input_tokens": 13228, "output_tokens": 3965},
        "research_search_calls": 1,
    }
    store.add_candidate(saved)
    store.dispose()
    with TestClient(create_app(database)) as client:
        view = client.get(f"/api/trips/{trip['id']}/history").json()["candidates"][0]
    cost = view["api_cost_preview"]
    assert cost["basis"] == "current_tariff_legacy_assumption"
    assert cost["subtotal"]["status"] == "range"
    assert Decimal(cost["subtotal"]["min_cny"]) == Decimal("0.01612456")
    assert Decimal(cost["subtotal"]["max_cny"]) == Decimal("0.06317600")
    reopened = Store(database)
    assert reopened.get_candidate(trip["id"], "old") == saved
    reopened.dispose()
    new = {
        **saved,
        "api_cost_estimate": {"tariff": {"version": "historic"}, "subtotal": {"min_cny": "1"}},
    }
    assert candidate_cost_view(new) == new
