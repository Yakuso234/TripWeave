from copy import deepcopy

from tripweave.domain import Itinerary, Requirements
from tripweave.route_evidence import hotel_shortlist


def hotel_fixture():
    req = Requirements(date="2026-10-09", days=2, nights=1)
    draft = Itinerary.model_validate(
        {
            "hotel_id": "base",
            "summary": "合成酒店比较样本",
            "stops": [
                {
                    "day": 1,
                    "place_id": "evening",
                    "kind": "meal",
                    "start_time": "18:00",
                    "end_time": "19:00",
                    "reason": "晚餐末站",
                },
                {
                    "day": 1,
                    "place_id": "base",
                    "kind": "hotel",
                    "start_time": "20:00",
                    "end_time": "21:00",
                    "reason": "过夜当天返回酒店",
                },
                {
                    "day": 2,
                    "place_id": "morning",
                    "kind": "activity",
                    "start_time": "10:00",
                    "end_time": "11:00",
                    "reason": "次日首站",
                },
            ],
        }
    )
    places = {
        name: {"id": name, "longitude": x, "latitude": 31.23}
        for name, x in [
            ("evening", 121.4),
            ("morning", 121.52),
            ("base", 121.4),
            ("near_evening", 121.407),
            ("outer", 121.43),
            ("central", 121.46),
            ("near_central", 121.45),
            ("far", 121.68),
        ]
    }
    hotels = [p for name, p in places.items() if name not in {"evening", "morning"}]
    cache = {
        ("evening", "base", "mixed"): {"duration_minutes": 20},
        ("base", "morning", "mixed"): {"duration_minutes": 60},
    }
    return req, draft, hotels, places, cache


def test_shortlist_prefers_hotels_close_to_both_fixed_anchors():
    req, draft, hotels, places, cache = hotel_fixture()
    selection = hotel_shortlist(req, draft, hotels, places, cache, min_leg_minutes=30)
    assert selection == hotel_shortlist(req, draft, list(reversed(hotels)), places, cache)
    anchors, chosen, queries = selection
    assert anchors == ("mixed", ((1, "evening", True), (2, "morning", False)))
    assert chosen == ("central", "near_central", "outer")
    assert [(q["origin_id"], q["destination_id"], q["transport"]) for q in queries] == [
        pair
        for hotel_id in chosen
        for pair in (("evening", hotel_id, "mixed"), (hotel_id, "morning", "mixed"))
    ]
    assert draft.hotel_id == "base"


def test_shortlist_reuses_a_walking_estimate_and_admits_only_whole_comparisons():
    req, draft, hotels, places, cache = hotel_fixture()
    cache["evening", "central", "walking"] = {"duration_minutes": 30}
    cache["evening", "near_central", "mixed"] = {"duration_minutes": 33}
    cache["near_central", "morning", "mixed"] = {"duration_minutes": 27}
    key, chosen, queries = hotel_shortlist(req, draft, hotels, places, cache, max_queries=1)
    assert chosen == ("central", "near_central")
    assert [(q["origin_id"], q["destination_id"]) for q in queries] == [("central", "morning")]

    selected = deepcopy(draft)
    selected.hotel_id = selected.stops[1].place_id = "near_central"
    changed_key, _, _ = hotel_shortlist(req, selected, hotels, places, cache)
    assert changed_key == key


def test_shortlist_requires_independent_anchors_complete_baseline_and_route_pressure():
    req, draft, hotels, places, cache = hotel_fixture()
    assert hotel_shortlist(req, draft, hotels, places, cache, min_leg_minutes=61) is None

    manual = req.model_copy(update={"hotel_id": "base"})
    assert hotel_shortlist(manual, draft, hotels, places, cache) is None
    incomplete = draft.model_copy(update={"stops": draft.stops[:2]})
    assert hotel_shortlist(req, incomplete, hotels, places, cache) is None
    onsite_breakfast = draft.model_copy(
        update={"stops": [*draft.stops[:2], draft.stops[2].model_copy(update={"place_id": "base"})]}
    )
    assert hotel_shortlist(req, onsite_breakfast, hotels, places, cache) is None

    del cache["base", "morning", "mixed"]
    assert hotel_shortlist(req, draft, hotels, places, cache) is None
