"""Typed requirements and itinerary contracts shared by real planning modes."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .planning_contracts import VisitConstraint

Clock = Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]
StrictPreference = Literal[
    "max_attractions",
    "max_sightseeing_minutes",
    "max_sightseeing_ratio",
    "max_transport_minutes",
    "start_time",
    "end_time",
]


def minutes(value: str) -> int:
    h, m = map(int, value.split(":"))
    return h * 60 + m


def clock(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


class AttractionSelection(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)


class TravelPreferences(BaseModel):
    strict_preferences: list[StrictPreference] = Field(
        default_factory=list,
        max_length=6,
        description="用户明确锁定的数值或每日时间要求；其他数值为期望目标，允许解释偏离",
    )
    hotel_budget: int = Field(default=450, ge=100, le=5000)
    hotel_preference: Literal["balanced", "value", "riverside"] = "balanced"
    pace: Literal["relaxed", "balanced", "full"] = "balanced"
    transport: Literal["walking", "mixed"] = "mixed"
    interests: str = Field(default="城市漫步、历史人文、美食探索", max_length=500)
    custom_interests: str = Field(default="", max_length=300)
    custom_pace: str = Field(default="", max_length=300)
    custom_transport: str = Field(default="", max_length=300)
    custom_hotel: str = Field(default="", max_length=300)
    max_attractions: int | None = Field(
        default=3,
        ge=0,
        le=20,
        description="每日不同景点/体验地点期望数量；同地点分段只计一个，空值不设目标；仅strict_preferences明确锁定时为上限",
    )
    max_sightseeing_minutes: int | None = Field(
        default=None,
        ge=0,
        le=1440,
        description="每日实际游玩累计分钟期望值；空值不设目标；仅明确锁定时为上限",
    )
    max_sightseeing_ratio: float | None = Field(
        default=None,
        ge=0,
        le=100,
        allow_inf_nan=False,
        description="每日游玩占原始出发至结束固定窗口的期望百分比；仅明确锁定时为上限",
    )
    max_transport_minutes: int | None = Field(
        default=None,
        ge=0,
        le=1440,
        description="每日地图交通累计分钟期望值，含首段和回酒店，不含缓冲；仅明确锁定时为上限",
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_activity_limit(cls, values):
        # Read old SQLite records/clients without rewriting historical snapshots.
        if isinstance(values, dict) and "max_activities" in values:
            values = dict(values)
            if "max_attractions" not in values:
                values["max_attractions"] = values["max_activities"]
            values.pop("max_activities")
        return values


class Requirements(TravelPreferences):
    model_config = ConfigDict(extra="forbid")

    city: str = Field(default="上海", max_length=40)
    date: date
    days: int = Field(default=1, ge=1, le=7)
    nights: int | None = Field(default=None, ge=0, le=6)
    party_size: int = Field(default=2, ge=1, le=6)
    budget_total: int = Field(
        default=3000,
        ge=100,
        le=100000,
        description="全体人数整趟期望金额，不是最高承诺；硬性金额要求在notes明确",
    )
    hotel_id: str | None = None
    origin_query: str = Field(default="人民广场", max_length=200)
    start_time: Clock = Field(
        default="09:00", description="每日期望出发时刻；仅明确锁定时不可提前，目的地当地时间"
    )
    end_time: Clock = Field(
        default="20:30", description="每日期望结束时刻；仅明确锁定时为最晚边界，目的地当地时间"
    )
    must_visit: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list, max_length=5
    )
    selected_attractions: list[AttractionSelection] = Field(default_factory=list, max_length=5)
    visit_constraints: list[VisitConstraint] = Field(default_factory=list, max_length=10)
    notes: str = Field(default="", max_length=1500)
    booking_query: str = Field(default="", max_length=200)
    booking_day: int = Field(default=1, ge=1, le=7)
    booking_start: Clock = "14:00"
    booking_end: Clock = "15:00"

    @property
    def hotel_nights(self) -> int:
        return self.days - 1 if self.nights is None else self.nights

    @model_validator(mode="after")
    def valid_windows(self):
        ids = [constraint.constraint_id for constraint in self.visit_constraints]
        if len(ids) != len(set(ids)):
            raise ValueError("景点时间要求的标识不能重复")
        if any(window.day > self.days for c in self.visit_constraints for window in c.windows):
            raise ValueError("景点时间要求不能超出旅行天数，请调整可选日期")
        if self.hotel_nights >= self.days:
            raise ValueError("住宿晚数须少于旅行天数")
        if self.start_time >= self.end_time:
            raise ValueError("出发时间必须早于每日结束边界")
        if self.booking_query and (
            self.booking_start >= self.booking_end or self.booking_day > self.days
        ):
            raise ValueError("请检查预约时段与预约所在天数")
        return self


class Stop(BaseModel):
    day: int = Field(ge=1, le=7)
    place_id: str
    start_time: Clock = Field(
        description="到达并开始本站的时刻，不是从上一站出发的时刻，目的地当地时间"
    )
    end_time: Clock = Field(
        description="结束本站的时刻，也是前往下一站的最早出发时刻，目的地当地时间"
    )
    kind: Literal["activity", "booking", "meal", "hotel"] = Field(
        default="activity",
        description="activity为实际参观体验，booking为用户固定预约，meal为用餐，hotel为住宿；出发和纯休息不建站",
    )
    reason: str = Field(max_length=1000)
    title: str = Field(default="", max_length=100)


class Itinerary(BaseModel):
    summary: str = Field(max_length=1500)
    hotel_id: str | None = None
    stops: list[Stop] = Field(max_length=56)
