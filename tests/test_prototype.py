"""API acceptance tests; HTTP transport is simulated, planner and DB are real code."""

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_real_planner import handler

import tripweave.app as application
import tripweave.attraction_picker as picker
from tripweave.domain import Requirements
from tripweave.real_planner import run_plan


def test_attraction_draft_survives_restart_and_is_bound_to_trip_revision(tmp_path, monkeypatch):
    database = str(tmp_path / "attractions.db")
    monkeypatch.setenv("AMAP_API_KEY", "fixture-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    discovery_calls = []
    ranking_calls = []
    research_calls = []
    options = [
        {
            "id": f"p{i}",
            "name": f"测试景点{i}",
            "address": "测试地址",
            "area": "测试区",
            "photos": [f"https://photos.example/{i}/{n}.jpg" for n in range(3)],
        }
        for i in range(12)
    ]

    async def discover(req, maps):
        discovery_calls.append(req.city)
        return options

    async def rank(req, candidates, settings, client):
        ranking_calls.append([place["id"] for place in candidates])
        return [
            {**place, "experience_group": place["id"], "experience_members": [place["id"]]}
            for place in candidates
        ], None

    async def research(req, candidates, settings, client):
        research_calls.append([place["id"] for place in candidates])
        for place in candidates:
            place["web_summary"] = "了解城市历史，适合慢游。"
            place["audience"] = "喜欢历史的人"
            place["web_sources"] = [{"title": "景点介绍", "url": "https://museum.example/visit"}]

    monkeypatch.setattr(application, "discover_candidates", discover)
    monkeypatch.setattr(application, "rank_candidates", rank)
    monkeypatch.setattr(picker, "research_attractions", research)
    with TestClient(application.create_app(database)) as client:
        trip = client.post("/api/trips", json={"date": "2026-11-10"}).json()
        base = f"/api/trips/{trip['id']}"
        endpoint = base + "/attractions/1"
        assert client.get(endpoint).json() is None
        first = client.post(endpoint).json()
        assert first["selection_revision"] == 1 and first["selected_ids"] == []
        assert [place["id"] for place in first["options"]] == [f"p{i}" for i in range(5)]
        assert all(len(place["photos"]) == 3 for place in first["options"])
        assert first["remaining_count"] == 7 and not first["exhausted"]
        payload = {"selection_revision": 1, "selected_ids": ["p1"]}
        # A known candidate that has not been shown is not selectable yet.
        assert client.put(endpoint, json={**payload, "selected_ids": ["p9"]}).status_code == 422
        saved = client.put(endpoint, json=payload).json()
        assert saved["selection_revision"] == 2 and saved["selected_ids"] == ["p1"]
        assert client.put(endpoint, json={**payload, "selected_ids": []}).status_code == 409
        second = client.post(endpoint + "/next", json={"selection_revision": 2}).json()
        assert second["selection_revision"] == 3
        assert [place["id"] for place in second["options"]] == [f"p{i}" for i in range(5, 10)]
        assert second["selected_ids"] == ["p1"]
        assert second["remaining_count"] == 2
        # A choice from a prior batch remains valid after changing the visible batch.
        saved = client.put(
            endpoint, json={"selection_revision": 3, "selected_ids": ["p1", "p7"]}
        ).json()
        assert saved["selection_revision"] == 4 and saved["selected_ids"] == ["p1", "p7"]
        assert client.post(endpoint + "/next", json={"selection_revision": 2}).status_code == 409

    with TestClient(application.create_app(database)) as client:
        assert client.get(endpoint).json() == saved
        assert client.post(endpoint).json() == saved
        assert discovery_calls == ["上海"] and len(ranking_calls) == 1
        last = client.post(endpoint + "/next", json={"selection_revision": 4}).json()
        assert last["selection_revision"] == 5 and last["exhausted"]
        assert [place["id"] for place in last["options"]] == ["p10", "p11"]
        assert last["selected_ids"] == ["p1", "p7"]
        assert len(set(last["seen_ids"])) == len(last["seen_ids"]) == 12
        assert client.post(endpoint + "/next", json={"selection_revision": 5}).json() == last
        assert client.get(endpoint).json() == last
        assert research_calls == [
            [f"p{i}" for i in range(start, end)] for start, end in [(0, 5), (5, 10), (10, 12)]
        ]
        assert discovery_calls == ["上海"] and len(ranking_calls) == 1
        change = {**trip["requirements"], "date": "2026-11-11", "expected_revision": 1}
        assert client.put(base + "/requirements", json=change).status_code == 200
        assert client.get(endpoint).status_code == 409
        assert client.post(endpoint + "/next", json={"selection_revision": 5}).status_code == 409
        assert client.get(base + "/attractions/2").json() is None


@pytest.mark.parametrize("old_selected", [["child"], ["child", "main"]])
def test_old_picker_is_regrouped_with_preserved_choices_before_showing_new_batch(
    tmp_path, monkeypatch, old_selected
):
    monkeypatch.setenv("AMAP_API_KEY", "fixture-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    places = [{"id": "main", "name": "主体景点测试"}] + [
        {"id": f"p{i}", "name": f"其他景点{i}"} for i in range(7)
    ]

    async def discover(*args):
        return places

    async def rank(req, candidates, *args):
        assert {p.id for p in req.selected_attractions} == set(old_selected)
        assert "child" in {p["id"] for p in candidates}
        return [
            {
                **p,
                "experience_group": p["id"],
                "experience_members": (["main", "child"] if p["id"] == "main" else [p["id"]]),
            }
            for p in places
        ], None

    async def research(*args):
        return None

    monkeypatch.setattr(application, "discover_candidates", discover)
    monkeypatch.setattr(application, "rank_candidates", rank)
    monkeypatch.setattr(picker, "research_attractions", research)
    app = application.create_app(str(tmp_path / "upgrade.db"))
    with TestClient(app) as client:
        trip = client.post("/api/trips", json={"date": "2026-11-20"}).json()
        path = f"/api/trips/{trip['id']}/attractions/1"
        app.state.store.create_attraction_draft(
            trip["id"],
            1,
            {
                "picker_version": 2,
                "options": [{"id": "child", "name": "主体的建筑群测试"}, places[0]],
                "selected_ids": old_selected,
            },
        )
        assert client.get(path).json() is None
        saved = client.post(path).json()
        assert saved["picker_version"] == picker.PICKER_VERSION
        assert saved["selected_ids"] == old_selected
        assert saved["selection_revision"] == 2
        assert [p["id"] for p in saved["options"]] == [f"p{i}" for i in range(5)]
        assert "main" not in saved["seen_ids"] or "main" in old_selected
        assert all(p["experience_group"] == "main" for p in saved["selected_items"])
        assert "child" not in {p["id"] for p in saved["candidate_pool"]}
        if len(old_selected) == 2:
            duplicate = {"selection_revision": 2, "selected_ids": old_selected}
            assert client.put(path, json=duplicate).status_code == 422
        assert (
            client.put(path, json={"selection_revision": 2, "selected_ids": ["child"]}).status_code
            == 200
        )
        assert client.get(path).json()["selected_ids"] == ["child"]


def test_failed_regrouping_keeps_old_picker_unchanged_without_raw_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("AMAP_API_KEY", "fixture-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")

    async def discover(*args):
        return [{"id": "p", "name": "测试景点"}]

    async def rank(*args):
        raise picker.AttractionRankingError("景点主体分组无法核对，请重新查询。")

    monkeypatch.setattr(application, "discover_candidates", discover)
    monkeypatch.setattr(application, "rank_candidates", rank)
    app = application.create_app(str(tmp_path / "failed-upgrade.db"))
    with TestClient(app) as client:
        trip = client.post("/api/trips", json={"date": "2026-11-20"}).json()
        path = f"/api/trips/{trip['id']}/attractions/1"
        legacy = app.state.store.create_attraction_draft(
            trip["id"],
            1,
            {
                "picker_version": 2,
                "options": [{"id": "p", "name": "测试景点"}],
                "selected_ids": ["p"],
            },
        )
        response = client.post(path)
        assert response.status_code == 502 and "分组无法核对" in response.json()["detail"]
        assert app.state.store.get_attraction_draft(trip["id"], 1) == legacy


@pytest.mark.parametrize("group_ids", [["p0", "p0", "p0"], ["p0", "p0", "p2", "p2"]])
def test_legacy_duplicate_choices_can_be_removed_incrementally(tmp_path, group_ids):
    app = application.create_app(str(tmp_path / "remove-duplicates.db"))
    with TestClient(app) as client:
        trip = client.post("/api/trips", json={"date": "2026-11-20"}).json()
        path = f"/api/trips/{trip['id']}/attractions/1"
        places = [
            {"id": f"p{i}", "name": f"旧选择{i}", "experience_group": group}
            for i, group in enumerate(group_ids)
        ]
        selected = [p["id"] for p in places]
        draft = app.state.store.create_attraction_draft(
            trip["id"],
            1,
            {
                "picker_version": picker.PICKER_VERSION,
                "options": places,
                "candidate_pool": [],
                "selected_items": places,
                "selected_ids": selected,
                "seen_ids": selected,
            },
        )
        original = selected[:]
        while len(selected) > 1:
            selected = selected[1:]
            result = client.put(
                path,
                json={"selection_revision": draft["selection_revision"], "selected_ids": selected},
            )
            assert result.status_code == 200
            draft = result.json()
            assert (
                client.put(
                    path,
                    json={
                        "selection_revision": draft["selection_revision"],
                        "selected_ids": original,
                    },
                ).status_code
                == 422
            )
        assert client.get(path).json()["selected_ids"] == selected


def test_missing_recommendation_model_does_not_consume_map_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("AMAP_API_KEY", "fixture-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("LLM_API_KEY", "")

    async def discover(*args):
        raise AssertionError("missing model configuration must fail before a map lookup")

    monkeypatch.setattr(application, "discover_candidates", discover)
    with TestClient(application.create_app(str(tmp_path / "preflight.db"))) as client:
        result = client.post("/api/attractions/preview", json={"date": "2026-11-20"})
        assert result.status_code == 422 and "DeepSeek" in result.json()["detail"]


def test_long_term_preferences_survive_restart_without_rewriting_trips(tmp_path):
    database = str(tmp_path / "profile.db")
    with TestClient(application.create_app(database)) as client:
        trip = client.post("/api/trips", json={"date": "2026-11-07"}).json()
        endpoint = "/api/preferences"
        profile = client.get(endpoint).json()
        assert profile["revision"] == 0
        values = {**profile["values"], "pace": "relaxed", "custom_pace": "午饭后休息一小时"}
        saved = client.put(endpoint, json={"expected_revision": 0, "values": values})
        assert saved.json() == {"revision": 1, "values": values}
        assert (
            client.put(endpoint, json={"expected_revision": 0, "values": values}).status_code == 409
        )

    with TestClient(application.create_app(database)) as client:
        assert client.get(endpoint).json() == saved.json()
        assert client.get(f"/api/trips/{trip['id']}").json()["requirements"]["pace"] == "balanced"
        updated = client.put(
            endpoint, json={"expected_revision": 1, "values": {**values, "pace": "full"}}
        )
        assert updated.json()["revision"] == 2
        assert client.get(endpoint).json()["values"]["pace"] == "full"


def test_browser_keys_are_local_and_never_returned(tmp_path, monkeypatch):
    monkeypatch.setenv("AMAP_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com")
    origin = "http://127.0.0.1"
    with TestClient(
        application.create_app(str(tmp_path / "keys.db")),
        base_url=origin,
        client=("127.0.0.1", 43210),
    ) as client:
        endpoint = "/api/config/keys"
        secret = "sample-not-a-real-secret"
        payload = {"amap_api_key": secret}
        assert client.put(endpoint, json=payload).status_code == 403
        headers = {"Origin": origin}
        invalid = client.put(endpoint, json={"api_key": [secret]}, headers=headers)
        assert invalid.status_code == 422 and secret not in invalid.text
        response = client.put(endpoint, json=payload, headers=headers)
        assert response.status_code == 200 and secret not in response.text
        assert response.json()["supported_modes"] == []
        response = client.put(endpoint, json={"api_key": secret}, headers=headers)
        assert response.json()["supported_modes"] == ["deepseek"]
        assert secret not in client.get("/api/config").text


def test_missing_key_blocks_instead_of_returning_demo(tmp_path, monkeypatch):
    monkeypatch.setenv("AMAP_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    client = TestClient(application.create_app(str(tmp_path / "test.db")))
    config = client.get("/api/config").json()
    assert config["supported_modes"] == []
    assert not config["map_configured"]
    assert "api_key" not in config
    assert config["defaults"]["booking_query"] == ""
    trip = client.post("/api/trips", json={"date": "2026-11-07"}).json()
    assert trip["requirements"]["booking_query"] == ""
    response = client.post("/api/trips/" + trip["id"] + "/plan", json={})
    assert response.status_code == 422 and "AMAP_API_KEY" in response.json()["detail"]
    assert client.get("/api/catalog").json()["hotels"] == []
    assert client.post("/api/hotels/preview", json={"date": "2026-11-07"}).status_code == 422


def test_full_api_versions_restore_and_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("AMAP_API_KEY", "test-map-key-not-real")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-model-key-not-real")

    previous_inputs = []

    async def tested_planner(
        req,
        *,
        mode,
        settings,
        previous_plan,
        research_context=None,
        on_draft=None,
        continuation=None,
    ):
        previous_inputs.append(previous_plan)
        assert mode == "deepseek"
        assert continuation is None
        return await run_plan(
            req, "basic", settings, previous_plan, transport=httpx2.MockTransport(handler)
        )

    monkeypatch.setattr(application, "run_plan", tested_planner)
    path = str(tmp_path / "test.db")
    client = TestClient(application.create_app(path))
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    req = Requirements(date="2026-11-07", days=2, booking_query="上海博物馆").model_dump(
        mode="json"
    )
    trip = client.post("/api/trips", json=req).json()
    url = "/api/trips/" + trip["id"]
    first = client.post(url + "/plan", json={}).json()
    assert first["status"] == "ready" and first["mode"] == "deepseek"
    assert client.post(url + "/plan", json={"mode": "basic"}).status_code == 422
    accept = url + "/candidates/" + first["id"] + "/accept"
    assert client.post(accept).json()["accepted_version"] == 1
    assert client.post(accept).json()["accepted_version"] == 1
    revision = {"source_plan_id": first["id"], "revision_notes": "保留博物馆"}
    assert (
        client.post(url + "/plan", json={**revision, "source_plan_id": "missing"}).status_code
        == 404
    )
    stale = client.post(url + "/plan", json=revision).json()
    assert stale["source_plan_id"] == first["id"]
    assert previous_inputs[-1]["revision_request"] == "保留博物馆"
    assert previous_inputs[-1]["itinerary"] == first["itinerary"]
    assert client.get(url).json()["accepted_plan"]["id"] == first["id"]
    req["start_time"] = "10:00"
    assert (
        client.put(url + "/requirements", json={**req, "expected_revision": 1}).status_code == 200
    )
    assert (
        client.put(url + "/requirements", json={**req, "expected_revision": 1}).status_code == 409
    )
    assert client.post(url + "/candidates/" + stale["id"] + "/accept").status_code == 409
    assert client.post(url + "/plan", json={"expected_requirements_revision": 1}).status_code == 409
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


def test_custom_trip_requirements():
    req = Requirements(date="2026-11-07", days=5, nights=2, must_visit=["外滩"])
    assert req.hotel_nights == 2
    assert Requirements(date="2026-11-07", days=5).hotel_nights == 4
    with pytest.raises(ValidationError, match="住宿晚数"):
        Requirements(date="2026-11-07", days=4, nights=4)
