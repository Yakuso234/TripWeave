"""Execute a planner's declared map queries, with per-run memory and safe traces.

This module does not plan a trip, create additional queries or call a model.
Cached evidence is normal reuse; the caller decides whether a whole unchanged
draft and repeated query batch represents lack of progress.
"""

from copy import deepcopy
from time import monotonic
from unicodedata import normalize

from .maps import MapError, cached_route, transport_allowed

MAX_BATCH_QUERIES = 24
QUERY_FIELDS = {
    "route": {"kind", "origin_id", "destination_id", "transport", "reason"},
    "nearby_restaurants": {"kind", "place_id", "reason", "purpose"},
    "place_search": {"kind", "query", "reason"},
}
PLACE_FIELDS = (
    "id",
    "name",
    "address",
    "type",
    "city",
    "citycode",
    "longitude",
    "latitude",
    "source",
    "queried_at",
    "rating",
)
ROUTE_FIELDS = (
    "origin_id",
    "destination_id",
    "duration_minutes",
    "distance_meters",
    "mode",
    "source",
    "queried_at",
)


def _name(value):
    return normalize("NFKC", value).strip().casefold() if isinstance(value, str) else ""


def _city(value):
    name = _name(value)
    return name.removesuffix("市")


def query_key(query):
    """Only actual query conditions affect reuse; reason and list order do not."""
    kind = query.get("kind")
    if kind == "route":
        return kind, query.get("origin_id"), query.get("destination_id"), query.get("transport")
    if kind == "nearby_restaurants":
        return kind, query.get("place_id")
    if kind == "place_search":
        return kind, _name(query.get("query"))
    return None


def _project(value, fields):
    return {key: value[key] for key in fields if key in value}


