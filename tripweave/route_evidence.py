"""Project the cached routes used by a draft, without making map requests."""

from .domain import Itinerary, Requirements, RestPeriod, Stop, minutes
from .maps import cached_route


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
                "source": leg.get("source") if leg else None,
                "distance_meters": leg.get("distance_meters") if leg else None,
                "buffer_minutes": buffer,
                "buffer_basis": basis,
            }
        )
    return rows


def hotel_comparisons(req: Requirements, draft: Itinerary, hotels: list[dict], cache: dict):
    """Compare the same evening and morning anchors for all affected days."""
    if not req.hotel_nights or req.hotel_id or not draft.hotel_id:
        return {}

    def duration(start, end):
        if start == end:
            return 0
        route = cached_route(cache, start, end, req.transport)
        return route["duration_minutes"] if route is not None else None

    anchors = []
    for night in range(1, req.hotel_nights + 1):
        evening = [s for s in draft.stops if s.day == night]
        morning = next((s for s in draft.stops if s.day == night + 1), None)
        if len(evening) < 2 or morning is None:
            # These interfaces need the same real stops on both sides to compare.
            return {}
        anchors += [
            (night, evening[-2].place_id, True),
            (night + 1, morning.place_id, False),
        ]

    original = [
        duration(anchor, draft.hotel_id) if returning else duration(draft.hotel_id, anchor)
        for _, anchor, returning in anchors
    ]
    if any(value is None for value in original):
        return {}

    comparisons = {}
    for hotel in hotels:
        hotel_id = hotel["id"]
        if hotel_id == draft.hotel_id:
            continue
        legs = []
        for (day, anchor, returning), current in zip(anchors, original, strict=True):
            start, end = (anchor, hotel_id) if returning else (hotel_id, anchor)
            travel = duration(start, end)
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
