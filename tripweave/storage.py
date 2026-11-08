"""Short SQLAlchemy transactions for trips, candidates and durable change history.

The two original TEXT-JSON tables remain readable. Compare-and-swap on the exact
old body protects both business versions without a SQLite-specific locking mode.
No model or map call belongs inside these transactions.
"""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
    func,
    select,
    update,
)
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool


class StoreError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


metadata = MetaData()
trips = Table(
    "trips", metadata, Column("id", Text, primary_key=True), Column("body", Text, nullable=False)
)
candidates = Table(
    "candidates",
    metadata,
    Column("id", Text, primary_key=True),
    Column("trip_id", Text, nullable=False),
    Column("body", Text, nullable=False),
)
trip_events = Table(
    "trip_events",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("trip_id", Text, ForeignKey("trips.id"), nullable=False),
    Column("candidate_id", Text),
    Column("kind", String(40), nullable=False),
    Column("body", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
)
candidate_index = Index("ix_candidates_trip_id", candidates.c.trip_id)
event_index = Index("ix_trip_events_trip_time", trip_events.c.trip_id, trip_events.c.created_at)
event_candidate_index = Index("ix_trip_events_candidate", trip_events.c.candidate_id)


def encode(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, database: str):
        if "://" not in database:
            if database == ":memory:":
                url = URL.create("sqlite", database=":memory:")
            else:
                path = Path(database).expanduser().resolve()
                path.parent.mkdir(parents=True, exist_ok=True)
                url = URL.create("sqlite", database=str(path))
        else:
            try:
                url = make_url(database)
            except SQLAlchemyError, ValueError:
                raise StoreError(503, "数据库连接配置格式无效") from None
        if url.drivername == "postgresql":
            url = url.set(drivername="postgresql+psycopg")
        if url.drivername not in ("sqlite", "sqlite+pysqlite", "postgresql+psycopg"):
            raise StoreError(503, "仅支持 SQLite 和 PostgreSQL（psycopg）数据库")
        self.backend = url.get_backend_name()
        options = {"pool_pre_ping": True, "hide_parameters": True}
        if self.backend == "sqlite":
            if url.database and url.database != ":memory:":
                Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            options["connect_args"] = {"timeout": 5, "check_same_thread": False}
            if not url.database or url.database == ":memory:":
                options["poolclass"] = StaticPool
        else:
            options["connect_args"] = {
                "connect_timeout": 5,
                "options": "-c lock_timeout=5000 -c statement_timeout=15000",
            }
            options.update(pool_size=5, max_overflow=5, pool_timeout=5)
        try:
            self.engine = create_engine(url, **options)
        except ImportError:
            raise StoreError(503, "数据库驱动未安装，请安装对应数据库可选依赖") from None
        if self.backend == "sqlite":

            @event.listens_for(self.engine, "connect")
            def enable_foreign_keys(connection, _):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    def initialize(self) -> None:
        """Add missing tables/indexes only; never replace or rewrite legacy data."""
        with self._transaction() as connection:
            metadata.create_all(connection)
            # create_all does not add new indexes on an already existing table.
            for index in (candidate_index, event_index, event_candidate_index):
                index.create(connection, checkfirst=True)

    def dispose(self) -> None:
        self.engine.dispose()

    @contextmanager
    def _transaction(self):
        try:
            with self.engine.begin() as connection:
                yield connection
        except SQLAlchemyError:
            # Do not expose SQL parameters (private itineraries), host or credentials.
            raise StoreError(503, "数据库暂不可用，请稍后重试；未确认的修改不会视为成功") from None

    @staticmethod
    def _read(connection, trip_id: str) -> tuple[str, dict]:
        body = connection.execute(
            select(trips.c.body).where(trips.c.id == trip_id)
        ).scalar_one_or_none()
        if body is None:
            raise StoreError(404, "行程不存在")
        return body, json.loads(body)

    @staticmethod
    def _event(connection, trip_id, kind, body, candidate_id=None):
        connection.execute(
            trip_events.insert().values(
                id=uuid4().hex,
                trip_id=trip_id,
                kind=kind,
                body=encode(body),
                candidate_id=candidate_id,
                created_at=now(),
            )
        )

    @staticmethod
    def _replace(connection, old_body, trip):
        result = connection.execute(
            update(trips)
            .where(trips.c.id == trip["id"], trips.c.body == old_body)
            .values(body=encode(trip))
        )
        if result.rowcount != 1:
            raise StoreError(409, "行程已被其他操作更新，请刷新后重试")

    def create_trip(self, requirements: dict) -> dict:
        timestamp = now()
        trip = {
            "id": uuid4().hex,
            "requirements": requirements,
            "requirements_revision": 1,
            "accepted_version": 0,
            "accepted_plan": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        with self._transaction() as connection:
            connection.execute(trips.insert().values(id=trip["id"], body=encode(trip)))
            self._event(
                connection,
                trip["id"],
                "trip_created",
                {
                    "requirements_revision": 1,
                    "requirements": requirements,
                },
            )
        return trip

    def get_trip(self, trip_id: str) -> dict:
        with self._transaction() as connection:
            return self._read(connection, trip_id)[1]

    def list_trips(self) -> list[dict]:
        latest = (
            select(trip_events.c.trip_id, func.max(trip_events.c.created_at).label("last_change"))
            .group_by(trip_events.c.trip_id)
            .subquery()
        )
        query = (
            select(trips.c.body)
            .outerjoin(latest, trips.c.id == latest.c.trip_id)
            .order_by(latest.c.last_change.desc().nulls_last(), trips.c.id.desc())
            .limit(20)
        )
        with self._transaction() as connection:
            bodies = connection.execute(query).scalars().all()
        return [
            {
                key: trip[key]
                for key in ("id", "requirements", "requirements_revision", "accepted_version")
            }
            for trip in map(json.loads, bodies)
        ]

    def update_requirements(self, trip_id: str, requirements: dict, expected_revision: int) -> dict:
        with self._transaction() as connection:
            old_body, trip = self._read(connection, trip_id)
            if expected_revision != trip["requirements_revision"]:
                raise StoreError(409, "需求已更新，请刷新后重试")
            trip["requirements"] = requirements
            trip["requirements_revision"] += 1
            trip["updated_at"] = now()
            self._replace(connection, old_body, trip)
            self._event(
                connection,
                trip_id,
                "requirements_updated",
                {
                    "requirements_revision": trip["requirements_revision"],
                    "requirements": requirements,
                },
            )
        return trip

    def add_candidate(self, candidate: dict) -> dict:
        with self._transaction() as connection:
            self._read(connection, candidate["trip_id"])
            connection.execute(
                candidates.insert().values(
                    id=candidate["id"],
                    trip_id=candidate["trip_id"],
                    body=encode(candidate),
                )
            )
            self._event(
                connection,
                candidate["trip_id"],
                "candidate_generated",
                {
                    "candidate_id": candidate["id"],
                    "status": candidate["status"],
                    "requirements_revision": candidate["requirements_revision"],
                    "base_version": candidate["base_version"],
                    "mode": candidate.get("mode"),
                },
                candidate_id=candidate["id"],
            )
        return candidate

    def accept_candidate(self, trip_id: str, candidate_id: str) -> dict:
        try:
            with self._transaction() as connection:
                old_body, trip = self._read(connection, trip_id)
                if trip["accepted_plan"] and trip["accepted_plan"]["id"] == candidate_id:
                    return trip
                body = connection.execute(
                    select(candidates.c.body).where(
                        candidates.c.id == candidate_id,
                        candidates.c.trip_id == trip_id,
                    )
                ).scalar_one_or_none()
                if body is None:
                    raise StoreError(404, "候选不存在")
                candidate = json.loads(body)
                if candidate["status"] != "ready":
                    raise StoreError(409, "此候选未通过检查")
                if (
                    candidate["requirements_revision"] != trip["requirements_revision"]
                    or candidate["base_version"] != trip["accepted_version"]
                ):
                    raise StoreError(409, "候选依据的需求或行程已过时，请重新生成")
                trip["accepted_plan"] = candidate
                trip["accepted_version"] += 1
                trip["updated_at"] = now()
                self._replace(connection, old_body, trip)
                self._event(
                    connection,
                    trip_id,
                    "candidate_accepted",
                    {
                        "candidate_id": candidate_id,
                        "accepted_version": trip["accepted_version"],
                        "requirements_revision": trip["requirements_revision"],
                    },
                )
            return trip
        except StoreError as exc:
            if exc.status_code == 409:
                # A simultaneous acceptance of the same candidate remains idempotent.
                current = self.get_trip(trip_id)
                if current["accepted_plan"] and current["accepted_plan"]["id"] == candidate_id:
                    return current
            raise

    def history(self, trip_id: str) -> dict:
        recorded = (
            select(
                trip_events.c.candidate_id,
                trip_events.c.created_at,
            )
            .where(trip_events.c.kind == "candidate_generated")
            .subquery()
        )
        query = (
            select(candidates.c.body)
            .outerjoin(
                recorded,
                candidates.c.id == recorded.c.candidate_id,
            )
            .where(candidates.c.trip_id == trip_id)
            .order_by(
                recorded.c.created_at.desc().nulls_last(),
                candidates.c.id.desc(),
            )
            .limit(20)
        )
        with self._transaction() as connection:
            self._read(connection, trip_id)
            candidate_bodies = connection.execute(query).scalars().all()
            events = (
                connection.execute(
                    select(trip_events)
                    .where(
                        trip_events.c.trip_id == trip_id,
                    )
                    .order_by(trip_events.c.created_at.desc(), trip_events.c.id.desc())
                    .limit(50)
                )
                .mappings()
                .all()
            )
        return {
            "candidates": [json.loads(body) for body in candidate_bodies],
            "events": [
                {
                    "id": item["id"],
                    "kind": item["kind"],
                    "created_at": item["created_at"],
                    **json.loads(item["body"]),
                }
                for item in events
            ],
        }
