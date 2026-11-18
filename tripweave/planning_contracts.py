"""Structured user constraints and evidence contracts, independent of orchestration.

Only explicit user requirements can prove a hard conflict here. Web-reported facts
can reject a draft, but they never prove that the user's trip is impossible.
"""

from collections import defaultdict
from collections.abc import Mapping
from typing import Annotated, Literal
from unicodedata import normalize

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Clock = Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]
PlaceId = Annotated[str, Field(min_length=1, max_length=64)]
ResearchField = Literal[
    "opening_hours",
    "last_admission",
    "reservation_channel",
    "reservation_requirement",
    "ticket_availability",
]


def _minutes(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TimeWindow(Contract):
    day: int = Field(ge=1, le=7)
    start_time: Clock
    end_time: Clock

    @model_validator(mode="after")
    def ordered(self):
        if self.end_time <= self.start_time:
            raise ValueError("时段结束必须晚于开始；跨午夜须拆成不同日期")
        return self


class VisitConstraint(Contract):
    constraint_id: str = Field(min_length=1, max_length=64)
    place_id: PlaceId | None = None
    place_query: str = Field(default="", max_length=200)
    windows: list[TimeWindow] = Field(min_length=1, max_length=14)
    min_duration_minutes: int = Field(default=60, ge=1, le=720)
    fixed: bool = False

    @model_validator(mode="after")
    def identity_and_fixed_window(self):
        if not self.place_id and not self.place_query:
            raise ValueError("地点ID和明确地点名称至少填写一项")
        if self.fixed and len(self.windows) != 1:
            raise ValueError("固定预约必须有且只有一个精确时段")
        return self


class PlanningNeed(Contract):
    place_id: PlaceId
    day: int = Field(ge=1, le=7)
    fields: list[ResearchField] = Field(min_length=1, max_length=3)
    question: str = Field(min_length=1, max_length=300)

    @field_validator("fields", mode="before")
    @classmethod
    def unique_fields(cls, values):
        return (
            list(
                dict.fromkeys(
                    "reservation_requirement" if value == "reservation_rules" else value
                    for value in values
                )
            )
            if isinstance(values, list) and all(isinstance(value, str) for value in values)
            else values
        )


class PlanningInformationRequest(Contract):
    needs: list[PlanningNeed] = Field(min_length=1, max_length=5)
    reason: str = Field(min_length=1, max_length=500)


class ResearchFact(Contract):
    place_id: PlaceId
    day: int = Field(ge=1, le=7)
    field: ResearchField
    status: Literal["found", "unknown"]
    value: str = Field(default="", max_length=600)
    source_ids: list[Annotated[int, Field(ge=1, strict=True)]] = Field(
        default_factory=list, max_length=3
    )
    quote: str = Field(default="", max_length=650)
    windows: list[TimeWindow] = Field(default_factory=list, max_length=14)
    closed: bool | None = None
    last_admission: Clock | None = None
    unknown_reason: (
        Literal[
            "field_not_stated",
            "content_unrelated",
            "excerpt_insufficient",
            "sources_conflict",
            "date_not_applicable",
            "other",
        ]
        | None
    ) = None
    unknown_detail: str = Field(default="", max_length=400)

    @field_validator("source_ids", mode="before")
    @classmethod
    def unique_sources(cls, values):
        return (
            list(dict.fromkeys(values))
            if isinstance(values, list) and all(isinstance(value, int) for value in values)
            else values
        )

    @model_validator(mode="after")
    def evidence_and_field_scope(self):
        if self.status == "unknown":
            if (
                self.value
                or self.source_ids
                or self.quote
                or self.windows
                or self.closed is not None
                or self.last_admission is not None
            ):
                raise ValueError("未知事实不能附带推定值、证据引用或已知时间结构")
            return self
        if self.unknown_reason is not None or self.unknown_detail:
            raise ValueError("已找到的事实不能同时标记未知原因")
        if not self.value or not self.quote or not self.source_ids:
            raise ValueError("已找到的事实须提供内容、来源编号和原文摘录")
        if self.field != "opening_hours" and (self.windows or self.closed is not None):
            raise ValueError("开放窗口和闭馆标识只能用于opening_hours")
        if self.field != "last_admission" and self.last_admission is not None:
            raise ValueError("最晚入场时刻只能用于last_admission")
        if any(window.day != self.day for window in self.windows):
            raise ValueError("开放窗口必须对应本事实的日期")
        if self.closed is True and self.windows:
            raise ValueError("当日关闭与非空开放窗口不能同时成立")
        return self


class ResearchReport(Contract):
    facts: list[ResearchFact] = Field(default_factory=list, max_length=15)


def _normalized_query(value: str) -> str:
    return normalize("NFKC", value).strip().casefold()


def _place_id(value) -> str | None:
    return value.get("id") if isinstance(value, Mapping) else value


def _identity_relation(left: dict, right: dict) -> bool | None:
    """Return known same/different identity; differing names can still be aliases."""
    if left["place_id"] and right["place_id"]:
        return left["place_id"] == right["place_id"]
    if (
        left["place_query"]
        and right["place_query"]
        and _normalized_query(left["place_query"]) == _normalized_query(right["place_query"])
    ):
        return True
    return None


def preflight_requirements(req, resolved_by_constraint: Mapping | None = None) -> list[dict]:
    """Prove only explicit boundary/fixed-window contradictions, without I/O.

    Optional resolved identities support a second preflight after POI resolution.
    ``booking`` is the reserved key for the legacy single fixed appointment.
    """
    resolved = resolved_by_constraint or {}
    issues, fixed_visits = [], []
    daily = {"start_time": req.start_time, "end_time": req.end_time}
    strict = getattr(req, "strict_preferences", [])
    start = _minutes(req.start_time) if "start_time" in strict else 0
    end = _minutes(req.end_time) if "end_time" in strict else 1440
    if req.booking_query:
        booking = {
            "constraint_id": "booking",
            "place_id": _place_id(resolved.get("booking")),
            "place_query": req.booking_query,
            "day": req.booking_day,
            "start_time": req.booking_start,
            "end_time": req.booking_end,
        }
        fixed_visits.append(booking)
        if _minutes(req.booking_start) < start or _minutes(req.booking_end) > end:
            issues.append(
                {
                    "category": "hard_conflict",
                    "code": "booking_outside_daily_window",
                    "constraint_ids": ["booking", "daily_window"],
                    "day": req.booking_day,
                    "message": f"第{req.booking_day}天固定预约{req.booking_start}–{req.booking_end}"
                    f"超出每日{req.start_time}–{req.end_time}边界，请确认调整哪项要求。",
                    "expected": daily,
                    "actual": booking,
                }
            )
    for constraint in getattr(req, "visit_constraints", []):
        windows = [window.model_dump() for window in constraint.windows]
        feasible = [
            window
            for window in constraint.windows
            if window.day <= req.days
            and min(end, _minutes(window.end_time)) - max(start, _minutes(window.start_time))
            >= constraint.min_duration_minutes
            and (
                not constraint.fixed
                or (_minutes(window.start_time) >= start and _minutes(window.end_time) <= end)
            )
        ]
        if not feasible:
            days = sorted({window.day for window in constraint.windows})
            issues.append(
                {
                    "category": "hard_conflict",
                    "code": "visit_window_unavailable",
                    "constraint_ids": [constraint.constraint_id, "daily_window"],
                    **({"day": days[0]} if len(days) == 1 else {"related_days": days}),
                    "place_id": constraint.place_id,
                    "place_query": constraint.place_query,
                    "message": f"硬要求「{constraint.place_query or constraint.place_id}」的允许时段"
                    f"与每日边界无法同时满足至少{constraint.min_duration_minutes}分钟的完整停留。",
                    "expected": {**daily, "min_duration_minutes": constraint.min_duration_minutes},
                    "actual": {"windows": windows, "fixed": constraint.fixed},
                }
            )
        if constraint.fixed:
            fixed_visits.append(
                {
                    "constraint_id": constraint.constraint_id,
                    "place_id": _place_id(resolved.get(constraint.constraint_id))
                    or constraint.place_id,
                    "place_query": constraint.place_query,
                    **windows[0],
                }
            )
    for index, left in enumerate(fixed_visits):
        for right in fixed_visits[index + 1 :]:
            if left["day"] != right["day"]:
                continue
            if max(left["start_time"], right["start_time"]) >= min(
                left["end_time"], right["end_time"]
            ):
                continue
            same_place = _identity_relation(left, right)
            if same_place is None:
                continue
            if same_place and all(
                left[field] == right[field] for field in ("start_time", "end_time")
            ):
                continue  # One exact stop can satisfy duplicate explicit requirements.
            issues.append(
                {
                    "category": "hard_conflict",
                    "code": "fixed_window_overlap",
                    "constraint_ids": [left["constraint_id"], right["constraint_id"]],
                    "day": left["day"],
                    "place_ids": list(
                        dict.fromkeys(p["place_id"] for p in (left, right) if p["place_id"])
                    ),
                    "message": f"第{left['day']}天两个精确固定预约的时段重叠，"
                    "不能由同一站同时满足，请选择调整哪项预约。",
                    "expected": "保留每项精确预约时段且站点之间不能重叠",
                    "actual": [left, right],
                }
            )
    return issues


def _numbered_stops(output):
    counts = defaultdict(int)
    for stop in output.stops:
        counts[stop.day] += 1
        yield stop, counts[stop.day]


def _satisfies_constraint(stop, constraint: VisitConstraint) -> bool:
    if stop.kind not in ("activity", "booking"):
        return False
    if _minutes(stop.end_time) - _minutes(stop.start_time) < constraint.min_duration_minutes:
        return False
    return any(
        stop.day == window.day
        and (
            stop.kind == "booking"
            and stop.start_time == window.start_time
            and stop.end_time == window.end_time
            if constraint.fixed
            else stop.start_time >= window.start_time and stop.end_time <= window.end_time
        )
        for window in constraint.windows
    )


def validate_visit_constraints(req, output, resolved_by_constraint: Mapping) -> list[dict]:
    """Check known identity, full duration, allowed windows and fixed appointments."""
    issues = []
    numbered = list(_numbered_stops(output))
    for constraint in getattr(req, "visit_constraints", []):
        place_id = _place_id(resolved_by_constraint.get(constraint.constraint_id))
        place_id = place_id or constraint.place_id
        occurrences = [
            (stop, number) for stop, number in numbered if place_id and stop.place_id == place_id
        ]

        if any(_satisfies_constraint(stop, constraint) for stop, _ in occurrences):
            continue
        allowed_days = {window.day for window in constraint.windows}
        occurrence_days = {
            stop.day for stop, _ in occurrences if stop.kind in ("activity", "booking")
        }
        required_days = sorted(allowed_days | occurrence_days) if len(allowed_days) == 1 else []
        issues.append(
            {
                "category": "draft_error",
                "code": "visit_constraint" if place_id else "visit_identity",
                "constraint_ids": [constraint.constraint_id],
                **({"day": next(iter(allowed_days))} if len(allowed_days) == 1 else {}),
                "related_days": sorted(allowed_days | occurrence_days),
                "required_days": required_days,
                "place_id": place_id,
                "place_query": constraint.place_query,
                "message": f"硬要求「{constraint.place_query or place_id}」未被完整满足："
                + (
                    "需按用户固定日期和精确起止时刻安排booking站点。"
                    if constraint.fixed
                    else f"需在任一允许日期/窗口内完整停留至少{constraint.min_duration_minutes}分钟。"
                ),
                "expected": {
                    "windows": [window.model_dump() for window in constraint.windows],
                    "min_duration_minutes": constraint.min_duration_minutes,
                    "fixed": constraint.fixed,
                    "kind": "booking" if constraint.fixed else ["activity", "booking"],
                },
                "actual": [
                    {"stop_number": number, **stop.model_dump(mode="json")}
                    for stop, number in occurrences
                ],
            }
        )
    return issues


def validate_research_facts(output, facts: list[dict]) -> list[dict]:
    """Check only explicit web-reported structures, never infer windows from prose."""
    issues = []
    for stop, number in _numbered_stops(output):
        if stop.kind not in ("activity", "booking"):
            continue
        for fact in facts:
            if (
                fact.get("status") != "found"
                or fact.get("place_id") != stop.place_id
                or fact.get("day") != stop.day
            ):
                continue
            field = fact.get("field")
            windows = fact.get("windows") or []
            code = None
            if field == "opening_hours":
                if fact.get("closed") is True:
                    code, detail = "reported_closed", "网页来源显示该日关闭"
                elif windows and not any(
                    window.get("day") == stop.day
                    and window["start_time"] <= stop.start_time
                    and stop.end_time <= window["end_time"]
                    for window in windows
                ):
                    code, detail = "reported_opening_window", "完整停留未落在网页报告的开放窗口内"
            elif field == "last_admission":
                latest = fact.get("last_admission")
                if latest and stop.start_time > latest:
                    code, detail = (
                        "reported_last_admission",
                        f"开始时刻晚于网页报告的{latest}最晚入场",
                    )
            if code:
                issues.append(
                    {
                        "category": "draft_error",
                        "code": code,
                        "day": stop.day,
                        "stop_number": number,
                        "place_id": stop.place_id,
                        "message": f"第{stop.day}天第{number}站：{detail}，请核对来源并修订。",
                        "expected": {
                            key: fact.get(key) for key in ("windows", "closed", "last_admission")
                        },
                        "actual": {"start_time": stop.start_time, "end_time": stop.end_time},
                        "evidence_status": "web_reported",
                        "source_ids": fact.get("source_ids", []),
                        "sources": fact.get("sources", []),
                        "queried_at": fact.get("queried_at"),
                        "quote": fact.get("quote", ""),
                    }
                )
    return issues
