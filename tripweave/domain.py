"""Typed requirements and itinerary contracts shared by real planning modes."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

Clock = Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]


def minutes(value: str) -> int:
    h, m = map(int, value.split(":"))
    return h * 60 + m


def clock(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


class Requirements(BaseModel):
    city: str = Field(default="上海", max_length=40)
    date: date
    days: int = Field(default=1, ge=1, le=3)
    party_size: int = Field(default=2, ge=1, le=6)
    budget_total: int = Field(default=3000, ge=100, le=100000)
    hotel_budget: int = Field(default=450, ge=100, le=5000)
    hotel_preference: Literal["balanced", "value", "riverside"] = "balanced"
    hotel_id: str | None = None
    pace: Literal["relaxed", "balanced", "full"] = "balanced"
    transport: Literal["walking", "mixed"] = "mixed"
    origin_query: str = Field(default="人民广场", max_length=200)
    start_time: Clock = "09:00"
    end_time: Clock = "20:30"
    interests: str = Field(default="城市漫步、历史人文、美食探索", max_length=500)
    notes: str = Field(default="", max_length=1500)
    booking_query: str = Field(default="上海博物馆", max_length=200)
    booking_day: int = Field(default=1, ge=1, le=3)
    booking_start: Clock = "14:00"
    booking_end: Clock = "15:00"
    max_activities: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def valid_windows(self):
        if self.start_time >= self.end_time:
            raise ValueError("出发时间必须早于每日结束边界")
        if self.booking_query and (
            self.booking_start >= self.booking_end or self.booking_day > self.days
        ):
            raise ValueError("请检查预约时段与预约所在天数")
        return self


class Stop(BaseModel):
    day: int = Field(default=1, ge=1, le=3)
    place_id: str
    start_time: Clock
    end_time: Clock
    kind: Literal["activity", "booking", "meal", "hotel"] = "activity"
    reason: str = Field(max_length=1000)
    title: str = Field(default="", max_length=100)


class Itinerary(BaseModel):
    summary: str = Field(max_length=1500)
    hotel_id: str | None = None
    stops: list[Stop] = Field(max_length=40)
