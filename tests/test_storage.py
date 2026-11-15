"""Real SQLite transaction tests; no map/model network requests."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import select

from tripweave.storage import Store, StoreError, trips


def make_candidate(trip, **changes):
    return {
        "id": uuid4().hex,
        "trip_id": trip["id"],
        "status": "ready",
        "mode": "basic",
        "requirements": trip["requirements"],
        "requirements_revision": trip["requirements_revision"],
        "base_version": trip["accepted_version"],
        "itinerary": {"stops": []},
        **changes,
    }


@pytest.mark.parametrize(
    "outcome", ["needs_information", "needs_user_decision", "incomplete", "service_error"]
)
def test_nonready_workflow_outcome_cannot_be_accepted_even_with_legacy_ready_status(store, outcome):
    trip = store.create_trip({"city": "上海"})
    candidate = store.add_candidate(make_candidate(trip, outcome=outcome, draft={"stops": []}))
    with pytest.raises(StoreError, match="未通过检查"):
        store.accept_candidate(trip["id"], candidate["id"])
    assert store.get_trip(trip["id"])["accepted_version"] == 0


@pytest.fixture
def store(tmp_path):
    storage = Store(str(tmp_path / "test.sqlite3"))
    storage.initialize()
    yield storage
    storage.dispose()


def test_accept_history_and_restart_preserve_snapshots(tmp_path):
    path = str(tmp_path / "resume.sqlite3")
    storage = Store(path)
    storage.initialize()
    trip = storage.create_trip({"city": "上海", "start_time": "09:00"})
    first = storage.add_candidate(make_candidate(trip))
    assert storage.accept_candidate(trip["id"], first["id"])["accepted_version"] == 1
    assert storage.accept_candidate(trip["id"], first["id"])["accepted_version"] == 1
    revised = storage.update_requirements(trip["id"], {"city": "杭州"}, 1)
    second = storage.add_candidate(make_candidate(revised))
    storage.dispose()

    reopened = Store(path)
    reopened.initialize()
    saved = reopened.get_trip(trip["id"])
    assert saved["accepted_plan"]["requirements"]["city"] == "上海"
    assert saved["requirements"]["city"] == "杭州"
    history = reopened.history(trip["id"])
    assert [c["id"] for c in history["candidates"]] == [second["id"], first["id"]]
    accepted = [event for event in history["events"] if event["kind"] == "candidate_accepted"]
    assert len(accepted) == 1
    assert reopened.list_trips()[0]["id"] == trip["id"]
    reopened.dispose()


def test_requirement_and_candidate_conflicts_are_rejected(store):
    trip = store.create_trip({"city": "上海"})
    old = store.add_candidate(make_candidate(trip))
    store.update_requirements(trip["id"], {"city": "苏州"}, 1)
    for operation in (
        lambda: store.update_requirements(trip["id"], {"city": "杭州"}, 1),
        lambda: store.accept_candidate(trip["id"], old["id"]),
    ):
        with pytest.raises(StoreError) as failure:
            operation()
        assert failure.value.status_code == 409
    current = store.get_trip(trip["id"])
    invalid = store.add_candidate(make_candidate(current, status="conflict"))
    with pytest.raises(StoreError, match="未通过"):
        store.accept_candidate(trip["id"], invalid["id"])
    other_trip = store.create_trip({"city": "北京"})
    with pytest.raises(StoreError) as failure:
        store.accept_candidate(other_trip["id"], old["id"])
    assert failure.value.status_code == 404


def race_updates(store, monkeypatch, operations):
    original = store._replace
    barrier = Barrier(len(operations))

    def synchronized(connection, old_body, trip):
        barrier.wait(timeout=5)
        return original(connection, old_body, trip)

    monkeypatch.setattr(store, "_replace", synchronized)

    def attempt(operation):
        try:
            return operation()
        except StoreError as error:
            return error

    with ThreadPoolExecutor(max_workers=len(operations)) as executor:
        futures = [executor.submit(attempt, operation) for operation in operations]
        return [future.result(timeout=10) for future in futures]


def test_two_different_candidates_cannot_both_be_accepted(store, monkeypatch):
    trip = store.create_trip({"city": "上海"})
    first, second = [store.add_candidate(make_candidate(trip)) for _ in range(2)]
    outcomes = race_updates(
        store,
        monkeypatch,
        [
            lambda: store.accept_candidate(trip["id"], first["id"]),
            lambda: store.accept_candidate(trip["id"], second["id"]),
        ],
    )
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    failures = [item for item in outcomes if isinstance(item, StoreError)]
    assert [item.status_code for item in failures] == [409]
    saved = store.get_trip(trip["id"])
    assert saved["accepted_version"] == 1
    assert sum(e["kind"] == "candidate_accepted" for e in store.history(trip["id"])["events"]) == 1


def test_simultaneous_same_candidate_acceptance_is_idempotent(store, monkeypatch):
    trip = store.create_trip({"city": "上海"})
    candidate = store.add_candidate(make_candidate(trip))
    outcomes = race_updates(
        store,
        monkeypatch,
        [
            lambda: store.accept_candidate(trip["id"], candidate["id"]),
            lambda: store.accept_candidate(trip["id"], candidate["id"]),
        ],
    )
    assert [item["accepted_version"] for item in outcomes] == [1, 1]
    assert sum(e["kind"] == "candidate_accepted" for e in store.history(trip["id"])["events"]) == 1


def test_requirement_update_racing_acceptance_does_not_lose_versions(store, monkeypatch):
    trip = store.create_trip({"city": "上海"})
    candidate = store.add_candidate(make_candidate(trip))
    outcomes = race_updates(
        store,
        monkeypatch,
        [
            lambda: store.accept_candidate(trip["id"], candidate["id"]),
            lambda: store.update_requirements(trip["id"], {"city": "杭州"}, 1),
        ],
    )
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert [item.status_code for item in outcomes if isinstance(item, StoreError)] == [409]
    saved = store.get_trip(trip["id"])
    assert (saved["requirements_revision"], saved["accepted_version"]) in ((1, 1), (2, 0))
    assert len(store.history(trip["id"])["events"]) == 3


def test_history_write_failure_rolls_back_acceptance(store, monkeypatch):
    trip = store.create_trip({"city": "上海"})
    candidate = store.add_candidate(make_candidate(trip))

    def fail_event(*_args, **_kwargs):
        raise StoreError(503, "injected history write failure")

    monkeypatch.setattr(store, "_event", fail_event)
    with pytest.raises(StoreError):
        store.accept_candidate(trip["id"], candidate["id"])
    assert store.get_trip(trip["id"])["accepted_version"] == 0
    assert len(store.history(trip["id"])["events"]) == 2


def test_existing_sqlite_schema_and_json_are_not_rewritten(tmp_path):
    path = str(tmp_path / "legacy.sqlite3")
    trip = {
        "id": "legacy",
        "requirements": {"city": "上海"},
        "requirements_revision": 1,
        "accepted_version": 0,
        "accepted_plan": None,
    }
    candidate = make_candidate(trip)
    original_body = json.dumps(trip, ensure_ascii=False, indent=2)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE trips (id TEXT PRIMARY KEY, body TEXT NOT NULL);"
            "CREATE TABLE candidates (id TEXT PRIMARY KEY, trip_id TEXT NOT NULL, body TEXT NOT NULL);"
        )
        connection.execute("INSERT INTO trips VALUES (?, ?)", (trip["id"], original_body))
        connection.execute(
            "INSERT INTO candidates VALUES (?, ?, ?)",
            (
                candidate["id"],
                trip["id"],
                json.dumps(candidate),
            ),
        )
    storage = Store(path)
    storage.initialize()
    storage.initialize()
    with storage.engine.connect() as connection:
        assert connection.execute(select(trips.c.body)).scalar_one() == original_body
    assert storage.get_trip("legacy") == trip
    assert storage.history("legacy")["candidates"][0]["id"] == candidate["id"]
    assert storage.accept_candidate("legacy", candidate["id"])["accepted_version"] == 1
    storage.dispose()


def test_missing_trip_and_invalid_database_scheme_are_explicit(store):
    with pytest.raises(StoreError) as failure:
        store.history("missing")
    assert failure.value.status_code == 404
    with pytest.raises(StoreError, match="仅支持"):
        Store("mysql://user:password@localhost/db")
