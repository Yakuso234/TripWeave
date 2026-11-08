"""API acceptance tests; HTTP transport is simulated, planner and DB are real code."""

import httpx2
from fastapi.testclient import TestClient
from test_real_planner import handler

import tripweave.app as application
from tripweave.domain import Requirements
from tripweave.real_planner import run_plan


def test_missing_key_blocks_instead_of_returning_demo(tmp_path, monkeypatch):
    monkeypatch.setenv("AMAP_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    client = TestClient(application.create_app(str(tmp_path / "test.db")))
    config = client.get("/api/config").json()
    assert config["supported_modes"] == []
    assert not config["map_configured"]
    assert "api_key" not in config
    trip = client.post("/api/trips", json={"date": "2026-11-07"}).json()
    response = client.post("/api/trips/" + trip["id"] + "/plan", json={})
    assert response.status_code == 422 and "AMAP_API_KEY" in response.json()["detail"]
    assert client.get("/api/catalog").json()["hotels"] == []
    assert client.post("/api/hotels/preview", json={"date": "2026-11-07"}).status_code == 422


def test_full_api_versions_restore_and_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("AMAP_API_KEY", "test-map-key-not-real")

    async def tested_planner(*args, **kwargs):
        return await run_plan(*args, **kwargs, transport=httpx2.MockTransport(handler))

    monkeypatch.setattr(application, "run_plan", tested_planner)
    path = str(tmp_path / "test.db")
    client = TestClient(application.create_app(path))
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    req = Requirements(date="2026-11-07", days=2).model_dump(mode="json")
    trip = client.post("/api/trips", json=req).json()
    url = "/api/trips/" + trip["id"]
    first = client.post(url + "/plan", json={}).json()
    assert first["status"] == "ready"
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
    assert any(
        s["kind"] == "booking" and s["start_time"] == "14:00" for s in revised["itinerary"]["stops"]
    )
    assert client.post(url + "/candidates/" + revised["id"] + "/accept").status_code == 200
    restored = TestClient(application.create_app(path)).get(url).json()
    assert restored["accepted_version"] == 2 and restored["requirements"]["start_time"] == "10:00"
    history = client.get(url + "/history").json()
    assert len(history["candidates"]) == 3
    assert history["candidates"][0]["id"] == revised["id"]
    assert history["candidates"][-1]["requirements"]["start_time"] == "09:00"
    assert sum(e["kind"] == "candidate_accepted" for e in history["events"]) == 2
    assert client.get("/api/trips/missing/history").status_code == 404
    assert client.post(url + "/plan", json={"mode": "replay"}).status_code == 422
    assert (
        client.post("/api/trips", json=req, headers={"origin": "https://other.test"}).status_code
        == 403
    )
