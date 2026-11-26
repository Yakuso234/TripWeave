"""Project the cached routes used by a draft, without making map requests."""

from math import isfinite

from .domain import Itinerary, Requirements, RestPeriod, Stop, minutes
from .maps import cached_route, distance


def planned_legs(req: Requirements, draft: Itinerary, origin_id: str):
    for day in range(1, req.days + 1):
        previous_id = draft.hotel_id if 2 <= day <= req.hotel_nights + 1 else origin_id
        previous = None
        for number, stop in enumerate((stop for stop in draft.stops if stop.day == day), 1):
            yield day, number, previous_id, previous, stop
            previous_id, previous = stop.place_id, stop


def rest_minutes(rests: list[RestPeriod], lower: int, upper: int) -> int | None:
    total = 0
    for rest in sorted(rests, key=lambda item: item.start_time):
        start, end = minutes(rest.start_time), minutes(rest.end_time)
        if start < lower or end > upper:
            return None
        total += end - start
        lower = end
    return total


def remaining_buffer(
    req: Requirements, previous: Stop | None, stop: Stop, travel_minutes: int
) -> tuple[int | None, str | None]:
    start = minutes(stop.start_time)
    previous_end = minutes(previous.end_time) if previous else None
    timing = stop.transfer
    if timing is None:
        # Without actual departure and rests, the gap only gives an upper bound.
        if previous_end is None:
            if "start_time" not in req.strict_preferences:
                return None, None
            previous_end = minutes(req.start_time)
        return start - previous_end - travel_minutes, "upper_bound"

    departure, arrival = minutes(timing.departure_time), minutes(timing.arrival_time)
    earliest_departure = previous_end
    if earliest_departure is None and "start_time" in req.strict_preferences:
        earliest_departure = minutes(req.start_time)
    if (earliest_departure is not None and departure < earliest_departure) or not (
        departure + travel_minutes <= arrival <= start
    ):
        return None, None
    after = rest_minutes(timing.rest_after, arrival, start)
    if after is None or rest_minutes(timing.rest_before, previous_end or 0, departure) is None:
        return None, None
    return start - departure - travel_minutes - after, "scheduled"


def route_table(req: Requirements, draft: Itinerary, origin_id: str, cache: dict) -> list[dict]:
    rows = []
    for day, number, previous_id, previous, stop in planned_legs(req, draft, origin_id):
        leg = cached_route(cache, previous_id, stop.place_id, req.transport)
        same_place = previous_id == stop.place_id
        duration = 0 if same_place else leg["duration_minutes"] if leg is not None else None
        buffer, basis = (None, None)
        if not same_place and duration is not None:
            buffer, basis = remaining_buffer(req, previous, stop, duration)
        rows.append(
            {
                "day": day,
                "stop_number": number,
                "origin_id": previous_id,
                "destination_id": stop.place_id,
                "status": "same_place" if same_place else "map_estimate" if leg else "unknown",
                "duration_minutes": duration,
                "mode": leg.get("mode") if leg else None,
                "checked_modes": leg.get("checked_modes") if leg else None,
                "source": leg.get("source") if leg else None,
                "distance_meters": leg.get("distance_meters") if leg else None,
                "buffer_minutes": buffer,
                "buffer_basis": basis,
            }
        )
    return rows


def _hotel_anchors(req: Requirements, draft: Itinerary) -> tuple[tuple[int, str, bool], ...]:
    if not req.hotel_nights or req.hotel_id or not draft.hotel_id:
        return ()

    anchors = []
    for night in range(1, req.hotel_nights + 1):
        evening = [s for s in draft.stops if s.day == night]
        morning = next((s for s in draft.stops if s.day == night + 1), None)
        if (
            len(evening) < 2
            or evening[-1].kind != "hotel"
            or evening[-1].place_id != draft.hotel_id
            or evening[-2].place_id == draft.hotel_id
            or morning is None
            or morning.kind == "hotel"
            or morning.place_id == draft.hotel_id
        ):
            # A meal or activity at the selected hotel can move with the hotel.
            # It cannot serve as an independent anchor for the comparison.
            return ()
        anchors += [
            (night, evening[-2].place_id, True),
            (night + 1, morning.place_id, False),
        ]
    return tuple(anchors)


def _hotel_route(hotel_id: str, anchor_id: str, returning: bool) -> tuple[str, str]:
    return (anchor_id, hotel_id) if returning else (hotel_id, anchor_id)


def _duration(cache, start, end, transport) -> int | None:
    if start == end:
        return 0
    route = cached_route(cache, start, end, transport)
    return route["duration_minutes"] if route is not None else None


def _hotel_minutes(anchors, hotel_id, cache, transport) -> list[int] | None:
    durations = []
    for _, anchor_id, returning in anchors:
        start, end = _hotel_route(hotel_id, anchor_id, returning)
        duration = _duration(cache, start, end, transport)
        if duration is None:
            return None
        durations.append(duration)
    return durations


def hotel_shortlist(
    req: Requirements,
    draft: Itinerary,
    hotels: list[dict],
    places: dict,
    cache: dict,
    *,
    max_queries: int = 24,
    min_leg_minutes: int = 0,
):
    """Rank up to three alternatives and return only their missing paired routes."""
    anchors = _hotel_anchors(req, draft)
    if not anchors:
        return None
    original = _hotel_minutes(anchors, draft.hotel_id, cache, req.transport)
    if original is None or max(original) < min_leg_minutes:
        return None

    standings = []
    for hotel_id in {hotel["id"] for hotel in hotels} - {draft.hotel_id}:
        try:
            score = sum(distance(places[hotel_id], places[anchor]) for _, anchor, _ in anchors)
        except KeyError, TypeError, ValueError:
            continue
        if isfinite(score):
            standings.append((score, hotel_id))

    shortlisted, missing = [], {}
    for _, hotel_id in sorted(standings)[:3]:
        pairs = dict.fromkeys(
            _hotel_route(hotel_id, anchor, returning) for _, anchor, returning in anchors
        )
        needed = [
            (start, end)
            for start, end in pairs
            if (start, end) not in missing and _duration(cache, start, end, req.transport) is None
        ]
        if len(missing) + len(needed) > max_queries:
            continue
        shortlisted.append(hotel_id)
        for start, end in needed:
            missing[start, end] = {
                "kind": "route",
                "origin_id": start,
                "destination_id": end,
                "transport": req.transport,
                "reason": "比较同一过夜前接驳和次日出发地点的自动酒店备选。",
            }
    if not shortlisted:
        return None
    return (req.transport, anchors), tuple(shortlisted), list(missing.values())


def hotel_comparisons(req: Requirements, draft: Itinerary, hotels: list[dict], cache: dict):
    """Compare the same evening and morning anchors for all affected days."""
    anchors = _hotel_anchors(req, draft)
    if not anchors:
        return {}
    original = _hotel_minutes(anchors, draft.hotel_id, cache, req.transport)
    if original is None:
        return {}

    comparisons = {}
    for hotel in hotels:
        hotel_id = hotel["id"]
        if hotel_id == draft.hotel_id:
            continue
        legs = []
        for (day, anchor, returning), current in zip(anchors, original, strict=True):
            start, end = _hotel_route(hotel_id, anchor, returning)
            travel = _duration(cache, start, end, req.transport)
            if travel is None:
                break
            legs.append(
                {
                    "day": day,
                    "origin_id": start,
                    "destination_id": end,
                    "current_minutes": current,
                    "alternative_minutes": travel,
                    "delta_minutes": travel - current,
                }
            )
        if len(legs) == len(anchors):
            comparisons[hotel_id] = legs
    return comparisons
