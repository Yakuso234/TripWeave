"""One pacing definition for prompts, validation and persisted result statistics.

The model chooses the experience. This module measures it; it never moves or deletes stops.
"""

from .domain import Itinerary, Requirements, minutes


def daily_metrics(
    req: Requirements, itinerary: Itinerary, groups: list[dict], travel: dict[int, int | None]
) -> list[dict]:
    group_by_id = {member: group["group_id"] for group in groups for member in group["members"]}
    window = minutes(req.end_time) - minutes(req.start_time)
    result = []
    for day in range(1, req.days + 1):
        visits = [
            (number, stop)
            for number, stop in enumerate((s for s in itinerary.stops if s.day == day), 1)
            if stop.kind in ("activity", "booking")
        ]
        # Union avoids inflating statistics for overlapping periods. Overlap still fails
        # the chronological/connection check, so these statistics cannot make it acceptable.
        intervals = sorted(
            (minutes(s.start_time), minutes(s.end_time))
            for _, s in visits
            if s.end_time > s.start_time
        )
        duration, last_end = 0, -1
        for start, end in intervals:
            duration += max(0, end - max(start, last_end))
            last_end = max(last_end, end)
        result.append(
            {
                "day": day,
                "attraction_count": len(
                    {group_by_id.get(s.place_id, s.place_id) for _, s in visits}
                ),
                "sightseeing_minutes": duration,
                "available_minutes": window,
                "sightseeing_ratio": duration * 100 / window,
                "transport_minutes": travel.get(day),
                "sightseeing_stops": [
                    {
                        "day": day,
                        "stop_number": number,
                        "place_id": s.place_id,
                        "start_time": s.start_time,
                        "end_time": s.end_time,
                    }
                    for number, s in visits
                ],
                "limits": {
                    name: getattr(req, name)
                    for name in (
                        "max_attractions",
                        "max_sightseeing_minutes",
                        "max_sightseeing_ratio",
                        "max_transport_minutes",
                    )
                },
            }
        )
    return result


def pacing_issues(metrics: list[dict]) -> list[dict]:
    issues = []
    definitions = (
        ("attraction_count", "max_attractions", "景点数量", "个"),
        ("sightseeing_minutes", "max_sightseeing_minutes", "游玩时长", "分钟"),
        ("sightseeing_ratio", "max_sightseeing_ratio", "游玩时间占比", "%"),
        ("transport_minutes", "max_transport_minutes", "交通时长", "分钟"),
    )
    for row in metrics:
        for field, limit_field, label, unit in definitions:
            actual, limit = row[field], row["limits"][limit_field]
            if actual is None or limit is None or actual <= limit + 1e-9:
                continue
            issues.append(
                {
                    "category": "draft_error",
                    "code": limit_field,
                    "day": row["day"],
                    "message": f"第{row['day']}天{label}{actual:.2f}{unit}，超出用户上限{limit:g}{unit}"
                    + (
                        f"（固定可安排窗口{row['available_minutes']}分钟）"
                        if field == "sightseeing_ratio"
                        else ""
                    ),
                    "actual": actual,
                    "limit": limit,
                    "metric": field,
                    "available_minutes": row["available_minutes"],
                    "occurrences": row["sightseeing_stops"] if field != "transport_minutes" else [],
                }
            )
    return issues
