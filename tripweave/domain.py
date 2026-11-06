"""Small, explicit domain contract for the offline Shanghai experiment."""

from datetime import date
from typing import Annotated

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
    origin_query: str = "人民广场"
    start_time: Clock = "09:00"
    end_time: Clock = "18:00"
    interests: str = Field(default="城市漫步", max_length=200)
    booking_query: str = "上海博物馆"
    booking_start: Clock = "14:00"
    booking_end: Clock = "15:00"
    max_activities: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def valid_windows(self):
        if self.start_time >= self.end_time:
            raise ValueError("出发时间必须早于结束时间")
        if self.booking_query and self.booking_start >= self.booking_end:
            raise ValueError("预约开始必须早于结束")
        return self


class Stop(BaseModel):
    place_id: str
    start_time: Clock
    end_time: Clock
    reason: str


class Itinerary(BaseModel):
    summary: str
    stops: list[Stop]


PLACES = [
    {"id": "square", "name": "人民广场", "address": "上海黄浦区（固定样本）"},
    {"id": "museum", "name": "上海博物馆", "address": "人民广场馆（固定样本）"},
    {"id": "park", "name": "人民公园", "address": "上海黄浦区（固定样本）"},
    {"id": "nanjing", "name": "南京路步行街", "address": "上海黄浦区（固定样本）"},
    {"id": "bund", "name": "外滩", "address": "上海黄浦区（固定样本）"},
]
# Educational, synthetic walking minutes. These are NOT current map measurements.
MATRIX = [
    [0, 8, 6, 18, 40],
    [8, 0, 12, 24, 42],
    [6, 12, 0, 16, 38],
    [18, 24, 16, 0, 22],
    [40, 42, 38, 22, 0],
]
INDEX = {p["id"]: i for i, p in enumerate(PLACES)}


def place_id(name: str) -> str:
    for p in PLACES:
        if name == p["name"]:
            return p["id"]
    raise ValueError("固定样本未覆盖该地点，请从页面列出的五个地点选择")


def travel(a: str, b: str) -> int:
    return MATRIX[INDEX[a]][INDEX[b]]


def validate_plan(req: Requirements, plan: Itinerary) -> list[dict]:
    issues = []

    def issue(code, message):
        issues.append({"code": code, "message": message})

    if not 1 <= len(plan.stops) <= req.max_activities:
        issue("activity_count", "活动数量不在请求范围内")
    previous, end = place_id(req.origin_query), minutes(req.start_time)
    seen = set()
    for s in plan.stops:
        if s.place_id not in INDEX:
            issue("unknown_place", "行程引用了未查询的地点")
            continue
        if s.place_id in seen:
            issue("duplicate", "同一地点重复安排")
        seen.add(s.place_id)
        start, stop_end = minutes(s.start_time), minutes(s.end_time)
        if start < end + travel(previous, s.place_id):
            issue("connection", "活动时间不足以完成上一段步行，或早于出发时间")
        if stop_end <= start or stop_end > minutes(req.end_time):
            issue("time_window", "活动时长或结束边界不合法")
        previous, end = s.place_id, stop_end
    if req.booking_query:
        booking = place_id(req.booking_query)
        if not any(
            s.place_id == booking
            and s.start_time == req.booking_start
            and s.end_time == req.booking_end
            for s in plan.stops
        ):
            issue("booking", "固定预约的地点或时段未被完整保留")
    return issues


def fixture_plan(req: Requirements) -> Itinerary:
    """Deterministic fixture policy, explicitly NOT an LLM or a general optimizer."""
    previous, now = place_id(req.origin_query), minutes(req.start_time)
    booking = place_id(req.booking_query) if req.booking_query else None
    order = ["park", "nanjing", "bund", "museum", "square"]
    if "外滩" in req.interests:
        order.remove("bund")
        order.insert(0, "bund")
    stops = []
    for pid in order:
        if pid in (previous, booking) or len(stops) >= req.max_activities - bool(booking):
            continue
        start = now + travel(previous, pid)
        end = start + 45
        limit = (
            minutes(req.booking_start) - travel(pid, booking) if booking else minutes(req.end_time)
        )
        if end > limit:
            continue
        stops.append(
            Stop(
                place_id=pid,
                start_time=clock(start),
                end_time=clock(end),
                reason="固定策略选择：45 分钟示例游览",
            )
        )
        previous, now = pid, end
    if booking:
        stops.append(
            Stop(
                place_id=booking,
                start_time=req.booking_start,
                end_time=req.booking_end,
                reason="保留用户填写的固定预约",
            )
        )
    return Itinerary(
        summary="离线固定策略生成的实验行程；开放时间、票价、景区内步行未核实。", stops=stops
    )
