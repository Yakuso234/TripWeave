"""Server-owned handoff, real local DB, no real supplier requests."""

import pytest
from fastapi.testclient import TestClient

import tripweave.app as application


def draft():
    return {
        "picker_version": 3,
        "selection_revision": 1,
        "selected_ids": ["p0"],
        "queried_at": "2026-11-13T03:00:00+00:00",
        "selected_items": [],
        "options": [],
        "seen_ids": ["p0", "p2"],
        "candidate_pool": [
            {
                "id": "p0",
                "name": "测试馆",
                "experience_group": "p0",
                "experience_members": ["p0", "alias"],
                "web_summary": "所选景点的城市历史介绍",
                "audience": "喜欢人文慢游的人",
                "recommendation_reason": "符合本次兴趣",
                "web_sources": [
                    {
                        "title": "馆方介绍",
                        "url": "https://museum.example/intro",
                        "excerpt": "RAW_PRIVATE_EXCERPT",
                    }
                ],
                "photos": ["https://photos.example/NEVER_SEND.jpg"],
            },
            {
                "id": "p2",
                "name": "未选地点",
                "experience_group": "p2",
                "experience_members": ["p2"],
                "web_summary": "UNSELECTED_DESCRIPTION",
                "audience": "未选人群",
            },
        ],
    }


def seeded_app(tmp_path):
    app = application.create_app(str(tmp_path / "handoff.db"))
    client = TestClient(app)
    trip = client.post("/api/trips", json={"date": "2026-11-21", "must_visit": ["手填必去"]}).json()
    app.state.store.create_attraction_draft(trip["id"], 1, draft())
    return app, client, trip


def test_confirmation_atomically_freezes_evidence_and_only_server_data_reaches_planning(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AMAP_API_KEY", "fixture-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    received = []

    async def plan(req, **kwargs):
        received.append(kwargs["research_context"])
        return {"status": "conflict", "itinerary": None, "error": "模拟规划结果"}

    monkeypatch.setattr(application, "run_plan", plan)
    app, client, trip = seeded_app(tmp_path)
    base = f"/api/trips/{trip['id']}"
    confirm = base + "/attractions/1/confirm"
    assert client.post(confirm, json={"selection_revision": 9}).status_code == 409
    payload = {
        "selection_revision": 1,
        "research_evidence": {"summary": "CLIENT_FORGED"},
        "selected_attractions": [{"id": "invented", "name": "伪造"}],
    }
    saved = client.post(confirm, json=payload).json()
    assert saved["requirements_revision"] == 2
    assert saved["requirements"]["selected_attractions"] == [{"id": "p0", "name": "测试馆"}]
    assert saved["requirements"]["must_visit"] == ["手填必去"]
    evidence = saved["research_evidence"]
    assert (
        evidence["source_requirements_revision"],
        evidence["selection_revision"],
        evidence["requirements_revision"],
    ) == (1, 1, 2)
    assert evidence["selected"][0]["summary"] == "所选景点的城市历史介绍"
    # Retry after a lost HTTP response is idempotent and doesn't create v3.
    assert client.post(confirm, json=payload).json() == saved
    assert (
        client.post(base + "/plan", json={"expected_requirements_revision": 2}).status_code == 200
    )
    assert received == [evidence]
    assert client.get(base + "/history").json()["candidates"][0]["research_evidence"] == evidence
    app.state.store.dispose()

    with TestClient(application.create_app(str(tmp_path / "handoff.db"))) as restarted:
        restored = restarted.get(base).json()
        assert restored["research_evidence"] == evidence
        unchanged_identity = {
            **restored["requirements"],
            "notes": "出门晚一点",
            "expected_revision": 2,
        }
        updated = restarted.put(base + "/requirements", json=unchanged_identity).json()
        assert updated["research_evidence"]["requirements_revision"] == 3
        assert updated["research_evidence"]["source_requirements_revision"] == 1
        moved = {**updated["requirements"], "city": "杭州", "expected_revision": 3}
        assert restarted.put(base + "/requirements", json=moved).json()["research_evidence"] is None


def test_confirmation_rolls_back_both_draft_reservation_and_trip_when_event_fails(
    tmp_path, monkeypatch
):
    app, client, trip = seeded_app(tmp_path)
    initial = app.state.store.get_attraction_draft(trip["id"], 1)

    def fail(*args, **kwargs):
        raise RuntimeError("simulated event write failure")

    monkeypatch.setattr(app.state.store, "_event", fail)
    with pytest.raises(RuntimeError, match="event write failure"):
        app.state.store.confirm_attractions(trip["id"], 1, 1)
    assert app.state.store.get_trip(trip["id"]) == trip
    assert app.state.store.get_attraction_draft(trip["id"], 1) == initial
    client.close()


def test_invalid_stored_evidence_is_reported_before_any_model_request(tmp_path, monkeypatch):
    monkeypatch.setenv("AMAP_API_KEY", "fixture-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")

    async def forbidden(*args, **kwargs):
        raise AssertionError("invalid evidence must fail before supplier calls")

    monkeypatch.setattr(application, "run_plan", forbidden)
    app, client, trip = seeded_app(tmp_path)
    base = f"/api/trips/{trip['id']}"
    assert (
        client.post(base + "/attractions/1/confirm", json={"selection_revision": 1}).status_code
        == 200
    )
    with app.state.store._transaction() as connection:
        old, saved = app.state.store._read(connection, trip["id"])
        saved["research_evidence"]["requirements_revision"] = 99
        app.state.store._replace(connection, old, saved)
    result = client.post(base + "/plan", json={"expected_requirements_revision": 2})
    assert result.status_code == 409 and "资料与当前需求不一致" in result.json()["detail"]
    client.close()
