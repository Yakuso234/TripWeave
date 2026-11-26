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
    JSON,
    Column,
    ForeignKey,
    Index,
    Integer,
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
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.pool import StaticPool

from .evidence import freeze_evidence, load_evidence, rebind_evidence


class StoreError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


metadata = MetaData()
travel_preferences = Table(
    "travel_preferences",
    metadata,
    Column("id", String(20), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("body", JSON, nullable=False),
)
LOCAL_PROFILE_ID = "local"
trips = Table(
    "trips", metadata, Column("id", Text, primary_key=True), Column("body", Text, nullable=False)
)
attraction_drafts = Table(
    "attraction_drafts",
    metadata,
    Column("trip_id", Text, ForeignKey("trips.id"), primary_key=True),
    Column("requirements_revision", Integer, primary_key=True),
    Column("selection_revision", Integer, nullable=False),
    Column("body", JSON, nullable=False),
)
candidates = Table(
    "candidates",
    metadata,
    Column("id", Text, primary_key=True),
    Column("trip_id", Text, nullable=False),
    Column("body", Text, nullable=False),
)
planning_runs = Table(
    "planning_runs",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("trip_id", Text, ForeignKey("trips.id"), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("body", Text, nullable=False),
)
planning_run_index = Index(
    "ix_planning_runs_trip_time", planning_runs.c.trip_id, planning_runs.c.created_at
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
            for index in (candidate_index, event_index, event_candidate_index, planning_run_index):
                index.create(connection, checkfirst=True)

    def dispose(self) -> None:
        self.engine.dispose()

    def get_preferences(self) -> dict:
        with self._transaction() as connection:
            row = (
                connection.execute(
                    select(
                        travel_preferences.c.revision, travel_preferences.c.body.label("values")
                    ).where(travel_preferences.c.id == LOCAL_PROFILE_ID)
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row else {"revision": 0, "values": {}}

    def save_preferences(self, values: dict, expected_revision: int) -> dict:
        revision = expected_revision + 1
        conflict = "长期偏好已被其他操作更新，请重新读取后再保存。"
        with self._transaction() as connection:
            if expected_revision == 0:
                try:
                    connection.execute(
                        travel_preferences.insert().values(
                            id=LOCAL_PROFILE_ID, revision=revision, body=values
                        )
                    )
                except IntegrityError:
                    raise StoreError(409, conflict) from None
            else:
                changed = connection.execute(
                    update(travel_preferences)
                    .where(
                        travel_preferences.c.id == LOCAL_PROFILE_ID,
                        travel_preferences.c.revision == expected_revision,
                    )
                    .values(revision=revision, body=values)
                )
                if changed.rowcount != 1:
                    raise StoreError(409, conflict)
        return {"revision": revision, "values": values}

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

    def _attraction_draft(self, connection, trip_id: str, requirements_revision: int):
        _, trip = self._read(connection, trip_id)
        if trip["requirements_revision"] != requirements_revision:
            raise StoreError(409, "旅行偏好已更新，请重新进入选景。")
        key = (
            attraction_drafts.c.trip_id == trip_id,
            attraction_drafts.c.requirements_revision == requirements_revision,
        )
        row = connection.execute(select(attraction_drafts).where(*key)).mappings().one_or_none()
        draft = {**row["body"], "selection_revision": row["selection_revision"]} if row else None
        return key, draft

    def get_attraction_draft(self, trip_id: str, requirements_revision: int) -> dict | None:
        with self._transaction() as connection:
            _, draft = self._attraction_draft(connection, trip_id, requirements_revision)
            return draft

    def create_attraction_draft(self, trip_id: str, requirements_revision: int, body: dict) -> dict:
        with self._transaction() as connection:
            _, existing = self._attraction_draft(connection, trip_id, requirements_revision)
            if existing is not None:
                return existing
            try:
                with connection.begin_nested():
                    connection.execute(
                        attraction_drafts.insert().values(
                            trip_id=trip_id,
                            requirements_revision=requirements_revision,
                            selection_revision=1,
                            body=body,
                        )
                    )
            except IntegrityError:
                _, existing = self._attraction_draft(connection, trip_id, requirements_revision)
                if existing is not None:
                    return existing
                raise
        return {**body, "selection_revision": 1}

    def update_attraction_draft(
        self,
        trip_id: str,
        requirements_revision: int,
        selection_revision: int,
        selected_ids: list[str],
    ) -> dict:
        with self._transaction() as connection:
            key, draft = self._attraction_draft(connection, trip_id, requirements_revision)
            if draft is None:
                raise StoreError(404, "选景草稿不存在，请先查询候选。")
            seen = set(draft.get("seen_ids", [place["id"] for place in draft["options"]]))
            if not set(selected_ids) <= seen:
                raise StoreError(422, "请选择本次查询中的景点。")
            known = {
                place["id"]: place
                for place in draft.get("candidate_pool", draft["options"])
                + draft.get("selected_items", [])
            }
            groups = set()
            # Legacy choices can contain several duplicates. Allow gradual removal.
            removing_only = set(selected_ids) < set(draft["selected_ids"])
            for place_id in dict.fromkeys(selected_ids):
                group = known.get(place_id, {}).get("experience_group")
                if group and group in groups and not removing_only:
                    raise StoreError(422, "这些景点属于同一游玩内容，请只保留其中一个。")
                if group:
                    groups.add(group)
            draft.pop("selection_revision")
            draft["selected_ids"] = list(dict.fromkeys(selected_ids))
            revision = selection_revision + 1
            updated = connection.execute(
                update(attraction_drafts)
                .where(*key, attraction_drafts.c.selection_revision == selection_revision)
                .values(selection_revision=revision, body=draft)
            )
            if updated.rowcount != 1:
                raise StoreError(409, "选景草稿已更新，请重新读取后再选择。")
        return {**draft, "selection_revision": revision}

    def replace_attraction_batch(
        self, trip_id: str, requirements_revision: int, selection_revision: int, body: dict
    ) -> dict:
        saved = {key: value for key, value in body.items() if key != "selection_revision"}
        revision = selection_revision + 1
        with self._transaction() as connection:
            key, draft = self._attraction_draft(connection, trip_id, requirements_revision)
            if draft is None:
                raise StoreError(404, "选景草稿不存在，请先查询景点。")
            updated = connection.execute(
                update(attraction_drafts)
                .where(*key, attraction_drafts.c.selection_revision == selection_revision)
                .values(selection_revision=revision, body=saved)
            )
            if updated.rowcount != 1:
                raise StoreError(409, "选景已更新，请重新读取后再换一批。")
        return {**saved, "selection_revision": revision}

    def confirm_attractions(
        self, trip_id: str, requirements_revision: int, selection_revision: int
    ) -> dict:
        """Atomically bridge an inspected selection to the next requirements version."""
        with self._transaction() as connection:
            old_body, trip = self._read(connection, trip_id)
            prior = load_evidence(
                trip.get("research_evidence"), trip["requirements"], trip["requirements_revision"]
            )
            # A lost HTTP response can be retried without confirming twice.
            if (
                prior
                and trip["requirements_revision"] == requirements_revision + 1
                and prior["source_requirements_revision"] == requirements_revision
                and prior["selection_revision"] == selection_revision
            ):
                return trip
            key, draft = self._attraction_draft(connection, trip_id, requirements_revision)
            if draft is None or draft.get("picker_version") != 3:
                raise StoreError(409, "请先按当前需求重新查询并选择景点。")
            if draft["selection_revision"] != selection_revision:
                raise StoreError(409, "选景已更新，请重新读取后确认。")
            if not draft["selected_ids"]:
                raise StoreError(422, "请至少选择一个景点。")
            next_revision = requirements_revision + 1
            try:
                evidence = freeze_evidence(
                    draft,
                    city=trip["requirements"]["city"],
                    source_requirements_revision=requirements_revision,
                    requirements_revision=next_revision,
                )
            except ValueError:
                raise StoreError(
                    409, "选景资料不完整或存在重复体验，请重新读取并核对后确认。"
                ) from None
            reserved = connection.execute(
                update(attraction_drafts)
                .where(*key, attraction_drafts.c.selection_revision == selection_revision)
                .values(selection_revision=selection_revision + 1)
            )
            if reserved.rowcount != 1:
                raise StoreError(409, "选景已更新，请重新读取后确认。")
            trip["requirements"] = {
                **trip["requirements"],
                "selected_attractions": [
                    {"id": place["place_id"], "name": place["name"]}
                    for place in evidence["selected"]
                ],
            }
            trip["requirements_revision"] = next_revision
            trip["research_evidence"] = evidence
            trip["updated_at"] = now()
            self._replace(connection, old_body, trip)
            self._event(
                connection,
                trip_id,
                "attractions_confirmed",
                {
                    "source_requirements_revision": requirements_revision,
                    "selection_revision": selection_revision,
                    "requirements_revision": next_revision,
                    "selected_ids": draft["selected_ids"],
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
            trip["research_evidence"] = rebind_evidence(
                trip.get("research_evidence"), requirements, trip["requirements_revision"]
            )
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

    def _insert_candidate(self, connection, candidate: dict) -> None:
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

    def add_candidate(self, candidate: dict) -> dict:
        with self._transaction() as connection:
            self._read(connection, candidate["trip_id"])
            self._insert_candidate(connection, candidate)
        return candidate

    def start_planning_run(
        self, trip_id: str, requirements_revision: int, base_version: int
    ) -> dict:
        """Create an execution record, without consuming an accepted-plan version."""
        with self._transaction() as connection:
            _, trip = self._read(connection, trip_id)
            if (trip["requirements_revision"], trip["accepted_version"]) != (
                requirements_revision,
                base_version,
            ):
                raise StoreError(409, "需求或已接受行程已更新，请重新开始规划。")
            timestamp = now()
            run = {
                "id": uuid4().hex,
                "trip_id": trip_id,
                "requirements_revision": requirements_revision,
                "base_version": base_version,
                "requirements": trip["requirements"],
                "status": "running",
                "outcome": None,
                "phase": "preparing",
                "draft": None,
                "query_requests": [],
                "pending_information": [],
                "assumptions": [],
                "query_log": [],
                "places": [],
                "issues": [],
                "candidate_id": None,
                "error": None,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            connection.execute(
                planning_runs.insert().values(
                    id=run["id"],
                    trip_id=trip_id,
                    created_at=timestamp,
                    body=encode(run),
                )
            )
        return run

    def _read_planning_run(self, connection, trip_id: str, run_id: str) -> tuple[str, dict]:
        body = connection.execute(
            select(planning_runs.c.body).where(
                planning_runs.c.id == run_id,
                planning_runs.c.trip_id == trip_id,
            )
        ).scalar_one_or_none()
        if body is None:
            raise StoreError(404, "规划执行记录不存在")
        return body, json.loads(body)

    def _replace_planning_run(self, connection, old_body: str, run: dict):
        run["updated_at"] = now()
        changed = connection.execute(
            update(planning_runs)
            .where(
                planning_runs.c.id == run["id"],
                planning_runs.c.body == old_body,
            )
            .values(body=encode(run))
        )
        if changed.rowcount != 1:
            raise StoreError(409, "规划执行记录已更新，请重新读取。")

    def save_planning_draft(self, trip_id: str, run_id: str, progress: dict) -> dict:
        """Persist a draft immediately; model data cannot replace immutable run bindings."""
        fields = (
            "draft",
            "query_requests",
            "pending_information",
            "assumptions",
            "query_log",
            "route_table",
            "issues",
            "places",
            "phase",
        )
        with self._transaction() as connection:
            old_body, run = self._read_planning_run(connection, trip_id, run_id)
            if run["status"] != "running":
                raise StoreError(409, "本次规划已经结束，不能覆盖草稿。")
            run.update({key: progress[key] for key in fields if key in progress})
            self._replace_planning_run(connection, old_body, run)
        return run

    def finish_planning_run(
        self, trip_id: str, run_id: str, result: dict | None = None, *, error: str | None = None
    ) -> dict:
        """Publish the candidate and settle its execution record together."""
        with self._transaction() as connection:
            old_body, run = self._read_planning_run(connection, trip_id, run_id)
            if run["status"] != "running":
                if result is not None and result["id"] != run["candidate_id"]:
                    raise StoreError(409, "本次规划已经结束，请查看已有执行记录。")
                return run
            if result is not None:
                if (
                    result["trip_id"],
                    result.get("planning_run_id"),
                    result["requirements_revision"],
                    result["base_version"],
                ) != (trip_id, run_id, run["requirements_revision"], run["base_version"]):
                    raise StoreError(409, "规划结果与执行记录依据不一致。")
                run["candidate_id"] = result["id"]
                run["outcome"] = result.get("outcome", result["status"])
                run["status"] = "completed" if run["outcome"] == "ready" else "incomplete"
                # A successful result may contain only itinerary; keep it inspectable
                # as an execution snapshot while acceptance still uses candidates.
                run["draft"] = result.get("itinerary") or result.get("draft") or run["draft"]
                for key in (
                    "query_log",
                    "route_table",
                    "query_requests",
                    "pending_information",
                    "assumptions",
                    "places",
                    "issues",
                ):
                    if key in result:
                        run[key] = result[key]
                run["error"] = result.get("error")
                run["phase"] = "finished"
                self._insert_candidate(connection, result)
            else:
                run.update(
                    status="interrupted",
                    outcome="incomplete",
                    phase="interrupted",
                    error=error or "本次规划已中断，已保存的草稿仍可查看。",
                )
            self._replace_planning_run(connection, old_body, run)
        return run

    def list_planning_runs(self, trip_id: str) -> list[dict]:
        with self._transaction() as connection:
            self._read(connection, trip_id)
            bodies = (
                connection.execute(
                    select(planning_runs.c.body)
                    .where(
                        planning_runs.c.trip_id == trip_id,
                    )
                    .order_by(planning_runs.c.created_at.desc(), planning_runs.c.id.desc())
                    .limit(20)
                )
                .scalars()
                .all()
            )
        return [json.loads(body) for body in bodies]

    def get_candidate(self, trip_id: str, candidate_id: str) -> dict:
        with self._transaction() as connection:
            body = connection.execute(
                select(candidates.c.body).where(
                    candidates.c.id == candidate_id, candidates.c.trip_id == trip_id
                )
            ).scalar_one_or_none()
        if body is None:
            raise StoreError(404, "方案不存在")
        return json.loads(body)

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
                if candidate["status"] != "ready" or candidate.get("outcome", "ready") != "ready":
                    raise StoreError(409, "此候选未通过检查")
                review = candidate.get("review")
                if review is not None and (
                    not isinstance(review, dict) or review.get("status") != "approved"
                ):
                    raise StoreError(409, "此候选尚未完成校验Agent复核")
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
