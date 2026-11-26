"""Draft-first output and bounded evidence-query contracts.

These contracts describe requests; they neither execute tools nor prove venue
availability. Route identity is just origin, destination and transport. Query
history and execution budgets belong to the caller's current planning run.
"""

from collections.abc import Iterable, Mapping
from typing import Annotated, Literal
from unicodedata import normalize

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator

from .domain import Itinerary, Requirements, Stop
from .maps import transport_allowed
from .planning_contracts import Contract, PlaceId, PlanningNeed

Reason = Annotated[str, Field(min_length=1, max_length=300)]


class RouteQuery(Contract):
    kind: Literal["route"]
    origin_id: PlaceId
    destination_id: PlaceId
    transport: Literal["walking", "mixed"]
    reason: Reason


class RestaurantQuery(Contract):
    kind: Literal["nearby_restaurants"]
    place_id: PlaceId
    reason: Reason
    purpose: Literal["required_for_plan"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="仅当餐厅结果决定能否满足当前明确用餐要求时填写；普通备选建议留空",
    )


class PlaceQuery(Contract):
    kind: Literal["place_search"]
    query: str = Field(min_length=1, max_length=80)
    reason: Reason


QueryRequest = Annotated[RouteQuery | RestaurantQuery | PlaceQuery, Field(discriminator="kind")]
QUERY_ADAPTER = TypeAdapter(QueryRequest)


class EvidenceRequests(Contract):
    query_requests: list[QueryRequest] = Field(max_length=24)
    information_needs: list[PlanningNeed] = Field(max_length=5)
    assumptions: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(max_length=10)


class DraftProposal(EvidenceRequests):
    draft: Itinerary


class DayReplacement(Contract):
    day: int = Field(ge=1, le=7)
    stops: list[Stop] = Field(
        max_length=56, description="替换该日期的全体站点；空列表表示明确清空当天"
    )

    @model_validator(mode="after")
    def matching_days(self):
        if any(stop.day != self.day for stop in self.stops):
            raise ValueError("替换站点的day必须等于所替换日期")
        return self


class ItineraryPatch(Contract):
    days: list[DayReplacement] = Field(max_length=7)
    hotel_id: PlaceId | None = Field(default=None, description="只在确需更换自动酒店时填写新ID")
    summary: str | None = Field(default=None, max_length=1500, description="可选的更新摘要")

    @model_validator(mode="after")
    def unique_days(self):
        if len({item.day for item in self.days}) != len(self.days):
            raise ValueError("同一日期只能替换一次")
        return self


class ScopedRevision(EvidenceRequests):
    patch: ItineraryPatch


class DraftPatchError(ValueError):
    """A safe message about an output that could not be merged."""


def hotel_revision_days(req: Requirements) -> set[int]:
    if req.hotel_id or not req.hotel_nights:
        return set()
    return set(range(1, req.hotel_nights + 2))


def apply_itinerary_patch(
    req: Requirements, original: Itinerary, patch: ItineraryPatch, allowed_days: set[int]
) -> Itinerary:
    replaced = {item.day: item.stops for item in patch.days}
    hotel_id = patch.hotel_id if patch.hotel_id is not None else original.hotel_id
    if hotel_id != original.hotel_id:
        hotel_days = hotel_revision_days(req)
        if not hotel_days:
            raise DraftPatchError("本次修订不能更换手选酒店或新增住宿，原草稿已保留。")
        active_hotel_days = hotel_days & {stop.day for stop in original.stops}
        if not active_hotel_days.issubset(replaced):
            raise DraftPatchError(
                "自动住宿更换须同时返回受影响的过夜和次日起点日期，原草稿已保留。"
            )
        allowed_days = allowed_days | hotel_days
    if replaced.keys() - allowed_days:
        raise DraftPatchError("模型修改了本次局部修订范围之外的安排，原草稿已保留。")

    remaining = [stop for stop in original.stops if stop.day not in replaced] + [
        stop for stops in replaced.values() for stop in stops
    ]
    try:
        return Itinerary(
            summary=original.summary if patch.summary is None else patch.summary,
            hotel_id=hotel_id,
            stops=sorted(remaining, key=lambda stop: stop.day),
        )
    except ValidationError:
        raise DraftPatchError("合并后的站点数或行程结构超出范围，原草稿已保留。") from None


def _query_name(value: str) -> str:
    return " ".join(normalize("NFKC", value).split()).casefold()


def canonical_query(query: QueryRequest | Mapping) -> tuple[str, ...]:
    """Stable cache key; reasons and list positions do not change a route."""
    item = QUERY_ADAPTER.validate_python(query) if isinstance(query, Mapping) else query
    if isinstance(item, RouteQuery):
        return (item.kind, item.origin_id, item.destination_id, item.transport)
    if isinstance(item, RestaurantQuery):
        return (item.kind, item.place_id)
    if isinstance(item, PlaceQuery):
        return (item.kind, _query_name(item.query))
    raise TypeError("不支持的查询类型")


def _place_ids(values: Mapping | Iterable) -> set[str]:
    if isinstance(values, Mapping):
        return set(values)
    return {item if isinstance(item, str) else item["id"] for item in values}


