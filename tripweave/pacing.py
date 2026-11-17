"""One pacing definition for prompts, validation and persisted result statistics.

The model chooses the experience. This module measures it; it never moves or deletes stops.
"""

from .domain import Itinerary, Requirements, minutes

REQUIREMENT_POLICY = (
    "需求分为三类。硬要求：旅行范围、明确必去/已选景点、手选酒店、固定预约、visit_constraints明确窗口，"
    "以及用户在notes、custom_*或本次revision_request中明确不可突破的条件；冲突时不能偷偷放宽或删掉。"
    "软目标：普通兴趣、节奏、四项数值、出发/结束期望和期望金额；strict_preferences列出的数值/时间项才是程序锁定上限。"
    "不能仅因字段名含max就把它当硬上限，也不要因文字没有「必须」二字就忽略明确限制（如「不坐出租车」）。"
    "明确硬要求优先于普通目标，本次明确修改优先于旧偏好；两项硬要求矛盾时具体指出，不能自行选一项违背。"
    "在不违反硬要求和实际衔接的前提下综合权衡体验，不为轻微偏离自动删景点、压缩必要停留或反复重排。"
    "偏离软目标时在summary简述期望/实际、原因与收益；不能声称全部满足，也不能只说「体验更好」。"
    "例如交通期望90分钟而实际107分钟可有理由保留；地图需56分钟却只留40分钟仍必须修正。"
    "资料未知是待核实，不是可放宽的偏好；未知价格不能宣称满足硬性金额、已经预订或确认开放。"
)


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
                "preference_policy": "soft_targets_v1",
                "strict_preferences": list(req.strict_preferences),
            }
        )
    return result


def pacing_issues(metrics: list[dict]) -> list[dict]:
    """Measure deviations; only explicitly locked targets block a new plan."""
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
            strict = limit_field in row.get("strict_preferences", [])
            issues.append(
                {
                    "category": "draft_error" if strict else "preference_deviation",
                    "severity": "error" if strict else "warning",
                    "code": limit_field,
                    "day": row["day"],
                    "message": f"第{row['day']}天{label}{actual:.2f}{unit}，超出用户"
                    + ("不可突破上限" if strict else "期望目标")
                    + f"{limit:g}{unit}"
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
