"""Explicit synthetic planner packets for the draft-first production protocol."""

import json
from copy import deepcopy

import httpx2


def route_queries(plan, origin_id="o", transport="mixed", *, origin=None):
    if origin is not None:
        origin_id = origin
    seen, queries = set(), []
    days = sorted({stop["day"] for stop in plan["stops"]})
    for day in days:
        overnight = any(
            stop["day"] == day - 1 and stop["kind"] == "hotel" for stop in plan["stops"]
        )
        previous = plan["hotel_id"] if overnight else origin_id
        for stop in (stop for stop in plan["stops"] if stop["day"] == day):
            key = previous, stop["place_id"], transport
            if key not in seen:
                seen.add(key)
                queries.append(
                    {
                        "kind": "route",
                        "origin_id": previous,
                        "destination_id": stop["place_id"],
                        "transport": transport,
                        "reason": f"核对第{day}天相邻站点交通。",
                    }
                )
            previous = stop["place_id"]
    return queries


def packet(plan=None, *, queries=None, needs=None, assumptions=()):
    if plan is None:
        plan = {
            "summary": "合成首稿，仅用于HTTP模拟。",
            "hotel_id": None,
            "stops": [
                {
                    "day": 1,
                    "place_id": "p0",
                    "start_time": "10:00",
                    "end_time": "11:00",
                    "kind": "activity",
                    "reason": "合成游览安排。",
                    "title": "",
                }
            ],
        }
    return {
        "draft": deepcopy(plan),
        "query_requests": route_queries(plan) if queries is None else deepcopy(queries),
        "information_needs": deepcopy(needs or []),
        "assumptions": list(assumptions),
    }


def day_patch(context, plan):
    """Turn a test's intended itinerary into the wire format for a local edit."""
    current = context["draft"]
    before_days = {stop["day"] for stop in current["stops"]}
    days = before_days | {stop["day"] for stop in plan["stops"]}
    hotel_change = plan["hotel_id"] != current["hotel_id"]
    hotel_days = before_days.intersection(context.get("hotel_revision_days", []))
    patch = {"days": []}
    for day in sorted(days):
        before = [stop for stop in current["stops"] if stop["day"] == day]
        after = [stop for stop in plan["stops"] if stop["day"] == day]
        if before != after or (hotel_change and day in hotel_days):
            patch["days"].append({"day": day, "stops": deepcopy(after)})
    if hotel_change:
        patch["hotel_id"] = plan["hotel_id"]
    if plan["summary"] != current["summary"]:
        patch["summary"] = plan["summary"]
    return patch


def phase_packet(context, plan, **kwargs):
    payload = packet(plan, **kwargs)
    if "revision_days" in context:
        payload["patch"] = day_patch(context, payload.pop("draft"))
    return payload


def negotiation_packet(context, response):
    payload = dict(response)
    if "revised_itinerary" in payload:
        payload["patch"] = day_patch(context, payload.pop("revised_itinerary"))
    return payload


def reply(name, arguments, number=1):
    return httpx2.Response(
        200,
        json={
            "id": f"draft-completion-{number}",
            "object": "chat.completion",
            "created": 1,
            "model": "deepseek-flash",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"draft-call-{number}",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 42, "completion_tokens": 20, "total_tokens": 62},
        },
    )


proposal = packet
routes = route_queries