def check_query_requests(
    req: Requirements,
    queries: Iterable[QueryRequest],
    places: Mapping | Iterable,
    *,
    allowed_place_queries: Iterable[str] = (),
) -> list[dict]:
    """Check known IDs and scope without doing IO or changing user choices.

    New names must be explicit user requirements, or individually supplied by
    the caller from this request's revision instruction. Free-form model reasons
    never grant permission to expand place search to unrelated destinations.
    """
    known = _place_ids(places)
    allowed = {
        _query_name(name)
        for name in (
            *req.must_visit,
            req.origin_query,
            req.booking_query,
            *(selection.name for selection in req.selected_attractions),
            *(constraint.place_query for constraint in req.visit_constraints),
            *allowed_place_queries,
        )
        if name and name.strip()
    }
    issues = []
    for index, query in enumerate(queries, start=1):
        base = {"category": "query_error", "query_number": index, "kind": query.kind}
        if isinstance(query, RouteQuery):
            missing = [pid for pid in (query.origin_id, query.destination_id) if pid not in known]
            if missing:
                issues.append(
                    {
                        **base,
                        "code": "query_identity",
                        "message": "路线只能引用已查询的起点和终点ID。",
                        "unknown_ids": list(dict.fromkeys(missing)),
                    }
                )
            if not transport_allowed(req.transport, query.transport):
                issues.append(
                    {
                        **base,
                        "code": "query_transport",
                        "message": "路线交通方式必须遵从本次用户设置。",
                        "expected": req.transport,
                        "actual": query.transport,
                    }
                )
        elif isinstance(query, RestaurantQuery) and query.place_id not in known:
            issues.append(
                {
                    **base,
                    "code": "query_identity",
                    "message": "附近餐饮只能查询已知地点周边。",
                    "unknown_ids": [query.place_id],
                }
            )
        elif isinstance(query, PlaceQuery) and _query_name(query.query) not in allowed:
            issues.append(
                {
                    **base,
                    "code": "query_scope",
                    "message": "地点检索名称不属于本次用户明确提出的地点需求。",
                    "query": query.query,
                }
            )
    return issues


_POI_FIELDS = frozenset(
    {
        "id",
        "name",
        "address",
        "type",
        "location",
        "source",
        "city",
        "citycode",
        "district",
        "adcode",
    }
)
_OMIT_FIELDS = frozenset(
    {
        "photos",
        "excerpt",
        "raw",
        "raw_response",
        "raw_payload",
        "provider_response",
        "research_evidence",
    }
)


def packet_context(value):
    """Project POI fields, retaining every selected/required identity and order.

    This is not candidate ranking: no place is removed and no list is truncated.
    Non-POI contracts retain their structure, including facts' quote and sources.
    """
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        poi = isinstance(value.get("id"), str) and isinstance(value.get("name"), str)
        return {
            key: packet_context(item)
            for key, item in value.items()
            if key not in _OMIT_FIELDS and (not poi or key in _POI_FIELDS)
        }
    if isinstance(value, (list, tuple)):
        return [packet_context(item) for item in value]
    return value


def check_draft_identity(
    req: Requirements,
    draft: Itinerary,
    places: Mapping | Iterable,
    hotels: Mapping | Iterable,
) -> list[dict]:
    """Cheap checks before saving a provisional draft; no route/web lookup.

    Full appointment, pacing, overlap, hotel-return and evidence validation still
    runs later. Unknown opening or travel times are never treated as confirmed.
    """
    known, known_hotels = _place_ids(places), _place_ids(hotels)
    issues = []

    def fail(code, message, **details):
        issues.append({"category": "draft_error", "code": code, "message": message, **details})

    visits = {stop.place_id for stop in draft.stops if stop.kind in {"activity", "booking"}}
    for selection in req.selected_attractions:
        if selection.id not in visits:
            fail(
                "must_visit",
                f"草稿遗漏用户已经选定的景点「{selection.name}」。",
                place_id=selection.id,
            )

    if req.hotel_nights and draft.hotel_id not in known_hotels:
        fail("hotel", "住宿须引用已查询的酒店候选。", place_id=draft.hotel_id)
    if not req.hotel_nights and draft.hotel_id:
        fail("hotel", "本次未安排住宿，不应添加酒店。", place_id=draft.hotel_id)
    if req.hotel_nights and req.hotel_id and draft.hotel_id != req.hotel_id:
        fail(
            "hotel_choice", "不能替换用户手选的酒店。", expected=req.hotel_id, actual=draft.hotel_id
        )
    counters = {}
    for stop in draft.stops:
        counters[stop.day] = counters.get(stop.day, 0) + 1
        position = {"day": stop.day, "stop_number": counters[stop.day], "place_id": stop.place_id}
        if stop.day > req.days:
            fail("day", "草稿站点超出本次旅行天数。", **position)
        if stop.place_id not in known:
            fail("identity", "草稿使用了未经查询的地点ID。", **position)
        if stop.kind == "hotel" and stop.place_id != draft.hotel_id:
            fail("hotel_identity", "住宿站点必须对应草稿所选酒店。", **position)
        if stop.end_time <= stop.start_time:
            fail(
                "time",
                "站点结束必须晚于开始。",
                **position,
                scheduled_start=stop.start_time,
                scheduled_end=stop.end_time,
            )
    for day in range(1, req.days + 1):
        if day not in counters:
            fail(
                "missing_day",
                f"草稿遗漏第{day}天，请补齐该天的安排。用餐或住宿也可作为站点。",
                day=day,
            )
    return issues
