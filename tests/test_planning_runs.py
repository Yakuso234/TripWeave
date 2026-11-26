"""Planning-run snapshots survive unfinished requests and never bypass acceptance."""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

import tripweave.app as application
from tripweave.storage import Store, StoreError


def draft(label="首轮暂定安排"):
    return {
        "summary": label,
        "hotel_id": None,
        "stops": [
            {
                "day": 1,
                "place_id": "p0",
                "start_time": "10:00",
                "end_time": "11:00",
                "kind": "activity",
                "reason": "开放时间及路线尚待核实。",
                "title": "测试景点",
            },
        ],
    }


def progress(label="首轮暂定安排"):
    return {
        "phase": "draft",
        "draft": draft(label),
        "query_requests": [
            {
                "type": "route",
                "origin_id": "o",
                "destination_id": "p0",
                "transport": "mixed",
                "reason": "核实第一段路程",
            }
        ],
        "pending_information": [],
        "assumptions": ["开放时间未核实，不宣称已经预约。"],
        "query_log": [],
        "places": [{"id": "o", "name": "测试起点"}, {"id": "p0", "name": "测试景点"}],
    }


def settings(monkeypatch):
    monkeypatch.setenv("AMAP_API_KEY", "fixture-map-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-model-key")


def create_trip(client):
    response = client.post("/api/trips", json={"date": "2026-11-21", "nights": 0})
    assert response.status_code == 200
    trip = response.json()
    return trip, f"/api/trips/{trip['id']}"


def test_draft_is_saved_immediately_and_immutable_run_bindings_cannot_be_overwritten(tmp_path):
    path = str(tmp_path / "runs.sqlite3")
    store = Store(path)
    store.initialize()
    trip = store.create_trip({"city": "上海", "date": "2026-11-21"})
    run = store.start_planning_run(trip["id"], 1, 0)
    store.save_planning_draft(
        trip["id"],
        run["id"],
        {
            **progress(),
            "id": "injected",
            "trip_id": "injected",
            "requirements_revision": 100,
            "base_version": 100,
            "status": "completed",
            "outcome": "ready",
            "candidate_id": "injected",
        },
    )
    store.dispose()
    restored = Store(path)
    restored.initialize()
    saved = restored.list_planning_runs(trip["id"])[0]
    assert saved["id"] == run["id"] and saved["trip_id"] == trip["id"]
    assert saved["requirements_revision"] == 1 and saved["base_version"] == 0
    assert (
        saved["status"] == "running" and saved["outcome"] is None and saved["candidate_id"] is None
    )
    assert saved["draft"] == draft() and saved["query_requests"] == progress()["query_requests"]
    assert saved["assumptions"] == progress()["assumptions"]
    assert restored.history(trip["id"])["candidates"] == []
    assert restored.get_trip(trip["id"])["accepted_version"] == 0
    restored.dispose()


def test_finished_run_rejects_late_draft_and_start_rejects_stale_business_version(tmp_path):
    store = Store(str(tmp_path / "runs.sqlite3"))
    store.initialize()
    trip = store.create_trip({"city": "上海", "date": "2026-11-21"})
    with pytest.raises(StoreError) as exc:
        store.start_planning_run(trip["id"], 2, 0)
    assert exc.value.status_code == 409
    run = store.start_planning_run(trip["id"], 1, 0)
    store.save_planning_draft(trip["id"], run["id"], progress())
    finished = store.finish_planning_run(trip["id"], run["id"])
    assert finished["status"] == "interrupted" and finished["draft"] == draft()
    with pytest.raises(StoreError) as exc:
        store.save_planning_draft(trip["id"], run["id"], progress("不应覆盖"))
    assert exc.value.status_code == 409
    assert store.finish_planning_run(trip["id"], run["id"]) == finished
    store.dispose()


@pytest.mark.parametrize("ready", [False, True])
def test_api_draft_callback_persists_before_completion_and_links_final_candidate(
    tmp_path, monkeypatch, ready
):
    settings(monkeypatch)
    database = str(tmp_path / "runs.sqlite3")
    app = application.create_app(database)
    observed = []

    async def simulated(*args, on_draft, **kwargs):
        await on_draft(progress())
        # This is read from another connection before the model run returns.
        reopened = Store(database)
        saved = reopened.list_planning_runs(trip["id"])[0]
        observed.append(saved)
        assert saved["draft"] == draft() and saved["status"] == "running"
        assert reopened.history(trip["id"])["candidates"] == []
        reopened.dispose()
        await on_draft({**progress("根据资料修改后的安排"), "phase": "revision"})
        return {
            "status": "ready" if ready else "conflict",
            "outcome": "ready" if ready else "incomplete",
            "itinerary": draft("根据资料修改后的安排") if ready else None,
            "draft": None if ready else draft("根据资料修改后的安排"),
            "query_log": [
                {"request": progress()["query_requests"][0], "result": {"status": "cached"}}
            ],
            "places": progress()["places"],
            "issues": [],
            "error": None,
            "review": {"status": "approved" if ready else "pending"},
        }

    monkeypatch.setattr(application, "run_plan", simulated)
    with TestClient(app) as client:
        trip, base = create_trip(client)
        response = client.post(base + "/plan", json={"expected_requirements_revision": 1})
        assert response.status_code == 200
        candidate = response.json()
        runs = client.get(base + "/planning-runs").json()["runs"]
        assert len(runs) == len(observed) == 1
        run = runs[0]
        assert run["id"] == candidate["planning_run_id"]
        assert run["candidate_id"] == candidate["id"]
        assert run["draft"] == draft("根据资料修改后的安排")
        assert run["status"] == ("completed" if ready else "incomplete")
        assert run["query_log"] == candidate["query_log"]
        assert app.state.store.finish_planning_run(trip["id"], run["id"], candidate) == run
        history = client.get(base + "/history").json()
        assert [item["id"] for item in history["candidates"]] == [candidate["id"]]
        assert sum(event["kind"] == "candidate_generated" for event in history["events"]) == 1
        assert client.get(base).json()["accepted_version"] == 0
        assert client.post(base + f"/candidates/{run['id']}/accept").status_code == 404
    with TestClient(application.create_app(database)) as restarted:
        assert restarted.get(base + "/planning-runs").json()["runs"] == runs
        assert restarted.get(base).json()["accepted_version"] == 0


