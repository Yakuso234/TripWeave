import pytest
from fastapi.testclient import TestClient

from tripweave.app import create_app
from tripweave.domain import Requirements, fixture_plan, validate_plan
from tripweave.planner import plan


@pytest.mark.asyncio
async def test_real_framework_tool_feedback_and_retry_limit():
    req = Requirements(date="2026-11-07")
    result = await plan(req)
    assert result["status"] == "ready"
    assert result["usage"]["requests"] == 3
    assert [t["event"] for t in result["trace"]] == [
        "tool",
        "fixture_fault",
        "validation",
        "validation",
    ]
    failed = await plan(req, always_invalid=True)
    assert failed["status"] == "conflict" and failed["error"]


@pytest.mark.asyncio
async def test_late_departure_conflict_and_cancelled_booking():
    impossible = await plan(Requirements(date="2026-11-07", start_time="14:30"))
    assert impossible["status"] == "conflict"
    cancelled = await plan(Requirements(date="2026-11-07", booking_query=""))
    assert cancelled["status"] == "ready"
    assert not any(s["reason"] == "保留用户填写的固定预约" for s in cancelled["itinerary"]["stops"])


def test_unknown_place_and_dropped_booking_are_detected():
    req = Requirements(date="2026-11-07")
    output = fixture_plan(req)
    output.stops[-1].place_id = "invented"
    assert {i["code"] for i in validate_plan(req, output)} >= {"unknown_place", "booking"}


def test_full_api_versions_restore_and_pages(tmp_path):
    path = str(tmp_path / "test.db")
    client = TestClient(create_app(path))
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    req = Requirements(date="2026-11-07").model_dump(mode="json")
    trip = client.post("/api/trips", json=req).json()
    url = "/api/trips/" + trip["id"]
    first = client.post(url + "/plan", json={}).json()
    accept = url + "/candidates/" + first["id"] + "/accept"
    assert client.post(accept).json()["accepted_version"] == 1
    assert client.post(accept).json()["accepted_version"] == 1
    stale = client.post(url + "/plan", json={}).json()
    req["start_time"] = "10:00"
    assert (
        client.put(url + "/requirements", json={**req, "expected_revision": 1}).status_code == 200
    )
    assert (
        client.put(url + "/requirements", json={**req, "expected_revision": 1}).status_code == 409
    )
    assert client.post(url + "/candidates/" + stale["id"] + "/accept").status_code == 409
    revised = client.post(url + "/plan", json={}).json()
    assert revised["itinerary"]["stops"][-1]["start_time"] == "14:00"
    assert client.post(url + "/candidates/" + revised["id"] + "/accept").status_code == 200
    restored = TestClient(create_app(path)).get(url).json()
    assert restored["accepted_version"] == 2 and restored["requirements"]["start_time"] == "10:00"
    assert client.post(url + "/plan", json={"mode": "live"}).status_code == 422
    assert (
        client.post("/api/trips", json=req, headers={"origin": "https://other.test"}).status_code
        == 403
    )
