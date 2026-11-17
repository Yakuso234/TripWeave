"""Draft-first output and bounded evidence-query contracts.

These contracts describe requests; they neither execute tools nor prove venue
availability. Route identity is just origin, destination and transport. Query
history and execution budgets belong to the caller's current planning run.
"""

from collections.abc import Iterable, Mapping
from typing import Annotated, Literal
from unicodedata import normalize

from pydantic import BaseModel, Field, TypeAdapter

from .domain import Itinerary, Requirements
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


class PlaceQuery(Contract):
    kind: Literal["place_search"]
    query: str = Field(min_length=1, max_length=80)
    reason: Reason


QueryRequest = Annotated[RouteQuery | RestaurantQuery | PlaceQuery, Field(discriminator="kind")]
QUERY_ADAPTER = TypeAdapter(QueryRequest)


class DraftProposal(Contract):
    draft: Itinerary
    query_requests: list[QueryRequest] = Field(max_length=24)
    information_needs: list[PlanningNeed] = Field(max_length=5)
    assumptions: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(max_length=10)


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
    return issues