def test_model_exception_keeps_first_draft_and_does_not_save_private_exception_body(
    tmp_path, monkeypatch
):
    settings(monkeypatch)

    async def simulated(*args, on_draft, **kwargs):
        await on_draft(progress())
        raise RuntimeError("PRIVATE-PROVIDER-RESPONSE-MUST-NOT-BE-STORED")

    monkeypatch.setattr(application, "run_plan", simulated)
    database = str(tmp_path / "interrupted.sqlite3")
    with TestClient(application.create_app(database), raise_server_exceptions=False) as client:
        _, base = create_trip(client)
        assert (
            client.post(base + "/plan", json={"expected_requirements_revision": 1}).status_code
            == 500
        )
        run = client.get(base + "/planning-runs").json()["runs"][0]
        assert run["status"] == "interrupted" and run["draft"] == draft()
        assert "PRIVATE-" not in str(run)
        assert client.get(base + "/history").json()["candidates"] == []
        assert client.get(base).json()["accepted_version"] == 0


def test_failed_run_finalization_does_not_publish_a_candidate_or_generation_event(
    tmp_path, monkeypatch
):
    settings(monkeypatch)
    replace_run = Store._replace_planning_run

    def reject_completion(self, connection, old_body, run):
        if run["status"] == "completed":
            raise StoreError(503, "模拟运行记录写入失败")
        return replace_run(self, connection, old_body, run)

    async def simulated(*args, on_draft, **kwargs):
        await on_draft(progress())
        return {
            "status": "ready",
            "outcome": "ready",
            "itinerary": draft("正式候选"),
            "review": {"status": "approved"},
            "issues": [],
        }

    monkeypatch.setattr(Store, "_replace_planning_run", reject_completion)
    monkeypatch.setattr(application, "run_plan", simulated)
    with TestClient(application.create_app(str(tmp_path / "atomic-result.sqlite3"))) as client:
        _, base = create_trip(client)
        assert client.post(base + "/plan", json={}).status_code == 503
        history = client.get(base + "/history").json()
        assert history["candidates"] == []
        assert all(event["kind"] != "candidate_generated" for event in history["events"])
        run = client.get(base + "/planning-runs").json()["runs"][0]
        assert run["status"] == "interrupted" and run["candidate_id"] is None
        assert run["draft"] == draft()


def test_run_listing_is_trip_scoped_and_missing_keys_do_not_create_execution(tmp_path, monkeypatch):
    monkeypatch.delenv("AMAP_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with TestClient(application.create_app(str(tmp_path / "scope.sqlite3"))) as client:
        first, _base = create_trip(client)
        _, other = create_trip(client)
        run = client.app.state.store.start_planning_run(first["id"], 1, 0)
        client.app.state.store.save_planning_draft(first["id"], run["id"], progress())
        assert client.get(other + "/planning-runs").json()["runs"] == []
        assert client.get("/api/trips/absent/planning-runs").status_code == 404
        assert (
            client.post(other + "/plan", json={"expected_requirements_revision": 1}).status_code
            == 422
        )
        assert client.get(other + "/planning-runs").json()["runs"] == []


def test_requirement_changes_keep_old_run_snapshot_and_block_old_candidate_acceptance(
    tmp_path, monkeypatch
):
    settings(monkeypatch)
    app = application.create_app(str(tmp_path / "stale.sqlite3"))

    async def simulated(*args, on_draft, **kwargs):
        await on_draft(progress())
        trip = app.state.store.get_trip(trip_id)
        requirements = deepcopy(trip["requirements"])
        requirements["notes"] = "新需求"
        app.state.store.update_requirements(trip_id, requirements, 1)
        return {
            "status": "ready",
            "outcome": "ready",
            "itinerary": draft(),
            "draft": None,
            "review": {"status": "approved"},
            "issues": [],
            "error": None,
        }

    monkeypatch.setattr(application, "run_plan", simulated)
    with TestClient(app) as client:
        trip, base = create_trip(client)
        trip_id = trip["id"]
        candidate = client.post(base + "/plan", json={"expected_requirements_revision": 1}).json()
        run = client.get(base + "/planning-runs").json()["runs"][0]
        assert run["requirements_revision"] == 1 and run["requirements"]["notes"] == ""
        assert client.get(base).json()["requirements_revision"] == 2
        assert client.post(base + f"/candidates/{candidate['id']}/accept").status_code == 409
        assert client.get(base).json()["accepted_version"] == 0
