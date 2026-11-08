"""Local HTTP workspace. Provider calls run outside short storage transactions."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx2
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from .domain import Requirements
from .identity import WORKSPACE_ID
from .maps import MapClient, MapError
from .real_planner import map_configured, prepare, run_plan
from .settings import Settings
from .storage import Store, StoreError

STATIC = Path(__file__).parent / "static"


def create_app(database: str | None = None) -> FastAPI:
    settings = Settings()
    target = database or (
        settings.database_url.get_secret_value() if settings.database_url else settings.database
    )
    store = Store(target)
    store.initialize()

    @asynccontextmanager
    async def lifespan(app):
        yield
        await run_in_threadpool(store.dispose)

    app = FastAPI(title="TripWeave · 旅行规划工作台", lifespan=lifespan)
    app.state.store = store

    @app.exception_handler(StoreError)
    async def storage_error(request, exc):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        # Do not expose SQL parameters, private plans or credential-bearing DSNs.
        return JSONResponse(
            {"detail": "数据库暂时不可用，请稍后重试；当前操作未确认保存"}, status_code=503
        )

    def check_requirements(req):
        if not req.city.strip() or not req.origin_query.strip():
            raise HTTPException(422, "请填写目的地城市和出发地点")

    @app.get("/api/health")
    def health():
        return {"application": "tripweave", "workspace_id": WORKSPACE_ID, "version": "0.2.0"}

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
        settings = Settings()
        tomorrow = (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1)).date()
        return {
            "model_configured": settings.configured,
            "map_configured": map_configured(settings),
            "supported_modes": (["basic", "deepseek"] if settings.configured else ["basic"])
            if map_configured(settings)
            else [],
            "model": settings.model,
            "provider": "DeepSeek / 兼容接口",
            "base_url": settings.base_url,
            "default_date": tomorrow.isoformat(),
            "defaults": Requirements(date=tomorrow, days=2).model_dump(mode="json"),
        }

    @app.get("/api/catalog")
    def catalog():
        return {"places": [], "hotels": [], "source": "规划时查询高德真实地点，不使用固定样本"}

    @app.post("/api/hotels/preview")
    async def preview_hotels(req: Requirements):
        check_requirements(req)
        settings = Settings()
        if not map_configured(settings):
            raise HTTPException(422, "请先配置高德Web服务Key，再查询真实酒店")
        try:
            async with httpx2.AsyncClient(timeout=15) as client:
                maps = MapClient(client, settings.amap_api_key.get_secret_value(), max_calls=8)
                data = await prepare(req, maps, [], hotels_only=True)
            return {"options": data["hotels"], "recommended": data["hotel"]}
        except MapError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.get("/api/trips")
    def list_trips():
        return store.list_trips()

    @app.post("/api/trips")
    def create(req: Requirements):
        check_requirements(req)
        return store.create_trip(req.model_dump(mode="json"))

    @app.get("/api/trips/{trip_id}")
    def get(trip_id: str):
        return store.get_trip(trip_id)

    @app.get("/api/trips/{trip_id}/history")
    def history(trip_id: str):
        return store.history(trip_id)

    class Revision(Requirements):
        expected_revision: int

    @app.put("/api/trips/{trip_id}/requirements")
    def update(trip_id: str, payload: Revision):
        req = Requirements.model_validate(payload.model_dump())
        check_requirements(req)
        return store.update_requirements(
            trip_id, req.model_dump(mode="json"), payload.expected_revision
        )

    class PlanRequest(BaseModel):
        mode: str = "basic"

    @app.post("/api/trips/{trip_id}/plan")
    async def generate(trip_id: str, payload: PlanRequest):
        if payload.mode not in ("basic", "deepseek"):
            raise HTTPException(422, "请选择真实地图基础规划或DeepSeek模式")
        settings = Settings()
        if not map_configured(settings):
            raise HTTPException(
                422, "尚未配置AMAP_API_KEY，请先接入真实地图；系统不会输出固定演示行程"
            )
        if payload.mode == "deepseek" and not settings.configured:
            raise HTTPException(422, "尚未配置DeepSeek API Key，请在项目.env填写后刷新配置")
        trip = await run_in_threadpool(store.get_trip, trip_id)
        candidate = {
            "id": uuid4().hex,
            "trip_id": trip_id,
            "requirements_revision": trip["requirements_revision"],
            "base_version": trip["accepted_version"],
            "requirements": trip["requirements"],
            "mode": payload.mode,
            "created_at": datetime.now(UTC).isoformat(),
            **await run_plan(
                Requirements.model_validate(trip["requirements"]),
                mode=payload.mode,
                settings=settings,
                previous_plan=trip["accepted_plan"],
            ),
        }
        await run_in_threadpool(store.add_candidate, candidate)
        return candidate

    @app.post("/api/trips/{trip_id}/candidates/{candidate_id}/accept")
    def accept(trip_id: str, candidate_id: str):
        return store.accept_candidate(trip_id, candidate_id)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
