"""Local HTTP workspace. Provider calls run outside short storage transactions."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx2
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from .api_cost import candidate_cost_view
from .attraction_picker import (
    PICKER_VERSION,
    AttractionRankingError,
    discover_candidates,
    make_batch,
    rank_candidates,
)
from .domain import Requirements, TravelPreferences
from .evidence import load_evidence
from .identity import WORKSPACE_ID
from .maps import MapClient, MapError
from .model_usage import planning_budget
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
    session_keys: dict[str, SecretStr] = {}

    def provider_settings():
        settings = Settings()
        overrides = session_keys.copy()
        if settings.base_url.rstrip("/") not in (
            "https://api.deepseek.com",
            "https://api.deepseek.com/v1",
        ):
            overrides.pop("api_key", None)
        return settings.model_copy(update=overrides)

    class KeyUpdate(BaseModel):
        amap_api_key: SecretStr | None = None
        api_key: SecretStr | None = None

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
        settings = provider_settings()
        tomorrow = (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1)).date()
        return {
            "model_configured": settings.configured,
            "map_configured": map_configured(settings),
            "supported_modes": (
                ["deepseek"] if settings.configured and map_configured(settings) else []
            )
            if map_configured(settings)
            else [],
            "model": settings.model,
            "planning_budget": planning_budget(settings),
            "thinking_configuration": {
                "planner": {"enabled": True, "effort": settings.planner_effort},
                "research": {"enabled": True, "effort": settings.research_effort},
            },
            "provider": "DeepSeek / 兼容接口",
            "base_url": settings.base_url,
            "default_date": tomorrow.isoformat(),
            "defaults": Requirements(date=tomorrow, days=2).model_dump(mode="json"),
        }

    @app.put("/api/config/keys")
    async def save_keys(request: Request):
        if (
            not request.client
            or request.client.host not in ("127.0.0.1", "::1")
            or request.url.hostname not in ("localhost", "127.0.0.1", "::1")
            or request.headers.get("origin") != str(request.base_url).rstrip("/")
        ):
            raise HTTPException(403, "请从本机工作台配置密钥")
        try:
            payload = KeyUpdate.model_validate(await request.json())
        except ValueError, ValidationError:
            raise HTTPException(422, "API Key 格式无效") from None
        updated = {}
        for field in payload.model_fields_set:
            value = getattr(payload, field)
            key = value.get_secret_value().strip() if value else ""
            if not 1 <= len(key) <= 512 or any(not 33 <= ord(char) <= 126 for char in key):
                raise HTTPException(422, "API Key 格式无效")
            updated[field] = SecretStr(key)
        if not updated:
            raise HTTPException(422, "请填写至少一个 API Key")
        if "api_key" in updated and Settings().base_url.rstrip("/") not in (
            "https://api.deepseek.com",
            "https://api.deepseek.com/v1",
        ):
            raise HTTPException(422, "自定义模型地址的密钥请通过本地 .env 配置")
        session_keys.update(updated)
        return JSONResponse(config(), headers={"Cache-Control": "no-store"})

    @app.get("/api/catalog")
    def catalog():
        return {"places": [], "hotels": [], "source": "规划时查询高德真实地点，不使用固定样本"}

    class PreferencesUpdate(BaseModel):
        expected_revision: int = Field(ge=0)
        values: TravelPreferences

    @app.get("/api/preferences")
    def get_preferences():
        profile = store.get_preferences()
        profile["values"] = TravelPreferences.model_validate(profile["values"]).model_dump()
        return profile

    @app.put("/api/preferences")
    def save_preferences(payload: PreferencesUpdate):
        return store.save_preferences(payload.values.model_dump(), payload.expected_revision)

    @app.post("/api/hotels/preview")
    async def preview_hotels(req: Requirements):
        check_requirements(req)
        settings = provider_settings()
        if not map_configured(settings):
            raise HTTPException(422, "请先配置高德Web服务Key，再查询真实酒店")
        try:
            async with httpx2.AsyncClient(timeout=15) as client:
                maps = MapClient(
                    client, settings.amap_api_key.get_secret_value(), max_calls=8, interval=0.4
                )
                data = await prepare(req, maps, [], hotels_only=True)
            return {"options": data["hotels"], "recommended": data["hotel"]}
        except MapError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.get("/api/trips")
    def list_trips():
        return store.list_trips()

    async def build_attraction_preview(
        req: Requirements, selected_details: list[dict] | None = None
    ):
        check_requirements(req)
        settings = provider_settings()
        if not map_configured(settings):
            raise HTTPException(422, "请先配置高德 Web 服务 Key，再查询景点。")
        if not settings.configured or settings.base_url.rstrip("/") not in {
            "https://api.deepseek.com",
            "https://api.deepseek.com/v1",
        }:
            raise HTTPException(422, "请先配置官方 DeepSeek API Key，才能整理和去重景点推荐。")
        try:
            async with httpx2.AsyncClient(timeout=15, follow_redirects=False) as client:
                maps = MapClient(
                    client, settings.amap_api_key.get_secret_value(), max_calls=8, interval=0.4
                )
                options = await discover_candidates(req, maps)
                known = {place["id"]: place for place in options}
                for place in selected_details or [p.model_dump() for p in req.selected_attractions]:
                    known.setdefault(place["id"], place)
                options = list(known.values())
                pool, ranking_notice = await rank_candidates(req, options, settings, client)
                selected_items = []
                for selected in req.selected_attractions:
                    group = next(
                        (
                            place
                            for place in pool
                            if selected.id in place.get("experience_members", [place["id"]])
                        ),
                        None,
                    )
                    if group is None:
                        raise AttractionRankingError(
                            "已有选择的主体分组不完整，已保留原草稿，请重新查询。"
                        )
                    selected_items.append(
                        {
                            **known[selected.id],
                            "experience_group": group["experience_group"],
                            "experience_members": group["experience_members"],
                        }
                    )
                selected_ids = [p.id for p in req.selected_attractions]
                return await make_batch(
                    req,
                    {
                        "candidate_pool": pool,
                        "selected_ids": selected_ids,
                        "selected_items": selected_items,
                        "seen_ids": selected_ids,
                        "ranking_notice": ranking_notice,
                    },
                    settings,
                    client,
                )
        except (MapError, AttractionRankingError) as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/attractions/preview")
    async def preview_attractions(req: Requirements):
        return await build_attraction_preview(req)

    @app.post("/api/trips")
    def create(req: Requirements):
        check_requirements(req)
        return store.create_trip(req.model_dump(mode="json"))

    class AttractionDraftUpdate(BaseModel):
        selection_revision: int = Field(ge=1)
        selected_ids: list[str] = Field(max_length=5)

    class NextAttractionBatch(BaseModel):
        selection_revision: int = Field(ge=1)

    @app.get("/api/trips/{trip_id}/attractions/{requirements_revision}")
    def get_attraction_draft(trip_id: str, requirements_revision: int):
        draft = store.get_attraction_draft(trip_id, requirements_revision)
        return draft if draft and draft.get("picker_version") == PICKER_VERSION else None

    @app.post("/api/trips/{trip_id}/attractions/{requirements_revision}")
    async def create_attraction_draft(trip_id: str, requirements_revision: int):
        existing = await run_in_threadpool(
            store.get_attraction_draft, trip_id, requirements_revision
        )
        if existing is not None and existing.get("picker_version") == PICKER_VERSION:
            return existing
        trip = await run_in_threadpool(store.get_trip, trip_id)
        if trip["requirements_revision"] != requirements_revision:
            raise HTTPException(409, "旅行偏好已更新，请重新进入选景。")
        req = Requirements.model_validate(trip["requirements"])
        selected = {place.id: place.model_dump() for place in req.selected_attractions}
        if existing:
            previous = existing.get("candidate_pool", existing["options"]) + existing.get(
                "selected_items", []
            )
            selected = {
                place["id"]: place for place in previous if place["id"] in existing["selected_ids"]
            }
            if set(selected) != set(existing["selected_ids"]):
                raise HTTPException(409, "旧草稿中的选择信息不完整，请先核对原来的景点选择。")
        req = Requirements.model_validate(
            {
                **req.model_dump(mode="json"),
                "selected_attractions": [
                    {"id": place["id"], "name": place["name"]} for place in selected.values()
                ],
            }
        )
        draft = await build_attraction_preview(req, list(selected.values()))
        if not draft["options"] and not draft["selected_items"]:
            raise HTTPException(422, "本轮没有找到符合偏好的推荐景点，请调整旅行偏好后再试。")
        if existing is not None:
            return await run_in_threadpool(
                store.replace_attraction_batch,
                trip_id,
                requirements_revision,
                existing["selection_revision"],
                draft,
            )
        return await run_in_threadpool(
            store.create_attraction_draft, trip_id, requirements_revision, draft
        )

    @app.post("/api/trips/{trip_id}/attractions/{requirements_revision}/next")
    async def next_attraction_batch(
        trip_id: str, requirements_revision: int, payload: NextAttractionBatch
    ):
        draft = await run_in_threadpool(store.get_attraction_draft, trip_id, requirements_revision)
        if draft is None or draft.get("picker_version") != PICKER_VERSION:
            raise HTTPException(409, "请先按当前需求重新查询景点。")
        if draft["selection_revision"] != payload.selection_revision:
            raise HTTPException(409, "选景已更新，请重新读取后再换一批。")
        if draft["exhausted"]:
            return draft
        trip = await run_in_threadpool(store.get_trip, trip_id)
        if trip["requirements_revision"] != requirements_revision:
            raise HTTPException(409, "旅行偏好已更新，请重新进入选景。")
        settings = provider_settings()
        async with httpx2.AsyncClient(timeout=15, follow_redirects=False) as client:
            batch = await make_batch(
                Requirements.model_validate(trip["requirements"]), draft, settings, client
            )
        return await run_in_threadpool(
            store.replace_attraction_batch,
            trip_id,
            requirements_revision,
            payload.selection_revision,
            batch,
        )

    @app.put("/api/trips/{trip_id}/attractions/{requirements_revision}")
    def update_attraction_draft(
        trip_id: str, requirements_revision: int, payload: AttractionDraftUpdate
    ):
        return store.update_attraction_draft(
            trip_id, requirements_revision, payload.selection_revision, payload.selected_ids
        )

    @app.post("/api/trips/{trip_id}/attractions/{requirements_revision}/confirm")
    def confirm_attractions(trip_id: str, requirements_revision: int, payload: NextAttractionBatch):
        return store.confirm_attractions(trip_id, requirements_revision, payload.selection_revision)

    @app.get("/api/trips/{trip_id}")
    def get(trip_id: str):
        trip = store.get_trip(trip_id)
        if trip.get("accepted_plan"):
            trip = {**trip, "accepted_plan": candidate_cost_view(trip["accepted_plan"])}
        return trip

    @app.get("/api/trips/{trip_id}/history")
    def history(trip_id: str):
        history = store.history(trip_id)
        return {**history, "candidates": [candidate_cost_view(p) for p in history["candidates"]]}

    class Revision(Requirements):
        expected_revision: int

    @app.put("/api/trips/{trip_id}/requirements")
    def update(trip_id: str, payload: Revision):
        req = Requirements.model_validate(payload.model_dump(exclude={"expected_revision"}))
        check_requirements(req)
        return store.update_requirements(
            trip_id, req.model_dump(mode="json"), payload.expected_revision
        )

    class PlanRequest(BaseModel):
        mode: str = "deepseek"
        source_plan_id: str | None = Field(default=None, max_length=64)
        revision_notes: str = Field(default="", max_length=1500)
        expected_requirements_revision: int | None = Field(default=None, ge=1)

    @app.post("/api/trips/{trip_id}/plan")
    async def generate(trip_id: str, payload: PlanRequest):
        if payload.mode != "deepseek":
            raise HTTPException(422, "新行程仅支持 DeepSeek 规划")
        settings = provider_settings()
        if not map_configured(settings):
            raise HTTPException(
                422, "尚未配置AMAP_API_KEY，请先接入真实地图；系统不会输出固定演示行程"
            )
        if not settings.configured:
            raise HTTPException(422, "尚未配置DeepSeek API Key，请在接入设置保存后重试")
        trip = await run_in_threadpool(store.get_trip, trip_id)
        expected = payload.expected_requirements_revision
        if expected is not None and trip["requirements_revision"] != expected:
            raise HTTPException(409, "旅行偏好已经更新，请核对后重新生成。")
        previous = trip["accepted_plan"]
        revision = payload.revision_notes.strip()
        if payload.source_plan_id:
            source = await run_in_threadpool(store.get_candidate, trip_id, payload.source_plan_id)
            if source["status"] != "ready" or not revision:
                raise HTTPException(422, "请选择已通过检查的原方案并填写修改要求")
            prior_requirements = source.get("requirements") or {}
            if prior_requirements.get("city") != trip["requirements"]["city"]:
                raise HTTPException(422, "原方案和当前目的地不同，请重新生成行程")
            prior_ids = {stop["place_id"] for stop in source["itinerary"]["stops"]}
            previous = {
                "itinerary": source["itinerary"],
                "places": [p for p in source.get("places", []) if p["id"] in prior_ids],
                "revision_request": revision,
            }
        elif revision:
            raise HTTPException(422, "请指定要修改的原方案")
        evidence = load_evidence(
            trip.get("research_evidence"), trip["requirements"], trip["requirements_revision"]
        )
        if trip.get("research_evidence") is not None and evidence is None:
            raise HTTPException(409, "已确认的选景资料与当前需求不一致，请重新确认景点后再规划。")
        candidate = {
            "source_plan_id": payload.source_plan_id,
            "revision_notes": revision,
            "id": uuid4().hex,
            "trip_id": trip_id,
            "requirements_revision": trip["requirements_revision"],
            "base_version": trip["accepted_version"],
            "requirements": trip["requirements"],
            "research_evidence": evidence,
            "mode": payload.mode,
            "created_at": datetime.now(UTC).isoformat(),
            **await run_plan(
                Requirements.model_validate(trip["requirements"]),
                mode=payload.mode,
                settings=settings,
                previous_plan=previous,
                research_context=evidence,
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
