"""Real-map planning: deterministic scheduling or a DeepSeek tool loop. No fixture fallback."""

import asyncio
import json
import math
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import httpx2
from langgraph.graph import END, START, StateGraph
from openai import AsyncOpenAI
from pydantic_ai import Agent, ToolOutput
from pydantic_ai.exceptions import (
    ModelConnectionError,
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import RunUsage, UsageLimits

from .api_cost import estimate_cost
from .domain import Itinerary, Requirements, Stop, clock, minutes
from .draft_contracts import (
    DraftPatchError,
    DraftProposal,
    ScopedRevision,
    apply_itinerary_patch,
    check_draft_identity,
    hotel_revision_days,
    packet_context,
)
from .evidence import load_evidence
from .maps import MapClient, MapError, cached_route, distance
from .model_usage import ModelUsageRecorder
from .pacing import daily_metrics, pacing_issues
from .planning_continuation import PlanningContinuation, fact_key
from .planning_contracts import (
    PlanningInformationRequest,
    TimeWindow,
    preflight_requirements,
    validate_research_facts,
    validate_visit_constraints,
)
from .planning_prompts import planner_instructions
from .planning_research import research_limit, research_missing_information
from .planning_review import (
    ReviewContractError,
    ReviewReport,
    ReviewSession,
    validate_positions,
    validate_response,
)
from .query_executor import QueryExecutor, query_key
from .research_evidence import FIELD_NAMES, usable_research_fact
from .route_evidence import hotel_comparisons, planned_legs, route_table
from .settings import Settings


def map_configured(settings: Settings) -> bool:
    return bool(settings.amap_api_key and settings.amap_api_key.get_secret_value().strip())


def same_city(origin: dict, place: dict) -> bool:
    """Compare provider city codes, or exact city names when a code is omitted.

    Basic POI searches can omit citycode while still returning cityname. Never
    infer locality from proximity or from a user-supplied place name.
    """
    origin_code = (origin.get("citycode") or "").strip()
    place_code = (place.get("citycode") or "").strip()
    if origin_code and place_code:
        return origin_code == place_code
    origin_city = (origin.get("city") or "").strip()
    place_city = (place.get("city") or "").strip()
    return bool(origin_city and place_city and origin_city == place_city)


async def discover_attractions(req: Requirements, maps: MapClient) -> list[dict]:
    keywords = [
        "博物馆" if "人文" in req.interests or "展览" in req.interests else "景点",
        "公园" if "自然" in req.interests else "风景名胜",
    ]
    attractions = {}
    for query in dict.fromkeys(keywords):
        for place in await maps.search(query, req.city, "110000|140000", 8):
            attractions.setdefault(place["id"], place)
    return list(attractions.values())


async def prepare(
    req: Requirements, maps: MapClient, trace: list, hotels_only: bool = False
) -> dict:
    origin = await maps.resolve(req.origin_query, req.city, label="出发点")
    booking = (
        await maps.resolve(req.booking_query, req.city, label="预约地点")
        if req.booking_query
        else None
    )
    hotel_keyword = {"balanced": "酒店", "value": "经济型酒店", "riverside": "江景酒店"}[
        req.hotel_preference
    ]
    hotels = await maps.search(hotel_keyword, req.city, "100000", 12) if req.hotel_nights else []
    center = booking or origin
    hotels.sort(key=lambda h: distance(h, center))
    if req.hotel_id and req.hotel_nights:
        if req.hotel_id not in {h["id"] for h in hotels}:
            path = "/v3/place/detail"
            data = await maps.request(path, {"id": req.hotel_id, "extensions": "base"})
            extra = next(
                (p for p in maps.read_places(data, path=path) if p["id"] == req.hotel_id), None
            )
            if not extra or not str(extra["type"]).startswith("住宿"):
                raise MapError("之前选择的住宿已无法确认，请重新查询并选择")
            # A detail lookup ignores the current city filter: a previously selected
            # hotel must not silently survive a destination change.
            if not (origin.get("citycode") and extra.get("citycode")) and not (
                origin.get("city") and extra.get("city")
            ):
                raise MapError("无法确认手选酒店是否位于当前城市，请重新查询住宿候选")
            if not same_city(origin, extra):
                raise MapError("手选酒店不在当前目的地城市，请重新查询并选择住宿")
            hotels.insert(0, extra)
        hotel = next(h for h in hotels if h["id"] == req.hotel_id)
    else:
        hotel = hotels[0] if hotels else None
    if req.hotel_nights and not hotel:
        raise MapError("未找到住宿地点，请更换出发片区或检查地图查询权限")
    rooms, nights = math.ceil(req.party_size / 2), req.hotel_nights
    for h in hotels:
        h.update(
            {
                "nightly_rate": None,
                "estimated_total": None,
                "nights": nights,
                "rooms": rooms,
                "reserved_budget": req.hotel_budget * nights * rooms,
                "availability": "未知",
                "room": "房型、可住人数与早餐待确认",
                "benefits": ["靠近出发或固定预约片区", "来自真实高德酒店POI查询"],
                "tradeoff": "地图未提供可靠的实时房价或空房，需在订房平台核实。",
                "eligible": None,
                "selection_reason": "按出发地点/预约位置的地理接近程度推荐；不把预算上限当作房价。",
            }
        )
    if hotels_only:
        return {
            "origin": origin,
            "booking": booking,
            "hotels": hotels,
            "hotel": hotel,
            "attractions": [],
        }
    attractions = await discover_attractions(req, maps)
    unique = {
        p["id"]: p
        for p in attractions
        if p["id"] not in {origin["id"], booking["id"] if booking else None}
    }
    required = {}
    for query in dict.fromkeys(name.strip() for name in req.must_visit if name.strip()):
        place = await maps.resolve(query, req.city, label="必去景点")
        required[place["id"]] = place
    for selected in req.selected_attractions:
        place = next((p for p in attractions if p["id"] == selected.id), None)
        if place is None:
            path = "/v3/place/detail"
            data = await maps.request(path, {"id": selected.id, "extensions": "base"})
            place = next(
                (p for p in maps.read_places(data, path=path) if p["id"] == selected.id), None
            )
        if not place or place["name"] != selected.name or not same_city(origin, place):
            raise MapError(f"无法确认已选景点「{selected.name}」，请返回选景页重新选择。")
        required[place["id"]] = place
    constrained_places = {}
    for constraint in req.visit_constraints:
        if constraint.place_id:
            place = maps.places.get(constraint.place_id)
            if place is None:
                path = "/v3/place/detail"
                body = await maps.request(path, {"id": constraint.place_id, "extensions": "base"})
                place = next(
                    (
                        p
                        for p in maps.read_places(body, path=path)
                        if p["id"] == constraint.place_id
                    ),
                    None,
                )
            if not place or not same_city(origin, place):
                raise MapError("无法确认景点时间要求中的地点，请核对地点和城市")
            if constraint.place_query and place["name"] != constraint.place_query.strip():
                raise MapError("景点时间要求的地点名称与ID不一致，请重新填写")
        else:
            place = await maps.resolve(constraint.place_query, req.city, label="景点时间要求的地点")
        constrained_places[constraint.constraint_id] = place
        required[place["id"]] = place
    unique.update(required)
    if not unique:
        raise MapError("没有查到可规划的景点，请更换兴趣或目的地")
    trace.append(
        {
            "event": "map",
            "detail": f"查询到{len(unique)}个景点、{len(hotels)}个酒店候选；地点为真实接口结果，房价未知",
        }
    )
    return {
        "origin": origin,
        "booking": booking,
        "hotels": hotels,
        "hotel": hotel,
        "attractions": list(unique.values()),
        "must_visit": list(required.values()),
        "constrained_places": constrained_places,
    }


def costs(req: Requirements, itinerary: Itinerary) -> dict:
    accommodation = req.hotel_budget * req.hotel_nights * math.ceil(req.party_size / 2)
    food = sum(s.kind == "meal" for s in itinerary.stops) * req.party_size * 65
    return {
        "accommodation": accommodation,
        "food": food,
        "tickets": None,
        "transport": None,
        "total": None,
        "subtotal": accommodation + food,
        "budget": req.budget_total,
        "remaining": req.budget_total - accommodation - food,
        "scope": "全体人数估算；住宿按每间每晚期望金额、餐饮按65元/人/餐预留；不是实际报价或最高费用承诺，门票/交通/购物/往返大交通待补充",
        "estimated": True,
        "currency": "CNY",
    }


def buffer_advisories(req, output, data, maps, *, routes=None):
    """Cached evidence only. Low spare time never blocks or starts model repair."""
    if routes is None:
        routes = route_table(req, output, data["origin"]["id"], maps.cache)
    warnings = []
    for row in routes:
        margin = row["buffer_minutes"]
        if margin is None or not 0 <= margin < 5:
            continue
        upper_bound = row["buffer_basis"] == "upper_bound"
        detail = (
            f"站间最多余{margin}分钟，实际出发或休息时间待核对。"
            if upper_bound
            else f"扣除地图路程和已安排的休息后，缓冲约{margin}分钟，可酌情多留一点；此项仅提醒。"
        )
        day, number = row["day"], row["stop_number"]
        warnings.append(
            {
                "category": "advisory",
                "severity": "warning",
                "code": "low_travel_buffer",
                "day": day,
                "stop_number": number,
                "place_id": row["destination_id"],
                "maximum_minutes" if upper_bound else "actual_minutes": margin,
                "suggested_minutes": 5,
                "message": f"第{day}天第{number}站：{detail}",
            }
        )
    return warnings


def preference_deviations(req, output, data, maps, metrics=None):
    """Read cached evidence only; never move a stop or fetch more data."""
    if metrics is None:
        travel = {}
        for day in range(1, req.days + 1):
            previous = output.hotel_id if 2 <= day <= req.hotel_nights + 1 else data["origin"]["id"]
            duration, complete = 0, True
            for stop in (s for s in output.stops if s.day == day):
                leg = cached_route(maps.cache, previous, stop.place_id, req.transport)
                if previous != stop.place_id:
                    if leg is None:
                        complete = False
                    else:
                        duration += leg["duration_minutes"]
                previous = stop.place_id
            travel[day] = duration if complete else None
        metrics = daily_metrics(req, output, data.get("experience_groups", []), travel)
    deviations = [i for i in pacing_issues(metrics) if i["category"] == "preference_deviation"]

    def add(code, message, **details):
        deviations.append(
            {
                "category": "preference_deviation",
                "severity": "warning",
                "code": code,
                "message": message,
                **details,
            }
        )

    for day in range(1, req.days + 1):
        stops = [s for s in output.stops if s.day == day]
        if not stops:
            continue
        origin = output.hotel_id if 2 <= day <= req.hotel_nights + 1 else data["origin"]["id"]
        leg = cached_route(maps.cache, origin, stops[0].place_id, req.transport)
        duration = 0 if origin == stops[0].place_id else leg["duration_minutes"] if leg else None
        if "start_time" not in req.strict_preferences and (
            duration is not None or stops[0].transfer
        ):
            explicit = stops[0].transfer
            departure = (
                minutes(explicit.departure_time)
                if explicit
                else minutes(stops[0].start_time) - duration
            )
            if (
                departure != minutes(req.start_time)
                if explicit
                else 0 <= departure < minutes(req.start_time)
            ):
                add(
                    "preferred_start_time",
                    f"第{day}天{'实际拟定' if explicit else '最迟需'}{clock(departure)}出发，与期望{req.start_time}不同。",
                    day=day,
                    actual=clock(departure),
                    target=req.start_time,
                )
        latest = max(s.end_time for s in stops)
        if "end_time" not in req.strict_preferences and latest > req.end_time:
            add(
                "preferred_end_time",
                f"第{day}天结束于{latest}，晚于期望{req.end_time}。",
                day=day,
                actual=latest,
                target=req.end_time,
            )
    reserved = costs(req, output)
    if reserved["remaining"] < 0:
        add(
            "expected_budget",
            f"住宿与餐饮估算预留{reserved['subtotal']}元，超过期望金额{req.budget_total}元；实际价格仍未核实。",
            actual=reserved["subtotal"],
            target=req.budget_total,
            basis="estimated_reserve",
        )
    return deviations


async def validate(req: Requirements, output: Itinerary, data: dict, maps: MapClient) -> list[dict]:
    errors = []
    travel_by_day: dict[int, int | None] = {}

    def fail(code, message, **details):
        errors.append({"category": "draft_error", "code": code, "message": message, **details})

    hotels = {h["id"]: h for h in data["hotels"]}
    if req.hotel_nights and output.hotel_id not in hotels:
        fail("hotel", "住宿必须引用已查询的真实酒店候选")
    if not req.hotel_nights and output.hotel_id:
        fail("hotel", "未安排住宿的行程不应增加酒店")
    if req.hotel_id and req.hotel_nights and output.hotel_id != req.hotel_id:
        fail("hotel_choice", "不能擅自替换手选酒店")
    if any(s.day > req.days for s in output.stops):
        fail("day", "活动超出旅行天数")
    booking = data["booking"]

    def is_legacy_booking(stop: Stop) -> bool:
        return (
            booking is not None
            and stop.kind == "booking"
            and stop.place_id == booking["id"]
            and stop.day == req.booking_day
            and stop.start_time == req.booking_start
            and stop.end_time == req.booking_end
        )

    def is_fixed_booking(stop: Stop) -> bool:
        return is_legacy_booking(stop) or any(
            constraint.fixed
            and stop.kind == "booking"
            and stop.place_id
            == data.get("constrained_places", {}).get(constraint.constraint_id, {}).get("id")
            and stop.day == constraint.windows[0].day
            and stop.start_time == constraint.windows[0].start_time
            and stop.end_time == constraint.windows[0].end_time
            for constraint in req.visit_constraints
        )

    visits: dict[str, list[tuple[Stop, int]]] = {}
    booking_occurrences: list[tuple[Stop, int]] = []
    for day in range(1, req.days + 1):
        previous = (
            data["origin"]
            if not 2 <= day <= req.hotel_nights + 1
            else hotels.get(output.hotel_id, data["origin"])
        )
        end = minutes(req.start_time) if "start_time" in req.strict_preferences else 0
        day_stops = [s for s in output.stops if s.day == day]
        if day <= req.hotel_nights and (
            not day_stops
            or day_stops[-1].kind != "hotel"
            or day_stops[-1].place_id != output.hotel_id
        ):
            fail("hotel_return", f"第{day}天最后一站必须返回所选酒店，类型须为 hotel", day=day)
        travel_by_day[day] = 0
        for stop_number, stop in enumerate(day_stops, start=1):
            target = maps.places.get(stop.place_id)
            if not target:
                travel_by_day[day] = None
                fail("identity", "模型使用了未经查询的地点", day=day, stop_number=stop_number)
                continue
            if stop.kind == "hotel" and stop.place_id != output.hotel_id:
                fail("hotel_identity", "返回住宿的地点必须与所选酒店一致")
            if booking and stop.place_id == booking["id"]:
                booking_occurrences.append((stop, stop_number))
            elif stop.kind == "booking" and not is_fixed_booking(stop):
                fail(
                    "booking_identity",
                    f"第{day}天第{stop_number}站（{target['name']}）不是用户确认的固定预约",
                    day=day,
                    stop_number=stop_number,
                    place_id=stop.place_id,
                )
            leg = await maps.route(previous, target, req.city, req.transport)
            travel_minutes = leg["duration_minutes"]
            if travel_by_day[day] is not None:
                travel_by_day[day] += travel_minutes
            timing = stop.transfer
            departure = minutes(timing.departure_time) if timing else end
            if departure < end:
                fail(
                    "departure_before_previous_end",
                    f"第{day}天第{stop_number}站：实际出发{clock(departure)}早于上一站结束{clock(end)}",
                    day=day,
                    stop_number=stop_number,
                    place_id=stop.place_id,
                    departure_time=clock(departure),
                    previous_end=clock(end),
                )
            minimum_arrival = departure + travel_minutes
            if timing and minutes(timing.arrival_time) < minimum_arrival:
                fail(
                    "transfer_duration",
                    f"第{day}天第{stop_number}站：{timing.departure_time}出发、{timing.arrival_time}到达，所留时间不足地图所需{travel_minutes}分钟",
                    day=day,
                    stop_number=stop_number,
                    place_id=stop.place_id,
                    field="transfer.arrival_time",
                    departure_time=timing.departure_time,
                    scheduled_arrival=timing.arrival_time,
                    available_minutes=minutes(timing.arrival_time) - departure,
                    travel_minutes=travel_minutes,
                    shortfall_minutes=minimum_arrival - minutes(timing.arrival_time),
                )
            arrival = (
                max(minimum_arrival, minutes(timing.arrival_time)) if timing else minimum_arrival
            )
            rest_end = arrival
            if timing:
                for field, rests, lower, upper in (
                    ("rest_before", timing.rest_before, end if stop_number > 1 else 0, departure),
                    ("rest_after", timing.rest_after, arrival, minutes(stop.start_time)),
                ):
                    previous_rest_end = lower
                    for rest_number, rest in sorted(
                        enumerate(rests, 1), key=lambda item: item[1].start_time
                    ):
                        start_rest, end_rest = minutes(rest.start_time), minutes(rest.end_time)
                        if start_rest < previous_rest_end or end_rest > upper:
                            fail(
                                "rest_timing",
                                f"第{day}天第{stop_number}站：{field}第{rest_number}段休息{rest.start_time}–{rest.end_time}与上一站、交通或下一站冲突",
                                day=day,
                                stop_number=stop_number,
                                place_id=stop.place_id,
                                field=f"transfer.{field}[{rest_number - 1}]",
                                expected_start_not_before=lower,
                                expected_end_not_after=upper,
                                actual_start=rest.start_time,
                                actual_end=rest.end_time,
                            )
                        previous_rest_end = max(previous_rest_end, end_rest)
                    if field == "rest_after" and rests:
                        rest_end = max(rest_end, max(minutes(r.end_time) for r in rests))
            earliest = rest_end
            if minutes(stop.start_time) < earliest:
                day_offset, minute_of_day = divmod(earliest, 24 * 60)
                arrival_time = clock(minute_of_day)
                arrival_label = (
                    f"第{day + day_offset}天 {arrival_time}" if day_offset else arrival_time
                )
                fail(
                    "connection",
                    f"第{day}天第{stop_number}站：从{previous['name']} {clock(departure)}出发，"
                    f"到{target['name']}需{travel_minutes}分钟，"
                    f"原定{stop.start_time}开始，扣除已安排休息后最早{arrival_label}开始本站",
                    day=day,
                    stop_number=stop_number,
                    origin_id=previous["id"],
                    destination_id=target["id"],
                    departure_time=clock(departure),
                    travel_minutes=travel_minutes,
                    buffer_minutes=timing.buffer_minutes if timing else 0,
                    rest_after=[r.model_dump() for r in timing.rest_after] if timing else [],
                    scheduled_start=stop.start_time,
                    available_minutes=minutes(stop.start_time) - departure,
                    required_minutes=earliest - departure,
                    shortfall_minutes=earliest - minutes(stop.start_time),
                    earliest_arrival_day=day + arrival // (24 * 60),
                    earliest_arrival_time=clock(arrival % (24 * 60)),
                    earliest_start_day=day + day_offset,
                    earliest_start_time=arrival_time,
                )
            if minutes(stop.end_time) <= minutes(stop.start_time) or (
                "end_time" in req.strict_preferences
                and minutes(stop.end_time) > minutes(req.end_time)
            ):
                fail(
                    "time",
                    f"第{day}天第{stop_number}站（{target['name']}）安排为"
                    f"{stop.start_time}–{stop.end_time}，结束必须晚于开始且不晚于{req.end_time}",
                    day=day,
                    stop_number=stop_number,
                    place_id=target["id"],
                    scheduled_start=stop.start_time,
                    scheduled_end=stop.end_time,
                    latest_end=req.end_time,
                )
            if stop.kind in ("activity", "booking"):
                visits.setdefault(stop.place_id, []).append((stop, stop_number))
            previous, end = target, minutes(stop.end_time)
    errors.extend(
        i
        for i in pacing_issues(
            daily_metrics(req, output, data.get("experience_groups", []), travel_by_day)
        )
        if i["category"] == "draft_error"
    )
    group_by_id = {
        member: group["group_id"]
        for group in data.get("experience_groups", [])
        for member in group["members"]
    }
    grouped_visits = {}
    for place_id, occurrences in visits.items():
        grouped_visits.setdefault(group_by_id.get(place_id, place_id), []).extend(occurrences)
    for group_id, occurrences in grouped_visits.items():
        # A real visit may resume after lunch on the same day. Count one place,
        # retain both time segments, and still check each connection/window.
        # Different POI aliases and repeated visits on other days remain checked.
        representatives = {}
        for stop, number in occurrences:
            key = (stop.day, stop.place_id)
            if key not in representatives or is_fixed_booking(stop):
                representatives[key] = (stop, number)
        occurrences = list(representatives.values())
        # Distinct user-confirmed appointments may legitimately repeat a venue.
        fixed = [occurrence for occurrence in occurrences if is_fixed_booking(occurrence[0])]
        if fixed:
            occurrences = [fixed[0]] + [
                occurrence for occurrence in occurrences if not is_fixed_booking(occurrence[0])
            ]
        if len(occurrences) < 2:
            continue
        occurrences.sort(key=lambda item: (item[0].day, item[1]))
        place_id = occurrences[0][0].place_id
        name = " / ".join(dict.fromkeys(maps.places[s.place_id]["name"] for s, _ in occurrences))
        keep_index = next(
            (i for i, (stop, _) in enumerate(occurrences) if is_fixed_booking(stop)), None
        )
        if keep_index is None:
            locations = [
                {
                    "day": stop.day,
                    "stop_number": number,
                    "start_time": stop.start_time,
                    "place_id": stop.place_id,
                    "place_name": maps.places[stop.place_id]["name"],
                }
                for stop, number in occurrences
            ]
            positions = "、".join(
                f"第{item['day']}天第{item['stop_number']}站" for item in locations
            )
            fail(
                "duplicate",
                f"「{name}」重复安排在{positions}。根据路线和时间选择保留一站，修订其余重复安排",
                place_id=place_id,
                place_name=name,
                experience_group=group_id,
                occurrences=locations,
                related_days=sorted({stop.day for stop, _ in occurrences}),
            )
            continue
        kept, kept_number = occurrences[keep_index]
        for i, (stop, stop_number) in enumerate(occurrences):
            if i == keep_index:
                continue
            fail(
                "duplicate",
                f"「{name}」重复安排。保留第{kept.day}天第{kept_number}站，"
                f"请修订第{stop.day}天第{stop_number}站",
                place_id=stop.place_id,
                place_name=maps.places[stop.place_id]["name"],
                experience_group=group_id,
                day=stop.day,
                stop_number=stop_number,
                keep_day=kept.day,
                keep_stop_number=kept_number,
                keep_kind=kept.kind,
                keep_place_id=kept.place_id,
            )
    for place in data.get("must_visit", []):
        if place["id"] not in visits:
            fail("must_visit", f"未安排必去景点：{place['name']}")
    if booking and not any(is_legacy_booking(stop) for stop, _ in booking_occurrences):
        occurrences = [
            {
                "stop_number": number,
                **stop.model_dump(include={"day", "kind", "start_time", "end_time"}),
            }
            for stop, number in booking_occurrences
        ]
        visit_days = {stop.day for stop, _ in visits.get(booking["id"], [])}
        actual = "、".join(
            f"第{stop.day}天第{number}站 {stop.start_time}–{stop.end_time}（{stop.kind}）"
            for stop, number in booking_occurrences
        )
        fail(
            "booking",
            f"第{req.booking_day}天 {req.booking_start}–{req.booking_end} 必须保留"
            f"「{booking['name']}」的固定预约（booking）。"
            + (f"草稿中该地点排在{actual}" if actual else "草稿中缺少该地点"),
            day=req.booking_day,
            place_id=booking["id"],
            place_name=booking["name"],
            expected_start=req.booking_start,
            expected_end=req.booking_end,
            expected_kind="booking",
            occurrences=occurrences,
            required_days=sorted(visit_days | {req.booking_day}),
        )
    errors.extend(validate_visit_constraints(req, output, data.get("constrained_places", {})))
    errors.extend(validate_research_facts(output, data.get("planning_facts", [])))
    return errors


async def schedule_day(req, data, maps, day, stops, used):
    hotel, booking = data["hotel"], data["booking"]
    duration = {"relaxed": 90, "balanced": 60, "full": 45}[req.pace]
    previous = data["origin"] if not 2 <= day <= req.hotel_nights + 1 else hotel
    now, end_limit, count = minutes(req.start_time), minutes(req.end_time), 0
    fixed = booking if booking and req.booking_day == day else None

    async def add(target, start, end, kind, title, reason, day=day):
        nonlocal previous, now, count
        stops.append(
            Stop(
                day=day,
                place_id=target["id"],
                start_time=clock(start),
                end_time=clock(end),
                kind=kind,
                title=title,
                reason=reason,
            )
        )
        previous, now = target, end
        if kind in ("activity", "booking"):
            used.add(target["id"])
            count += 1

    async def fill(limit, maximum):
        nonlocal count
        for target in sorted(data["attractions"], key=lambda p: distance(p, previous)):
            if target["id"] in used or count >= maximum:
                continue
            leg = await maps.route(previous, target, req.city, req.transport)
            start = now + leg["duration_minutes"] + 10
            end = start + duration
            latest = limit
            if fixed and now < minutes(req.booking_start):
                next_leg = await maps.route(target, fixed, req.city, req.transport)
                latest = min(latest, minutes(req.booking_start) - next_leg["duration_minutes"] - 15)
            if end <= latest:
                await add(
                    target,
                    start,
                    end,
                    "activity",
                    target["name"],
                    f"预计停留{duration}分钟，预留10分钟找入口与缓冲；开放与门票需确认。",
                )

    cap = req.max_attractions if req.max_attractions is not None else 20
    await fill(min(12 * 60, end_limit), min(2, cap - bool(fixed)))

    async def add_lunch(before_booking: bool) -> bool:
        lunch = max(now, 12 * 60)
        latest = min(end_limit, minutes(req.booking_start)) if before_booking else end_limit
        if lunch + 50 > latest or lunch > 14 * 60 + 30:
            return False
        restaurants = await maps.nearby(previous)
        target = restaurants[0] if restaurants else previous
        leg = await maps.route(previous, target, req.city, req.transport)
        start = lunch + leg["duration_minutes"]
        if start + 50 > latest:
            return False
        if before_booking:
            next_leg = await maps.route(target, fixed, req.city, req.transport)
            if start + 50 + next_leg["duration_minutes"] > latest:
                return False
        await add(
            target,
            start,
            start + 50,
            "meal",
            "午餐 · " + (target["name"] if restaurants else "周边自行选择"),
            "按65元/人预留，菜单、营业和排队需核实；不执行订位。",
        )
        return True

    lunch_added = await add_lunch(before_booking=bool(fixed))
    if fixed:
        await maps.route(previous, fixed, req.city, req.transport)
        await add(
            fixed,
            minutes(req.booking_start),
            minutes(req.booking_end),
            "booking",
            "固定预约 · " + fixed["name"],
            "保留用户指定时段；请自行完成真实预约。",
        )
        if not lunch_added:
            await add_lunch(before_booking=False)
    await fill(min(18 * 60, end_limit), cap)
    dinner = max(now, 18 * 60)
    if dinner + 70 <= end_limit:
        restaurants = await maps.nearby(previous)
        target = restaurants[0] if restaurants else previous
        leg = await maps.route(previous, target, req.city, req.transport)
        start = dinner + leg["duration_minutes"]
        if start + 50 <= end_limit:
            await add(
                target,
                start,
                start + 50,
                "meal",
                "晚餐 · " + (target["name"] if restaurants else "周边自行选择"),
                "按65元/人预留，真实消费与营业状态待确认。",
            )
    if hotel and day <= req.hotel_nights:
        leg = await maps.route(previous, hotel, req.city, req.transport)
        start = now + leg["duration_minutes"] + 10
        if start + 20 <= end_limit:
            await add(
                hotel,
                start,
                start + 20,
                "hotel",
                "入住 / 返回 · " + hotel["name"],
                "房型、空房与房价未验证；此条是计划，不是已预订。",
            )


async def schedule(req: Requirements, data: dict, maps: MapClient) -> Itinerary:
    stops, used = [], set()
    hotel = data["hotel"]
    for day in range(1, req.days + 1):
        await schedule_day(req, data, maps, day, stops, used)
    return Itinerary(
        summary=f"基于真实高德地点与路线，为{req.party_size}人安排{req.days}天{req.city}行程。按距离与可用时间排程，保留固定预约。",
        hotel_id=hotel["id"] if hotel else None,
        stops=stops,
    )


class PlanningState(TypedDict, total=False):
    candidate: Itinerary
    issues: list[dict]
    messages: list[ModelMessage]
    attempts: int
    error: str | None
    outcome: str | None
    information_request: PlanningInformationRequest | None
    research_rounds: int
    research_facts: list[dict]
    research_diagnostics: list[dict]
    research_requests: list[dict]
    research_search_calls: int
    research_limits: list[dict]
    facts_updated: bool
    evidence_revision_days: list[int] | None
    review: dict
    query_requests: list
    query_log: list[dict]
    evidence_results: list[dict]
    planning_phase: str
    assumptions: list[str]
    light_check_failed: bool


def model_context(value):
    """Project shared input without images, raw web text or duplicated research."""
    if isinstance(value, dict):
        return {
            key: model_context(item)
            for key, item in value.items()
            if key
            not in {
                "photos",
                "excerpt",
                "experience_groups",
                "research_evidence",
                "research_history",
                "research_feedback",
            }
        }
    if isinstance(value, list):
        return [model_context(item) for item in value]
    return value


def information_issues(request: PlanningInformationRequest) -> list[dict]:
    return [
        {
            "category": "missing_fact",
            "code": "critical_information_missing",
            "place_id": need.place_id,
            "day": need.day,
            "missing_fields": need.fields,
            "message": need.question,
        }
        for need in request.needs
    ]


def unresolved_critical_facts(
    output: Itinerary, needs: list[dict], facts: list[dict]
) -> list[dict]:
    """A fact for one date cannot silently follow a venue to a different date."""
    fields_by_place = {}
    for need in needs:
        fields_by_place.setdefault(need["place_id"], set()).update(need["fields"])
    available = {
        (fact["place_id"], fact["day"], fact["field"])
        for fact in facts
        if usable_research_fact(fact)
    }
    issues, counters = [], {}
    for stop in output.stops:
        counters[stop.day] = counters.get(stop.day, 0) + 1
        if stop.kind not in ("activity", "booking"):
            continue
        missing = sorted(
            field
            for field in fields_by_place.get(stop.place_id, set())
            if (stop.place_id, stop.day, field) not in available
        )
        if missing:
            issues.append(
                {
                    "category": "missing_fact",
                    "code": "critical_fact_date_missing",
                    "day": stop.day,
                    "stop_number": counters[stop.day],
                    "place_id": stop.place_id,
                    "missing_fields": missing,
                    "message": f"第{stop.day}天第{counters[stop.day]}站仍缺当日关键资料："
                    f"{'、'.join(FIELD_NAMES[field] for field in missing)}。"
                    "其他日期的资料不能沿用；本轮不再自动补查，请核对信息或调整要求。",
                }
            )
    return issues


async def preflight_transfers(req: Requirements, data: dict, maps: MapClient) -> list[dict]:
    """Check necessary travel bounds only between user-fixed commitments."""
    fixed = {}

    def add(place, window, constraint_id):
        key = (window.day, window.start_time, window.end_time, place["id"])
        item = fixed.setdefault(key, {"place": place, "window": window, "constraint_ids": []})
        item["constraint_ids"].append(constraint_id)

    for constraint in req.visit_constraints:
        if constraint.fixed:
            add(
                data["constrained_places"][constraint.constraint_id],
                constraint.windows[0],
                constraint.constraint_id,
            )
    if data.get("booking"):
        add(
            data["booking"],
            TimeWindow(day=req.booking_day, start_time=req.booking_start, end_time=req.booking_end),
            "booking",
        )
    issues = []
    for day in range(1, req.days + 1):
        commitments = sorted(
            (item for key, item in fixed.items() if key[0] == day),
            key=lambda item: item["window"].start_time,
        )
        if not commitments:
            continue
        origin = (
            data["origin"]
            if not 2 <= day <= req.hotel_nights + 1
            else data.get("hotel")
            if req.hotel_id
            else None
        )
        previous = origin if "start_time" in req.strict_preferences else None
        departure, ids = req.start_time, ["daily_departure"]
        for item in commitments:
            target, window = item["place"], item["window"]
            if previous:
                leg = await maps.route(previous, target, req.city, req.transport)
                travel = leg["duration_minutes"]
                if minutes(departure) + travel > minutes(window.start_time):
                    issues.append(
                        {
                            "category": "hard_conflict",
                            "code": "fixed_connection_unreachable",
                            "constraint_ids": ids + item["constraint_ids"],
                            "day": day,
                            "place_ids": [previous["id"], target["id"]],
                            "expected": {"earliest_departure": departure, "travel_minutes": travel},
                            "actual": {"fixed_start": window.start_time},
                            "source": leg.get("source"),
                            "queried_at": leg.get("queried_at"),
                            "message": f"第{day}天从{previous['name']}最早{departure}出发，到{target['name']}地图需{travel}分钟，"
                            f"赶不上用户固定的{window.start_time}预约。请确认调整出发或预约要求。",
                        }
                    )
            previous, departure, ids = target, window.end_time, item["constraint_ids"]
    return issues


async def run_planning_graph(
    agent: Agent,
    req: Requirements,
    maps: MapClient,
    data: dict,
    trace: list[dict],
    previous_plan: dict | None,
    settings: Settings,
    usage: RunUsage,
    research_context: dict | None = None,
    *,
    researcher=None,
    review_session,
    progress: dict | None = None,
    on_draft=None,
    recorder=None,
    continuation: PlanningContinuation | None = None,
) -> PlanningState:
    # This in-process snapshot survives a node exception so a failed draft can
    # be stored with the candidate result. It is not a LangGraph checkpoint.
    progress = {} if progress is None else progress

    def record(update):
        progress.update(update)
        return update

    executor = None
    repeated = {}
    checked = set()
    delivered_meal_decisions = set()
    research_attempts = dict(continuation.search_attempts) if continuation else {}
    informed_limits = {fact_key(item): item for item in continuation.limits} if continuation else {}
    supplied_keys = (
        {
            fact_key(fact)
            for fact in continuation.facts
            if fact.get("evidence_status") == "user_reported"
        }
        if continuation
        else set()
    )

    def limit_for(key):
        return (
            "user_fact_supplied" if key in supplied_keys else research_limit(key, research_attempts)
        )

    def draft_identity(draft):
        return {
            "hotel_id": draft.hotel_id,
            "stops": [
                s.model_dump(
                    include={"day", "place_id", "start_time", "end_time", "kind", "transfer"}
                )
                for s in draft.stops
            ],
        }

    def known_places():
        return {
            pid: place
            for pid, place in maps.places.items()
            if executor is not None and pid in executor.allowed_place_ids
        }

    async def save_draft(state, phase):
        if on_draft:
            await on_draft(
                {
                    "draft": state["candidate"].model_dump(mode="json"),
                    "phase": phase,
                    "places": list(known_places().values()),
                    "route_table": route_table(
                        req, state["candidate"], data["origin"]["id"], maps.cache
                    ),
                    "query_log": state.get("query_log", []),
                    "query_requests": [
                        q.model_dump(mode="json") if hasattr(q, "model_dump") else q
                        for q in state.get("query_requests", [])
                    ],
                    "pending_information": state.get("information_request").model_dump(mode="json")
                    if state.get("information_request")
                    else None,
                    "assumptions": state.get("assumptions", []),
                }
            )

    async def preflight(_: PlanningState) -> dict:
        issues = preflight_requirements(req)
        trace.append(
            {
                "event": "preflight",
                "detail": "；".join(i["message"] for i in issues)
                or "已声明硬条件预检通过，继续核实地点",
            }
        )
        initial = {
            "attempts": 0,
            "query_requests": [],
            "query_log": [],
            "evidence_results": [],
            "planning_phase": "first_draft",
            "assumptions": [],
            "research_rounds": 0,
            "research_facts": [],
            "research_diagnostics": [],
            "research_requests": [],
            "research_search_calls": 0,
            "facts_updated": False,
            "review": {"version": 1, "status": "pending"},
            "issues": issues,
            "outcome": "needs_user_decision" if issues else None,
        }
        if continuation and not issues:
            initial.update(
                candidate=continuation.draft,
                planning_phase="revision",
                facts_updated=True,
                research_facts=continuation.facts,
                research_requests=continuation.requests,
                research_diagnostics=continuation.diagnostics,
                research_limits=continuation.limits,
                evidence_results=list(continuation.query_results.values()),
            )
        return record(initial)

    async def prepare_evidence(_: PlanningState) -> dict:
        nonlocal executor
        data.update(await prepare(req, maps, trace))
        if research_context:
            data["experience_groups"] = research_context["experience_groups"]
            groups = {
                member: group["group_id"]
                for group in data["experience_groups"]
                for member in group["members"]
            }
            data["selected_attraction_research"] = {
                **{
                    key: value
                    for key, value in research_context.items()
                    if key not in ("selected", "experience_groups")
                },
                "selected": [
                    {**place, "experience_group": groups[place["place_id"]]}
                    for place in research_context["selected"]
                ],
            }
            trace.append(
                {
                    "event": "research_context",
                    "detail": f"复用{len(research_context['selected'])}个已选地点的资料快照；未重新检索网页",
                }
            )
        if previous_plan and previous_plan.get("revision_request"):
            for place in previous_plan["places"]:
                maps.places.setdefault(place["id"], place)
        if continuation:
            for place in continuation.places:
                maps.places.setdefault(place["id"], place)
            for key, entry in continuation.query_results.items():
                if key[0] == "route":
                    maps.cache.setdefault(key[1:], entry["data"])
            data["planning_facts"] = continuation.facts
            data["critical_needs"] = continuation.critical_needs
            trace.append(
                {"event": "user_facts", "detail": "从已保存草稿继续，使用用户补充的资料。"}
            )
        executor = QueryExecutor(req, maps, trace)
        if continuation:
            executor.cache.update(continuation.query_results)
        resolved = {**data.get("constrained_places", {}), "booking": data.get("booking")}
        issues = preflight_requirements(req, resolved)
        if not issues:
            issues = await preflight_transfers(req, data, maps)
        if issues:
            trace.append({"event": "hard_conflict", "detail": "已核实地点对应的固定要求互斥"})
        return record({"issues": issues, "outcome": "needs_user_decision" if issues else None})

    async def propose(state: PlanningState) -> dict:
        if settings.max_requests is not None and usage.requests >= settings.max_requests:
            return record(
                {"outcome": "incomplete", "error": "模型轮次预算已用完，已保留当前草稿与证据。"}
            )
        phase = "first_draft" if not state.get("candidate") else "revision"
        prompt = {
            "task": phase,
            "requirements": {**req.model_dump(mode="json"), "hotel_nights": req.hotel_nights},
            "queried_data": packet_context(
                model_context(
                    {key: value for key, value in data.items() if key != "planning_facts"}
                )
            ),
            "known_places": packet_context(list(known_places().values())),
            "revision_request": (previous_plan or {}).get("revision_request"),
            "previous_plan": model_context(previous_plan) if phase == "first_draft" else None,
            "draft": state["candidate"].model_dump(mode="json") if state.get("candidate") else None,
            "evidence_results": state.get("evidence_results", []),
            "planning_facts": data.get("planning_facts", []),
            "research_feedback": state.get("research_diagnostics", []),
            "research_limits": state.get("research_limits", []),
            "validation_issues": state.get("issues", []),
            "instruction": "按本轮输出合同提交行程、结构化查询清单、关键补查需求和暂定假设。"
            "仅根据已提供证据修订；普通工具不可直接调用。查询用明确起点/终点/交通方式，"
            "资料问题关联具体用户要求。纯介绍性未知放assumptions。"
            "已查询结果不要反复索取。首稿允许拟定时刻，营业和预订未知必须保留未知。",
        }
        if phase == "first_draft":
            prompt["instruction"] = (
                "先交完整初稿与结构化查询清单、关键补查问题、暂定假设。"
                "使用已有证据，未知路线可暂估并列查询，不声称已核实。"
                "检查已知硬要求、地点身份、日期和基础时间结构后提交；"
                "不为软目标小幅偏离反复优化，说明简短。"
            )
        elif continuation:
            prompt["instruction"] += (
                "从当前候选草稿接续。planning_facts中user_reported是用户对指定地点及日期提供的资料，"
                "直接用于相应字段，不重复补查，优先保留未受影响的安排。"
                "若当天必须预约，注明预约入口（已知时）、拟定入场时段、预约成功后才执行；"
                "余票与预约完成情况仍未知，不能写成用户已预约。"
            )
        if phase == "revision" and req.hotel_nights and not req.hotel_id:
            hotel_ids = {hotel["id"] for hotel in data["hotels"]}
            prompt["hotel_route_evidence"] = [
                {
                    "origin_id": origin_id,
                    "destination_id": destination_id,
                    "transport": transport,
                    "mode": route["mode"],
                    "duration_minutes": route["duration_minutes"],
                }
                for (origin_id, destination_id, transport), route in maps.cache.items()
                if origin_id in hotel_ids or destination_id in hotel_ids
            ]
            prompt["hotel_comparisons"] = hotel_comparisons(
                req, state["candidate"], data["hotels"], maps.cache
            )
        if phase == "revision":
            prompt["route_table"] = route_table(
                req, state["candidate"], data["origin"]["id"], maps.cache
            )
        # Local repairs use day replacements. When the scope is unknown, the
        # model can still return a new complete draft.
        issues = state.get("issues", [])
        affected = [
            set(
                ([issue["day"]] if "day" in issue else [])
                + issue.get("related_days", [])
                + issue.get("required_days", [])
            )
            for issue in issues
        ]
        evidence_days = state.get("evidence_revision_days")
        days = set()
        if (
            phase == "revision"
            and all(group and group <= set(range(1, req.days + 1)) for group in affected)
            and (not state.get("facts_updated") or evidence_days is not None)
        ):
            days = set(evidence_days or ()).union(*affected)
        if days:
            prompt["revision_days"] = sorted(days)
            prompt["hotel_revision_days"] = sorted(hotel_revision_days(req))
        trace.append(
            {
                "event": "agent",
                "role": "planner",
                "phase": phase,
                **({"revision_days": sorted(days)} if days else {}),
                "detail": "规划Agent先交草稿和查询清单"
                if phase == "first_draft"
                else "规划Agent根据最新草稿、证据和具体问题修订",
            }
        )
        try:
            with recorder.stage(phase) if recorder else nullcontext():
                response = await agent.run(
                    json.dumps(prompt, ensure_ascii=False),
                    instructions=planner_instructions(phase, scoped=bool(days)),
                    output_type=ToolOutput(
                        ScopedRevision if days else DraftProposal,
                        name="draft_proposal",
                        strict=False,
                    ),
                    usage=usage,
                    usage_limits=UsageLimits(request_limit=settings.max_requests),
                    model_settings=settings.generation_settings("planner"),
                )
        except UsageLimitExceeded:
            return record(
                {"outcome": "incomplete", "error": "模型轮次预算已用完，当前草稿未通过完整流程。"}
            )
        output = response.output
        if isinstance(output, ScopedRevision):
            try:
                candidate = apply_itinerary_patch(req, state["candidate"], output.patch, days)
            except DraftPatchError as exc:
                return record({"outcome": "incomplete", "error": str(exc)})
        else:
            candidate = output.draft
        request = (
            PlanningInformationRequest(
                needs=output.information_needs,
                reason="；".join(n.question for n in output.information_needs)[:500],
            )
            if output.information_needs
            else None
        )
        update = {
            "candidate": candidate,
            "query_requests": output.query_requests,
            "assumptions": output.assumptions,
            "information_request": request,
            "issues": check_draft_identity(req, candidate, known_places(), data["hotels"]),
            "facts_updated": False,
            "evidence_revision_days": None,
            "outcome": None,
            "planning_phase": phase,
        }
        if (
            state.get("candidate")
            and state.get("issues")
            and not state.get("facts_updated")
            and draft_identity(candidate) == draft_identity(state["candidate"])
        ):
            update["issues"] = update["issues"] + [
                i
                for i in state["issues"]
                if i.get("code") not in {"query_failed", "route_queries_missing"}
                and not any(
                    (i.get("code"), i.get("place_id"), i.get("day"))
                    == (current.get("code"), current.get("place_id"), current.get("day"))
                    for current in update["issues"]
                )
            ]
        if request:
            update["research_requests"] = state.get("research_requests", []) + [
                request.model_dump(mode="json")
            ]
            if any(n.place_id not in known_places() or n.day > req.days for n in request.needs):
                update.update(
                    outcome="incomplete",
                    error="补查请求包含未核实地点或旅行范围外日期，未执行研究。",
                    issues=[
                        {
                            "category": "missing_fact",
                            "code": "invalid_research_scope",
                            "message": "补查地点和日期必须属于本次已知旅行范围。",
                        }
                    ],
                )
        record(update)
        await save_draft({**state, **update}, phase)
        return update

    def required_routes(draft):
        routes, days = [], set()
        for day, _, origin, _, stop in planned_legs(req, draft, data["origin"]["id"]):
            if (
                origin in maps.places
                and stop.place_id in maps.places
                and origin != stop.place_id
                and cached_route(maps.cache, origin, stop.place_id, req.transport) is None
            ):
                routes.append(
                    {
                        "kind": "route",
                        "origin_id": origin,
                        "destination_id": stop.place_id,
                        "transport": req.transport,
                        "reason": f"核实第{day}天到下一站的交通",
                    }
                )
                days.add(day)
        return routes, sorted(days)

    async def collect_queries(state):
        revision = (previous_plan or {}).get("revision_request", "")
        executor.allow_query_names(
            [
                q.query
                for q in state.get("query_requests", [])
                if getattr(q, "kind", None) == "place_search" and q.query in revision
            ]
        )
        draft = state["candidate"]
        light_issues = check_draft_identity(req, draft, known_places(), data["hotels"])
        initial_missing = (
            required_routes(draft)[0]
            if state["planning_phase"] == "first_draft" and not light_issues
            else []
        )
        comparison_before = hotel_comparisons(req, draft, data["hotels"], maps.cache)
        if light_issues:
            result = {
                "results": [
                    {
                        "query": q.model_dump(mode="json"),
                        "status": "not_executed",
                        "error": "草稿存在结构或身份问题，先修订草稿后再查询。",
                        "data": None,
                        "cache_hit": False,
                        "phase": state["planning_phase"],
                        "model_round": usage.requests,
                    }
                    for q in state.get("query_requests", [])
                ],
                "new_information": False,
            }
        else:
            result = await executor.execute(
                state.get("query_requests", []),
                phase=state["planning_phase"],
                model_round=usage.requests,
            )
        results = result["results"]
        log = state.get("query_log", []) + results
        missing, missing_days = ([], []) if light_issues else required_routes(draft)
        issues = state["issues"] + [i for i in light_issues if i not in state["issues"]]
        comparison_after = hotel_comparisons(req, draft, data["hotels"], maps.cache)
        decision_days = {
            leg["day"]
            for key, legs in comparison_after.items()
            if not light_issues
            and comparison_before.get(key) != legs
            and any(leg["delta_minutes"] < 0 for leg in legs)
            for leg in legs
        }
        remaining_route_keys = {query_key(query) for query in missing}
        # A first draft may have built the trip around unverified links. Give it
        # a first evidence pass when any required link is actually resolved,
        # but do not repeat this on subsequent deliveries of the same estimates.
        trip_wide_decision = any(
            query_key(query) not in remaining_route_keys for query in initial_missing
        )
        for item in results if not light_issues else []:
            query = item["query"]
            kind = query["kind"]
            required_meal = (
                kind == "nearby_restaurants" and query.get("purpose") == "required_for_plan"
            )
            meal_days = (
                {
                    stop.day
                    for stop in draft.stops
                    if stop.kind == "meal" and stop.place_id == query["place_id"]
                }
                if required_meal
                else set()
            )
            if item["status"] == "ok":
                if kind == "place_search" and item["new_information"]:
                    trip_wide_decision = True
                if required_meal and item["data"]:
                    key = query_key(query)
                    if key not in delivered_meal_decisions:
                        delivered_meal_decisions.add(key)
                        if meal_days:
                            decision_days.update(meal_days)
                        else:
                            trip_wide_decision = True
                if not required_meal or item["data"]:
                    continue
            elif not (
                required_meal
                or kind == "place_search"
                or (item["status"] == "not_executed" and item.get("code") != "map_budget_exhausted")
            ):
                # Missing routes needed by the draft are reported below. A failed
                # optional comparison or dining suggestion can be read in the log.
                continue
            issues.append(
                {
                    "category": "draft_error",
                    "code": "query_failed",
                    "message": item["error"]
                    or "当前用餐要求所依赖的查询没有返回候选，请调整安排。",
                    "query": query,
                    **({"related_days": sorted(meal_days)} if meal_days else {}),
                }
            )
        if missing:
            issues.append(
                {
                    "category": "draft_error",
                    "code": "route_queries_missing",
                    "message": "草稿仍有未核实路段，请用起点和终点提交查询清单。",
                    "required_queries": missing,
                    "related_days": missing_days,
                }
            )
        fingerprint = json.dumps(
            {
                "draft": draft_identity(state["candidate"]),
                "queries": sorted({query_key(r["query"]) for r in results}, key=repr),
                "facts": state.get("research_facts", []),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        count = repeated.get(fingerprint, 0)
        repeated[fingerprint] = count + 1
        update = {
            "evidence_results": results,
            "query_log": log,
            "issues": issues,
            "facts_updated": trip_wide_decision or bool(decision_days),
            "evidence_revision_days": None if trip_wide_decision else sorted(decision_days),
            "outcome": None,
            "light_check_failed": bool(light_issues),
        }
        if not result["new_information"] and issues and count >= 2:
            update.update(
                outcome="incomplete", error="草稿和重复查询持续没有新进展，已保留草稿与具体问题。"
            )
        record(update)
        await save_draft({**state, **update}, "evidence")
        return update

    async def research_information(state: PlanningState) -> dict:
        request = state["information_request"]
        if researcher is None:
            return record({"outcome": "needs_information", "issues": information_issues(request)})
        if settings.max_requests is not None and usage.requests >= settings.max_requests:
            return record(
                {
                    "outcome": "incomplete",
                    "issues": information_issues(request),
                    "error": "共享模型请求预算已用完，未启动额外研究；不代表需求无解。",
                }
            )
        critical = {(n["place_id"], n["day"]): n for n in data.get("critical_needs", [])}
        requested_keys = []
        eligible = []
        for need in request.needs:
            item = need.model_dump(mode="json")
            key = (need.place_id, need.day)
            requested_keys.extend((*key, field) for field in need.fields)
            fields = [field for field in need.fields if not limit_for((*key, field))]
            if fields:
                updates = {"fields": fields}
                if fields != need.fields:
                    updates["question"] = "确认" + "、".join(FIELD_NAMES[field] for field in fields)
                eligible.append(need.model_copy(update=updates))
            item["fields"] = list(
                dict.fromkeys(critical.get(key, {}).get("fields", []) + need.fields)
            )
            critical[key] = item
        data["critical_needs"] = list(critical.values())
        rounds = state["research_rounds"]
        report = {"outcome": "complete"}
        if eligible:
            data["research_history"] = state["research_facts"]
            data["research_feedback"] = state.get("research_diagnostics", [])
            rounds += 1
            record({"research_rounds": rounds})
            report = await researcher(eligible, research_attempts)
        facts = state["research_facts"] + report.get("facts", [])
        # Keep history for audit/context; business checks use the latest version
        # of each field so superseded opening windows cannot contradict repairs.
        latest = {(f["place_id"], f["day"], f["field"]): f for f in facts}
        data["planning_facts"] = list(latest.values())
        outcome = report.get("outcome", "needs_information")
        limit_feedback = []
        for key in dict.fromkeys(requested_keys):
            code = limit_for(key)
            if not code:
                continue
            place_id, day, field = key
            place_name = known_places()[place_id]["name"]
            label = FIELD_NAMES[field]
            attempts = research_attempts.get(key, 0)
            fact_available = key in latest and usable_research_fact(latest[key])
            detail = {
                "search_limit_reached": f"已查过{attempts}次，本轮不会继续查。",
                "live_availability_unverified": "当前没有实时库存接口，请到官方渠道确认；已跳过网页检索。",
                "user_fact_supplied": "用户已补充资料，请直接使用，不再执行搜索。",
            }[code]
            limit_feedback.append(
                {
                    "place_id": place_id,
                    "day": day,
                    "field": field,
                    "date": (req.date + timedelta(days=day - 1)).isoformat(),
                    "code": code,
                    "search_attempts": attempts,
                    "fact_available": fact_available,
                    "message": f"第{day}天 {place_name}的{label}：{detail}",
                }
            )
        newly_limited = [
            item
            for item in limit_feedback
            if (item["place_id"], item["day"], item["field"]) not in informed_limits
        ]
        for item in newly_limited:
            informed_limits[(item["place_id"], item["day"], item["field"])] = item
            trace.append({"event": "research_field_limited", **item})
        issues = report.get("issues", [])
        can_return = outcome in {"complete", "needs_information"}
        if not eligible and not newly_limited:
            outcome, can_return = "needs_information", False
            report["error"] = "规划仍在请求已拦截的资料，已停止本轮补查并保留草稿与历次证据。"
            issues = [
                {
                    **item,
                    "category": "missing_fact",
                    "code": "research_no_progress",
                    "missing_fields": [item["field"]],
                }
                for item in limit_feedback
            ]
            trace.append({"event": "research_no_progress", "detail": report["error"]})
        elif newly_limited and not eligible:
            issues = [
                {**item, "category": "missing_fact", "missing_fields": [item["field"]]}
                for item in newly_limited
            ]
        if (
            can_return
            and settings.max_requests is not None
            and usage.requests >= settings.max_requests
        ):
            outcome = "incomplete"
            can_return = False
            report["error"] = "资料处理后模型轮次已耗尽，已保留草稿、事实与缺口。"
        return record(
            {
                "research_rounds": rounds,
                "research_search_calls": state["research_search_calls"]
                + report.get("search_calls", 0),
                "research_facts": facts,
                "research_diagnostics": state.get("research_diagnostics", [])
                + report.get("diagnostics", []),
                "research_limits": list(informed_limits.values()),
                "facts_updated": can_return,
                "evidence_revision_days": None,
                "information_request": None,
                "outcome": None if can_return else outcome,
                "issues": state["issues"] + issues,
                "error": report.get("error"),
            }
        )

    async def check_candidate(state: PlanningState) -> dict:
        signature = state["candidate"].model_dump_json()
        if (
            settings.max_candidates is not None
            and signature not in checked
            and len(checked) >= settings.max_candidates
        ):
            return record(
                {
                    "outcome": "incomplete",
                    "error": f"最多检查{settings.max_candidates}份候选，当前草稿已保存。",
                }
            )
        checked.add(signature)
        identities = check_draft_identity(req, state["candidate"], known_places(), data["hotels"])
        issues = identities or await validate(req, state["candidate"], data, maps)
        critical = unresolved_critical_facts(
            state["candidate"], data.get("critical_needs", []), data.get("planning_facts", [])
        )
        for issue in critical:
            issue["diagnostics"] = [
                d
                for d in state.get("research_diagnostics", [])
                if d["place_id"] == issue["place_id"]
                and d["day"] == issue["day"]
                and d["field"] in issue["missing_fields"]
                and not d.get("resolved")
            ]
        issues.extend(critical)
        trace.append(
            {
                "event": "validation",
                "detail": "；".join(i["message"] for i in issues)
                or "已知地点、预约与时间可行性检查通过；偏好偏离另行展示",
            }
        )
        outcome = None
        if critical:
            outcome = "needs_information"
        elif (
            issues
            and settings.max_candidates is not None
            and len(checked) >= settings.max_candidates
        ):
            outcome = "incomplete"
        return record({"issues": issues, "outcome": outcome, "attempts": len(checked)})

    def review_context(state):
        routes = route_table(req, state["candidate"], data["origin"]["id"], maps.cache)
        current_pairs = {(row["origin_id"], row["destination_id"]) for row in routes}
        return {
            "requirements": {**req.model_dump(mode="json"), "hotel_nights": req.hotel_nights},
            "revision_request": (previous_plan or {}).get("revision_request"),
            "queried_data": packet_context(model_context(data)),
            "known_places": packet_context(list(known_places().values())),
            "route_table": routes,
            "alternative_routes": [
                route
                for (origin_id, destination_id, _), route in maps.cache.items()
                if (origin_id, destination_id) not in current_pairs
            ],
            "meal_query_results": [
                {"place_id": key[1], "options": entry["data"]}
                for key, entry in executor.cache.items()
                if key[0] == "nearby_restaurants"
                and entry["status"] == "ok"
                and any(
                    stop.kind == "meal" and stop.place_id == key[1]
                    for stop in state["candidate"].stops
                )
            ],
            "draft": state["candidate"].model_dump(mode="json"),
            "program_issues": state["issues"],
            "preference_deviations": preference_deviations(req, state["candidate"], data, maps),
            # Avoid repeating full original and revised itineraries inside review.
            # The latest full draft is above; the reviewer's own history retains
            # its original input, while only explicit decisions cross roles.
            "review": {
                "status": state["review"]["status"],
                "initial": state["review"].get("initial"),
                "planner_decisions": state["review"].get("planner_response", {}).get("decisions"),
            },
        }

    def review_budget(stage):
        if settings.max_requests is None or usage.requests < settings.max_requests:
            return None
        return record(
            {
                "outcome": "incomplete",
                "error": "共享模型请求预算已用完，校验协商尚未完成；不代表规划分歧或需求无解。",
                "review": {
                    **progress.get("review", {}),
                    "status": "incomplete",
                    "pending_stage": stage,
                },
            }
        )

    async def review_candidate(state):
        if stopped := review_budget("initial_review"):
            return stopped
        trace.append(
            {"event": "review", "role": "reviewer", "detail": "独立校验Agent检查完整行程与用户体验"}
        )
        report = await review_session.review(review_context(state))
        validate_positions(report, state["candidate"])
        approved = not report.suggestions
        return record(
            {
                "outcome": "ready" if approved else None,
                "review": {
                    "version": 1,
                    "status": "approved" if approved else "awaiting_planner",
                    "initial": report.model_dump(mode="json"),
                    "original_draft": state["candidate"].model_dump(mode="json"),
                },
            }
        )

    async def respond_to_review(state):
        if stopped := review_budget("planner_response"):
            return stopped
        report = ReviewReport.model_validate(state["review"]["initial"])
        response = await review_session.respond(
            {
                **review_context(state),
                "revision_days": sorted({p.day for s in report.suggestions for p in s.positions}),
                "hotel_revision_days": sorted(hotel_revision_days(req)),
            }
        )
        validate_response(response, report)
        revised = None
        if response.patch is not None:
            adopted = {d.suggestion_id for d in response.decisions if d.action == "revise"}
            days = {
                p.day for s in report.suggestions if s.suggestion_id in adopted for p in s.positions
            }
            try:
                revised = apply_itinerary_patch(req, state["candidate"], response.patch, days)
            except DraftPatchError as exc:
                raise ReviewContractError(str(exc)) from None
        trace.append(
            {
                "event": "review_response",
                "role": "planner",
                "detail": "规划Agent逐项解释采纳或不修改的理由，重新运行程序检查",
            }
        )
        updated = {
            **state["review"],
            "status": "awaiting_adjudication",
            "planner_response": {
                **response.model_dump(mode="json", exclude={"patch"}),
                "revised_itinerary": revised.model_dump(mode="json")
                if revised is not None
                else None,
            },
        }
        if (
            revised is not None
            and settings.max_candidates is not None
            and state["attempts"] >= settings.max_candidates
        ):
            return record(
                {
                    "review": {
                        **updated,
                        "status": "incomplete",
                        "pending_stage": "candidate_limit",
                    },
                    "outcome": "incomplete",
                    "error": "已达到最多3份候选草稿，未继续采用额外修订；不代表双方分歧或需求无解。",
                }
            )
        update = {
            "review": updated,
            "candidate": revised or state["candidate"],
            "query_requests": response.query_requests,
            "information_request": None,
            "planning_phase": "review_revision",
            # Discard stale draft messages before any program-driven repair.
            "messages": [],
            "outcome": None,
        }
        record(update)
        await save_draft({**state, **update}, "review_revision")
        return update

    async def adjudicate_review(state):
        if stopped := review_budget("final_review"):
            return stopped
        report = await review_session.review(review_context(state))
        validate_positions(
            report, state["candidate"], Itinerary.model_validate(state["review"]["original_draft"])
        )
        approved = not report.suggestions
        trace.append(
            {
                "event": "review_adjudication",
                "role": "reviewer",
                "detail": "校验Agent认同最新方案及解释"
                if approved
                else "双方仍有规划分歧，保留草稿与双方理由，等待用户协调",
            }
        )
        return record(
            {
                "outcome": "ready" if approved else "needs_user_decision",
                "review": {
                    **state["review"],
                    "status": "approved" if approved else "disputed",
                    "final": report.model_dump(mode="json"),
                },
                "issues": []
                if approved
                else [
                    {
                        "category": "review_disagreement",
                        "code": "review_disagreement",
                        "message": s.concern,
                        "day": s.positions[0].day,
                        "stop_number": s.positions[0].stop_number,
                        "place_id": s.positions[0].place_id,
                        "suggestion_id": s.suggestion_id,
                    }
                    for s in report.suggestions
                ],
            }
        )

    def after_preflight(state):
        return END if state.get("outcome") else "prepare_evidence"

    def after_prepare(state):
        return END if state.get("outcome") else "propose"

    def after_proposal(state):
        if state.get("outcome"):
            return END
        return "collect_queries"

    def after_queries(state):
        if state.get("outcome"):
            return END
        if state.get("light_check_failed"):
            return "propose"
        if state.get("information_request"):
            return "research_information"
        return "propose" if state["issues"] or state.get("facts_updated") else "check_candidate"

    def after_research(state):
        return END if state.get("outcome") else "propose"

    def next_step(state):
        if state.get("outcome"):
            return END
        if state["issues"]:
            return "propose"
        return (
            "adjudicate_review"
            if state["review"]["status"] == "awaiting_adjudication"
            else "review_candidate"
        )

    graph = StateGraph(PlanningState)
    graph.add_node("preflight", preflight)
    graph.add_node("prepare_evidence", prepare_evidence)
    graph.add_node("propose", propose)
    graph.add_node("collect_queries", collect_queries)
    graph.add_node("research_information", research_information)
    graph.add_node("check_candidate", check_candidate)
    graph.add_node("review_candidate", review_candidate)
    graph.add_node("respond_to_review", respond_to_review)
    graph.add_node("adjudicate_review", adjudicate_review)
    graph.add_edge(START, "preflight")
    graph.add_conditional_edges("preflight", after_preflight)
    graph.add_conditional_edges("prepare_evidence", after_prepare)
    graph.add_conditional_edges("propose", after_proposal)
    graph.add_conditional_edges("collect_queries", after_queries)
    graph.add_conditional_edges("research_information", after_research)
    graph.add_conditional_edges("check_candidate", next_step)
    graph.add_conditional_edges(
        "review_candidate", lambda state: END if state.get("outcome") else "respond_to_review"
    )
    graph.add_conditional_edges(
        "respond_to_review", lambda state: END if state.get("outcome") else "collect_queries"
    )
    graph.add_edge("adjudicate_review", END)
    return await graph.compile().ainvoke({}, config={"recursion_limit": 1000})


async def run_plan(
    req: Requirements,
    mode: str,
    settings: Settings,
    previous_plan=None,
    transport=None,
    research_context: dict | None = None,
    on_draft=None,
    continuation: PlanningContinuation | None = None,
) -> dict:
    if not map_configured(settings):
        raise MapError("请先配置AMAP_API_KEY（高德Web服务Key），才能使用真实地点与路线规划")
    if mode == "deepseek" and not settings.configured:
        raise MapError("请先在本地.env配置DEEPSEEK_API_KEY")
    trace, issues, result, error = [], [], None, None
    outcome, progress = "incomplete", {}
    model_usage = RunUsage()
    recorder = ModelUsageRecorder()
    data = {"hotel": None, "hotels": []}
    research_context = load_evidence(
        research_context,
        req.model_dump(mode="json"),
        research_context.get("requirements_revision") if isinstance(research_context, dict) else 0,
    )
    async with httpx2.AsyncClient(
        timeout=15, transport=transport, follow_redirects=False
    ) as client:
        maps = MapClient(
            client,
            settings.amap_api_key.get_secret_value(),
            interval=0.4 if transport is None else 0,
        )
        try:
            async with asyncio.timeout(settings.run_timeout):
                if mode == "basic":
                    data = await prepare(req, maps, trace)
                    result = await schedule(req, data, maps)
                    trace.append(
                        {
                            "event": "schedule",
                            "detail": "按真实路段耗时、地理距离和预约时间进行约束排程",
                        }
                    )
                    issues = await validate(req, result, data, maps)
                    outcome = "incomplete" if issues else "ready"
                    trace.append(
                        {
                            "event": "validation",
                            "detail": "；".join(i["message"] for i in issues)
                            or "已知时间、预约、地点与预算预留检查通过",
                        }
                    )
                else:
                    async with AsyncOpenAI(
                        api_key=settings.api_key.get_secret_value(),
                        base_url=settings.base_url,
                        # The SDK owns and closes its client. Keep the map client
                        # alive for final validation after the SDK context exits.
                        http_client=httpx2.AsyncClient(
                            timeout=settings.run_timeout,
                            transport=transport,
                            follow_redirects=False,
                            event_hooks={
                                "request": [recorder.request],
                                "response": [recorder.response],
                            },
                        ),
                        max_retries=0,
                    ) as sdk:
                        model = OpenAIChatModel(
                            settings.model,
                            provider=OpenAIProvider(openai_client=sdk),
                            profile=OpenAIModelProfile(
                                # DeepSeek documents max_tokens; the OpenAI
                                # default otherwise emits max_completion_tokens.
                                openai_chat_supports_max_completion_tokens=(
                                    settings.base_url.rstrip("/")
                                    not in (
                                        "https://api.deepseek.com",
                                        "https://api.deepseek.com/v1",
                                    )
                                ),
                                openai_supports_strict_tool_definition=False,
                                supports_forced_tool_choice=False,
                                openai_chat_thinking_field="reasoning_content",
                                openai_chat_send_back_thinking_parts="field",
                                openai_chat_thinking_requires_field_on_tool_calls=True,
                                openai_chat_supports_max_reasoning_effort=True,
                            ),
                        )
                        agent = Agent(
                            model,
                            output_type=ToolOutput(
                                DraftProposal, name="draft_proposal", strict=False
                            ),
                            retries=1,
                            instructions=None,
                        )

                        async def research(needs, search_attempts):
                            with recorder.role("research"), recorder.stage("research"):
                                return await research_missing_information(
                                    model,
                                    needs,
                                    req,
                                    maps.places,
                                    settings,
                                    model_usage,
                                    trace,
                                    transport=transport,
                                    prefetch=True,
                                    prior_facts=data.get("research_history", []),
                                    prior_diagnostics=data.get("research_feedback", []),
                                    search_attempts=search_attempts,
                                    on_progress=lambda facts, diagnostics: progress.update(
                                        {
                                            "research_facts": data.get("research_history", [])
                                            + facts,
                                            "research_diagnostics": data.get(
                                                "research_feedback", []
                                            )
                                            + diagnostics,
                                        }
                                    ),
                                )

                        state = await run_planning_graph(
                            agent,
                            req,
                            maps,
                            data,
                            trace,
                            previous_plan,
                            settings,
                            model_usage,
                            research_context,
                            researcher=research,
                            review_session=ReviewSession(
                                model, agent, settings, model_usage, recorder
                            ),
                            progress=progress,
                            on_draft=on_draft,
                            recorder=recorder,
                            continuation=continuation,
                        )
                        issues = state.get("issues", [])
                        error = state.get("error")
                        outcome = state.get("outcome") or "incomplete"
                        if outcome == "ready" and not issues and not error:
                            result = state.get("candidate")
        except (
            MapError,
            TimeoutError,
            ModelHTTPError,
            ModelConnectionError,
            UnexpectedModelBehavior,
            UsageLimitExceeded,
        ) as exc:
            outcome = (
                "incomplete"
                if isinstance(exc, (TimeoutError, UsageLimitExceeded, UnexpectedModelBehavior))
                else "service_error"
            )
            issues = progress.get("issues", issues)
            if progress.get("information_request"):
                issues = issues + information_issues(progress["information_request"])
            error = (
                str(exc)
                if isinstance(exc, (MapError, ReviewContractError))
                else f"本次规划未完成（{type(exc).__name__}），请检查接口配置或减少活动后重试；没有改用固定数据。"
            )
        measured = result or progress.get("candidate")
        metrics = []
        itinerary_routes, route_rows = [], []
        if measured and data.get("origin"):
            route_rows = route_table(req, measured, data["origin"]["id"], maps.cache)
            travel = dict.fromkeys(range(1, req.days + 1), 0)
            for row in route_rows:
                day = row["day"]
                duration = row["duration_minutes"]
                travel[day] = (
                    None if duration is None or travel[day] is None else travel[day] + duration
                )
                if result:
                    leg = cached_route(
                        maps.cache, row["origin_id"], row["destination_id"], req.transport
                    )
                    if leg:
                        itinerary_routes.append(
                            {**leg, "day": day, "stop_index": row["stop_number"] - 1}
                        )
            metrics = daily_metrics(req, measured, data.get("experience_groups", []), travel)
        hotel = next((h for h in data["hotels"] if result and h["id"] == result.hotel_id), None)
        observation = recorder.snapshot(settings)
        search_audits = [
            event.get("billable") for event in trace if event["event"] == "research_search"
        ]
        search_attempts = sum(event["event"] == "research_search_attempt" for event in trace)
        api_cost = estimate_cost(
            observation["model_calls"],
            search_audits + [None] * max(0, search_attempts - len(search_audits)),
            official=settings.base_url.rstrip("/")
            in ("https://api.deepseek.com", "https://api.deepseek.com/v1"),
        )
        return {
            "status": "ready" if result and not issues and not error else "conflict",
            "outcome": outcome,
            "itinerary": result.model_dump(mode="json") if result else None,
            "draft": progress["candidate"].model_dump(mode="json")
            if result is None and progress.get("candidate")
            else None,
            "research_facts": progress.get("research_facts", []),
            "planning_protocol": "draft_first",
            "query_log": progress.get("query_log", []),
            **(
                {
                    "user_supplied_facts": continuation.new_facts,
                    "carried_query_results": list(continuation.query_results.values()),
                    "carried_research_attempts": [
                        {
                            "place_id": key[0],
                            "day": key[1],
                            "field": key[2],
                            "search_attempts": attempts,
                        }
                        for key, attempts in continuation.search_attempts.items()
                    ],
                }
                if continuation
                else {}
            ),
            "assumptions": progress.get("assumptions", []),
            "research_diagnostics": progress.get("research_diagnostics", []),
            "research_requests": progress.get("research_requests", []),
            "research_limits": progress.get("research_limits", []),
            "review": progress.get("review") if mode == "deepseek" else None,
            "pending_information": progress["information_request"].model_dump(mode="json")
            if progress.get("information_request")
            else None,
            "research_rounds": progress.get("research_rounds", 0),
            "research_search_calls": max(
                progress.get("research_search_calls", 0),
                sum(event["event"] == "research_search_attempt" for event in trace),
            ),
            "hotel": hotel,
            "hotel_options": data["hotels"],
            "places": list(maps.places.values()),
            "must_visit_ids": [place["id"] for place in data.get("must_visit", [])],
            "routes": itinerary_routes,
            "route_table": route_rows,
            "daily_metrics": metrics,
            "timing_coverage": {
                "stop_count": len(measured.stops),
                "explicit_count": sum(s.transfer is not None for s in measured.stops),
            }
            if measured
            else None,
            "buffer_advisories": buffer_advisories(req, measured, data, maps, routes=route_rows)
            if measured and data.get("origin")
            else [],
            "preference_policy": "soft_targets_v1",
            "preference_deviations": preference_deviations(req, measured, data, maps, metrics)
            if measured and data.get("origin")
            else [],
            "costs": costs(req, result) if result else None,
            "issues": issues,
            "trace": trace,
            "usage": {
                "requests": model_usage.requests,
                "input_tokens": model_usage.input_tokens,
                "output_tokens": model_usage.output_tokens,
            }
            if model_usage.requests
            else None,
            "usage_observation": observation,
            "api_cost_estimate": api_cost,
            "error": error,
            "model": settings.model if mode == "deepseek" else "基于真实地图的约束排程",
            "thinking_configuration": {
                "planner": {"enabled": True, "effort": settings.planner_effort},
                "research": {"enabled": True, "effort": settings.research_effort},
                "reviewer": {"enabled": True, "effort": settings.reviewer_effort},
            }
            if mode == "deepseek"
            else None,
            "data_mode": (
                "本地硬要求预检 · 未调用外部服务"
                if not maps.calls and not model_usage.requests
                else "高德地点与路线预检 · 未调用模型"
                if mode == "deepseek" and not model_usage.requests
                else "真实高德＋" + ("DeepSeek" if mode == "deepseek" else "基础排程")
            ),
            "warnings": [
                "酒店推荐仅基于位置，房价、空房和房型待订房平台确认。",
                "预算只核对住宿与餐饮预留，交通与门票未知，不宣称总费用已满足。",
                "地点名称与馆区请核对；开放、预约、排队与实际交通仍需确认。",
            ],
            "map_calls": maps.calls,
            "created_at": datetime.now(UTC).isoformat(),
        }