class QueryExecutor:
    def __init__(self, req, maps, trace, *, allowed_queries=()):
        self.req, self.maps, self.trace = req, maps, trace
        self.cache = {}
        self.allowed_place_ids = set(maps.places)
        self.allowed_queries = {
            _name(query)
            for query in (
                *req.must_visit,
                *(constraint.place_query for constraint in req.visit_constraints),
                req.booking_query,
                *allowed_queries,
            )
            if _name(query)
        }
        self.city_codes = {
            place["citycode"]
            for place in maps.places.values()
            if _city(place.get("city")) == _city(req.city) and place.get("citycode")
        }

    def allow_query_names(self, names):
        """Add names already authorized by server-side user-revision matching.

        This is not exposed as a model tool. The orchestration layer must check
        that a proposed new name occurs in the user's actual revision text,
        rather than trusting the model's query reason as authorization.
        """
        self.allowed_queries.update(
            _name(name)
            for name in names
            if isinstance(name, str)
            and 1 <= len(name.strip()) <= 200
            and "://" not in name
            and not any(ord(ch) < 32 for ch in name)
        )

    def _same_city(self, place, *, existing=False):
        city, code = place.get("city"), place.get("citycode")
        if city and _city(city) != _city(self.req.city):
            return False
        if code and self.city_codes and code not in self.city_codes:
            return False
        if city:
            return True
        if code and code in self.city_codes:
            return True
        # Existing IDs have already been scoped by prepare_evidence. Some basic
        # responses retain only a city code. New results need explicit city
        # identity, rather than an inference from nearby coordinates.
        return existing

    def _known(self, place_id):
        return (
            isinstance(place_id, str)
            and place_id in self.allowed_place_ids
            and place_id in self.maps.places
            and self._same_city(self.maps.places[place_id], existing=True)
        )

    def _validate(self, query):
        kind = query.get("kind")
        fields = QUERY_FIELDS.get(kind) if isinstance(kind, str) else None
        if fields is None:
            return "unsupported_query", "不支持该查询类型。"
        if set(query) - fields:
            return "unexpected_query_fields", "查询含合同之外的字段，不接受自定义网址、城市或凭据。"
        reason = query.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            return "query_reason_missing", "查询必须说明具体必要性，且不超过500字。"
        if kind == "route":
            if not transport_allowed(self.req.transport, query.get("transport")):
                return "transport_mismatch", "路线交通方式必须遵守本次用户设置，不能静默更换。"
            if not self._known(query.get("origin_id")) or not self._known(
                query.get("destination_id")
            ):
                return "unknown_place_id", "路线起点和终点必须是本次已查询且城市身份适用的地点ID。"
            origin = self.maps.places[query["origin_id"]]
            destination = self.maps.places[query["destination_id"]]
            if (
                origin.get("citycode")
                and destination.get("citycode")
                and origin["citycode"] != destination["citycode"]
            ):
                return "city_mismatch", "路线起点和终点的供应商城市代码不一致。"
        elif kind == "nearby_restaurants":
            if query.get("purpose") not in (None, "required_for_plan"):
                return "invalid_query_purpose", "不支持该餐饮查询用途。"
            if not self._known(query.get("place_id")):
                return "unknown_place_id", "餐饮查询锚点必须是本次已查询且城市身份适用的地点ID。"
        else:
            name = query.get("query")
            if (
                not isinstance(name, str)
                or not 1 <= len(name.strip()) <= 200
                or "://" in name
                or any(ord(ch) < 32 for ch in name)
            ):
                return "invalid_place_query", "请提供明确地点名称，不能提供网址或控制字符。"
            if _name(name) not in self.allowed_queries:
                return (
                    "unrequested_place_search",
                    "只能查找用户明确要求或本次修订明确新增的地点，不能扩展搜索目标。",
                )
        return None

    async def execute(self, queries, *, phase="evidence", model_round=None):
        """Return per-query outcomes and whether previously unseen evidence arrived.

        new_information concerns delivery to this executor's caller. A route
        cached by earlier program checks can be new to the model even though it
        costs no map request. Successes and attempted failures are cached for
        this run, so identical failure loops do not repeatedly spend quota.
        """
        if not isinstance(queries, (list, tuple)) or len(queries) > MAX_BATCH_QUERIES:
            raise ValueError("每批结构化地图查询必须为列表，且最多24项。")
        results, any_new = [], False
        for raw in queries:
            query = raw.model_dump(mode="json") if hasattr(raw, "model_dump") else raw
            if not isinstance(query, dict):
                query = {}
            kind = query.get("kind")
            fields = QUERY_FIELDS.get(kind, {"kind"}) if isinstance(kind, str) else {"kind"}
            # Extra untrusted fields never enter logs or the model result.
            shown_query = {
                key: value[:500]
                for key, value in query.items()
                if key in fields and isinstance(value, str)
            }
            started = monotonic()
            item = {
                "query": shown_query,
                "status": "not_executed",
                "data": None,
                "error": None,
                "cache_hit": False,
                "new_information": False,
                "phase": phase,
                "model_round": model_round,
            }
            invalid = self._validate(query)
            key = query_key(query) if invalid is None else None
            if invalid:
                item.update(code=invalid[0], error=invalid[1])
            elif key in self.cache:
                item.update(
                    deepcopy(self.cache[key]),
                    query=shown_query,
                    cache_hit=True,
                    new_information=False,
                    phase=phase,
                    model_round=model_round,
                )
            else:
                # Existing MapClient routes remain reusable when the map quota
                # is exhausted: no request is needed to return cached evidence.
                map_cached = (
                    query["kind"] == "route" and cached_route(self.maps.cache, *key[1:]) is not None
                )
                item["cache_hit"] = map_cached
                if self.maps.calls >= self.maps.max_calls and not map_cached:
                    item.update(
                        code="map_budget_exhausted", error="本次地图请求预算已用完，该查询未执行。"
                    )
                else:
                    try:
                        if query["kind"] == "route":
                            result = await self.maps.route(
                                self.maps.places[query["origin_id"]],
                                self.maps.places[query["destination_id"]],
                                self.req.city,
                                query["transport"],
                            )
                            item["data"] = _project(result, ROUTE_FIELDS)
                        elif query["kind"] == "nearby_restaurants":
                            places = await self.maps.nearby(self.maps.places[query["place_id"]])
                            accepted = [place for place in places if self._same_city(place)]
                            if places and not accepted:
                                raise MapError("返回餐饮地点的城市身份不适用于本次旅行或无法核实。")
                            self.allowed_place_ids.update(place["id"] for place in accepted)
                            item["data"] = [_project(place, PLACE_FIELDS) for place in accepted]
                        else:
                            place = await self.maps.resolve(query["query"], self.req.city)
                            if not self._same_city(place):
                                raise MapError("返回地点的城市身份不适用于本次旅行或无法核实。")
                            self.allowed_place_ids.add(place["id"])
                            item["data"] = _project(place, PLACE_FIELDS)
                        item.update(status="ok", new_information=True)
                    except MapError as exc:
                        item.update(
                            status="error",
                            code="map_query_failed",
                            error=str(exc),
                            new_information=True,
                        )
                    # MapClient sanitizes provider/network errors. Do not echo
                    # arbitrary HTTP exceptions or credential-bearing URLs here.
                    self.cache[key] = deepcopy(item)
                    any_new = any_new or item["new_information"]
            item["elapsed_ms"] = round((monotonic() - started) * 1000)
            self.trace.append(
                {
                    "event": "query_execution",
                    "detail": f"结构化地图查询：{item['status']}",
                    **deepcopy(item),
                }
            )
            results.append(item)
        return {"results": results, "new_information": any_new}
