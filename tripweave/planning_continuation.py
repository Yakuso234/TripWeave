"""Restore saved draft evidence and bind user-supplied facts to specific gaps."""

from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .domain import Itinerary, Requirements
from .planning_contracts import Clock, Contract, PlaceId, TimeWindow
from .query_executor import query_key


class FactScope(Contract):
    place_id: PlaceId
    day: int = Field(ge=1, le=7)


class UserOpeningHours(FactScope):
    field: Literal["opening_hours"]
    windows: list[TimeWindow] = Field(default_factory=list, max_length=14)
    closed: bool = False

    @model_validator(mode="after")
    def check_windows(self):
        if self.closed == bool(self.windows) or any(w.day != self.day for w in self.windows):
            raise ValueError("需要填写当日开放时段，或明确当天关闭")
        return self


class UserLastAdmission(FactScope):
    field: Literal["last_admission"]
    last_admission: Clock


class UserReservationRequirement(FactScope):
    field: Literal["reservation_requirement"]
    required: bool
    channel: str = Field(default="", max_length=300)


class UserReservationChannel(FactScope):
    field: Literal["reservation_channel"]
    channel: str = Field(min_length=1, max_length=300)


UserFact = Annotated[
    UserOpeningHours | UserLastAdmission | UserReservationRequirement | UserReservationChannel,
    Field(discriminator="field"),
]


def fact_key(fact):
    return fact["place_id"], fact["day"], fact["field"]


def user_fact_entries(item: UserFact, req: Requirements) -> list[dict]:
    entry = {
        "place_id": item.place_id,
        "day": item.day,
        "date": (req.date + timedelta(days=item.day - 1)).isoformat(),
        "field": item.field,
        "status": "found",
        "evidence_status": "user_reported",
        "quote": "",
        "sources": [],
        "queried_at": None,
    }
    if isinstance(item, UserOpeningHours):
        value = "当天关闭" if item.closed else "用户提供当天开放时段"
        return [
            {
                **entry,
                "value": value,
                "closed": item.closed,
                "windows": [window.model_dump(mode="json") for window in item.windows],
            }
        ]
    if isinstance(item, UserLastAdmission):
        return [
            {
                **entry,
                "value": f"当天最晚入场 {item.last_admission}",
                "last_admission": item.last_admission,
            }
        ]
    if isinstance(item, UserReservationChannel):
        return [{**entry, "value": item.channel}]
    facts = [
        {
            **entry,
            "value": "当天必须提前预约" if item.required else "当天不要求提前预约",
            "reservation_required": item.required,
        }
    ]
    if item.channel:
        facts.append({**entry, "field": "reservation_channel", "value": item.channel})
    return facts


@dataclass
class PlanningContinuation:
    draft: Itinerary
    places: list[dict]
    facts: list[dict]
    new_facts: list[dict]
    requests: list[dict]
    critical_needs: list[dict]
    query_results: dict
    search_attempts: dict
    limits: list[dict]
    diagnostics: list[dict]

    @classmethod
    def from_candidate(cls, req: Requirements, source: dict, supplied: list[UserFact]):
        if source.get("outcome") != "needs_information" or not source.get("draft"):
            raise ValueError("请选择仍有待确认资料的规划草稿")
        issues = [
            issue for issue in source.get("issues", []) if issue.get("category") == "missing_fact"
        ]
        gaps = {
            (issue.get("place_id"), issue.get("day"), field)
            for issue in issues
            for field in issue.get("missing_fields", [])
        }
        places = source.get("places", [])
        known_ids = {place["id"] for place in places}
        additions = {}
        for item in supplied:
            key = item.place_id, item.day, item.field
            if key not in gaps or item.place_id not in known_ids or item.day > req.days:
                raise ValueError("补充资料必须对应这份草稿中待确认的地点、日期和字段")
            for entry in user_fact_entries(item, req):
                if fact_key(entry) in additions:
                    raise ValueError("同一地点、日期和字段请只填写一次")
                additions[fact_key(entry)] = entry

        latest = {fact_key(fact): fact for fact in source.get("research_facts", [])}
        # An optional booking channel may accompany a missing booking requirement.
        # Keep previously confirmed user facts immutable in subsequent continuations.
        if any(latest.get(key, {}).get("evidence_status") == "user_reported" for key in additions):
            raise ValueError("这份草稿已有用户补充的同一字段，请保留原资料")
        latest.update(additions)
        requests = source.get("research_requests", [])
        needs = [need for request in requests for need in request["needs"]]
        needs.extend(
            {"place_id": i["place_id"], "day": i["day"], "fields": i["missing_fields"]}
            for i in issues
            if i.get("place_id") in known_ids and i.get("day") and i.get("missing_fields")
        )
        grouped_needs = {}
        for need in needs:
            key = need["place_id"], need["day"]
            if key not in grouped_needs:
                grouped_needs[key] = {**need, "fields": []}
            fields = grouped_needs[key]["fields"]
            fields.extend(field for field in need["fields"] if field not in fields)
        attempts = Counter(
            {
                fact_key(item): item["search_attempts"]
                for item in source.get("carried_research_attempts", [])
            }
        )
        for event in source.get("trace", []):
            if event.get("event") == "research_search_attempt":
                for field in event.get("fields", []):
                    attempts[(event["place_id"], event["day"], field)] += 1

        return cls(
            draft=Itinerary.model_validate(source["draft"]),
            places=places,
            facts=list(latest.values()),
            new_facts=list(additions.values()),
            requests=requests,
            critical_needs=list(grouped_needs.values()),
            query_results={
                query_key(entry["query"]): entry
                for entry in [
                    *source.get("carried_query_results", []),
                    *source.get("query_log", []),
                ]
                if entry.get("status") == "ok"
            },
            search_attempts=dict(attempts),
            limits=[
                item
                for item in source.get("research_limits", [])
                if fact_key(item) not in additions
            ],
            diagnostics=[
                {**item, "resolved": True} if fact_key(item) in additions else item
                for item in source.get("research_diagnostics", [])
            ],
        )
