"""Local-only HTTP application and transactional prototype state."""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .domain import Requirements, place_id
from .identity import WORKSPACE_ID
from .planner import plan

STATIC = Path(__file__).parent / "static"


def create_app(database: str | None = None) -> FastAPI:
    db_path = Path(database or os.environ.get("TRIPWEAVE_DATABASE", ".runtime/tripweave.sqlite3"))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection():
        db = sqlite3.connect(db_path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    with connection() as db:
        db.executescript(
            "CREATE TABLE IF NOT EXISTS trips (id TEXT PRIMARY KEY, body TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS candidates (id TEXT PRIMARY KEY, trip_id TEXT NOT NULL, body TEXT NOT NULL);"
        )

    def read_trip(db, trip_id):
        row = db.execute("SELECT body FROM trips WHERE id=?", (trip_id,)).fetchone()
        if not row:
            raise HTTPException(404, "行程不存在")
        return json.loads(row[0])

    def save_trip(db, trip):
        db.execute(
            "INSERT OR REPLACE INTO trips VALUES (?, ?)",
            (trip["id"], json.dumps(trip, ensure_ascii=False)),
        )

    def check_requirements(req):
        try:
            if req.city not in ("上海", "上海市"):
                raise ValueError("当前离线实验只覆盖上海固定样本")
            place_id(req.origin_query)
            if req.booking_query:
                place_id(req.booking_query)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    app = FastAPI(title="TripWeave · 离线实验")

    @app.get("/api/health")
    def health():
        return {"application": "tripweave", "workspace_id": WORKSPACE_ID, "mode": "replay"}

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if (
            request.method not in ("GET", "HEAD")
            and origin
            and origin != str(request.base_url).rstrip("/")
        ):
            return JSONResponse({"detail": "仅允许同源页面操作"}, status_code=403)
        return await call_next(request)

    @app.get("/api/config")
    def config():
        return {
            "model_configured": False,
            "map_configured": False,
            "supported_modes": ["replay"],
            "default_date": (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1))
            .date()
            .isoformat(),
        }

    @app.post("/api/trips")
    def create(req: Requirements):
        check_requirements(req)
        trip = {
            "id": uuid4().hex,
            "requirements": req.model_dump(mode="json"),
            "requirements_revision": 1,
            "accepted_version": 0,
            "accepted_plan": None,
        }
        with connection() as db:
            save_trip(db, trip)
        return trip

    @app.get("/api/trips/{trip_id}")
    def get(trip_id: str):
        with connection() as db:
            return read_trip(db, trip_id)

    class Revision(Requirements):
        expected_revision: int

    @app.put("/api/trips/{trip_id}/requirements")
    def update(trip_id: str, payload: Revision):
        req = Requirements.model_validate(payload.model_dump())
        check_requirements(req)
        with connection() as db:
            db.execute("BEGIN IMMEDIATE")
            trip = read_trip(db, trip_id)
            if payload.expected_revision != trip["requirements_revision"]:
                raise HTTPException(409, "需求已更新，请刷新后重试")
            trip["requirements"] = req.model_dump(mode="json")
            trip["requirements_revision"] += 1
            save_trip(db, trip)
        return trip

    class PlanRequest(BaseModel):
        mode: str = "replay"

    @app.post("/api/trips/{trip_id}/plan")
    async def generate(trip_id: str, payload: PlanRequest):
        if payload.mode != "replay":
            raise HTTPException(422, "此版仅支持固定模型＋固定地图，真实调用尚未接入")
        with connection() as db:
            trip = read_trip(db, trip_id)
        candidate = {
            "id": uuid4().hex,
            "trip_id": trip_id,
            "requirements_revision": trip["requirements_revision"],
            "base_version": trip["accepted_version"],
            "mode": "replay",
            "created_at": datetime.now(UTC).isoformat(),
            **await plan(Requirements.model_validate(trip["requirements"])),
        }
        with connection() as db:
            db.execute(
                "INSERT INTO candidates VALUES (?, ?, ?)",
                (candidate["id"], trip_id, json.dumps(candidate, ensure_ascii=False)),
            )
        return candidate

    @app.post("/api/trips/{trip_id}/candidates/{candidate_id}/accept")
    def accept(trip_id: str, candidate_id: str):
        with connection() as db:
            db.execute("BEGIN IMMEDIATE")
            trip = read_trip(db, trip_id)
            if trip["accepted_plan"] and trip["accepted_plan"]["id"] == candidate_id:
                return trip
            row = db.execute(
                "SELECT body FROM candidates WHERE id=? AND trip_id=?", (candidate_id, trip_id)
            ).fetchone()
            if not row:
                raise HTTPException(404, "候选不存在")
            candidate = json.loads(row[0])
            if candidate["status"] != "ready":
                raise HTTPException(409, "此候选未通过检查")
            if (
                candidate["requirements_revision"] != trip["requirements_revision"]
                or candidate["base_version"] != trip["accepted_version"]
            ):
                raise HTTPException(409, "候选依据的需求或行程已过时，请重新生成")
            trip["accepted_plan"] = candidate
            trip["accepted_version"] += 1
            save_trip(db, trip)
        return trip

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
