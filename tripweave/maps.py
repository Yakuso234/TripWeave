"""Amap Web Service adapter. Never emits the credential-bearing request URL."""

import asyncio
import math
from datetime import UTC, datetime

import httpx2


class MapError(Exception):
    pass


def distance(a: dict, b: dict) -> float:
    return (float(a["longitude"]) - float(b["longitude"])) ** 2 + (
        float(a["latitude"]) - float(b["latitude"])
    ) ** 2


class MapClient:
    def __init__(self, client: httpx2.AsyncClient, key: str, max_calls: int = 50):
        self.client, self.key, self.max_calls = client, key, max_calls
        self.calls = 0
        self.cache = {}
        self.places = {}

    async def request(self, path: str, params: dict) -> dict:
        for attempt in range(2):
            if self.calls >= self.max_calls:
                raise MapError("本次地图请求已达到上限，请减少活动或重试")
            self.calls += 1
            try:
                response = await self.client.get(
                    "https://restapi.amap.com" + path,
                    params={**params, "key": self.key, "output": "JSON"},
                )
                if response.status_code >= 500 and attempt == 0:
                    await asyncio.sleep(0.3)
                    continue
                if response.status_code != 200:
                    raise MapError(f"地图接口HTTP {response.status_code}，请检查服务或额度")
                body = response.json()
                if not isinstance(body, dict):
                    raise MapError("地图响应结构异常，无法读取地点或路线")
                if body.get("status") != "1":
                    code = str(body.get("infocode", "unknown"))
                    code = code if code.isdigit() and len(code) <= 8 else "unknown"
                    if code == "10003":
                        raise MapError("高德调用额度不足，请检查账户配额")
                    raise MapError(f"高德请求失败，错误码{code}；请检查Web服务Key与权限")
                return body
            except (httpx2.RequestError, ValueError) as exc:
                if attempt == 0:
                    await asyncio.sleep(0.3)
                    continue
                raise MapError("地图连接超时或响应格式异常，请稍后重试") from exc
        raise MapError("地图请求未完成")

    def normalize(self, raw: dict) -> dict | None:
        if not isinstance(raw, dict):
            return None
        try:
            lon, lat = map(float, str(raw["location"]).split(","))
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                return None
            if not all(isinstance(raw.get(k), str) and raw[k] for k in ("id", "name")):
                return None
        except KeyError, TypeError, ValueError:
            return None
        place = {
            "id": raw["id"],
            "name": raw["name"],
            "address": raw.get("address") if isinstance(raw.get("address"), str) else "",
            "area": raw.get("adname") if isinstance(raw.get("adname"), str) else "",
            "city": raw.get("cityname") if isinstance(raw.get("cityname"), str) else "",
            "citycode": raw.get("citycode") if isinstance(raw.get("citycode"), str) else "",
            "adcode": raw.get("adcode") if isinstance(raw.get("adcode"), str) else "",
            "longitude": lon,
            "latitude": lat,
            "type": raw.get("type") if isinstance(raw.get("type"), str) else "",
            "source": "高德地点搜索",
            "queried_at": datetime.now(UTC).isoformat(),
            "description": raw.get("type", "地点信息"),
            "tips": "开放时间、预约与门票请通过官方渠道核实。",
            "cost": None,
        }
        self.places[place["id"]] = place
        return place

    def read_places(self, data: dict) -> list[dict]:
        raw_places = data.get("pois", [])
        if not isinstance(raw_places, list):
            raise MapError("地图地点列表格式异常，请稍后重试")
        return [place for raw in raw_places if (place := self.normalize(raw))]

    async def search(self, query: str, city: str, types: str = "", limit: int = 8) -> list[dict]:
        data = await self.request(
            "/v3/place/text",
            {
                "keywords": query,
                "city": city,
                "citylimit": "true",
                "types": types,
                "offset": limit,
                "page": 1,
                "extensions": "base",
            },
        )
        return self.read_places(data)

    async def nearby(self, center: dict) -> list[dict]:
        data = await self.request(
            "/v3/place/around",
            {
                "location": f"{center['longitude']},{center['latitude']}",
                "types": "050000",
                "radius": 800,
                "offset": 3,
                "sortrule": "distance",
            },
        )
        return self.read_places(data)

    async def resolve(self, query: str, city: str, near: dict | None = None) -> dict:
        matches = await self.search(query, city, limit=5)
        if not matches:
            raise MapError(f"未找到「{query}」，请补充更完整的地点名称或地址")
        exact = [p for p in matches if p["name"] == query]
        pool = exact or matches
        selected = min(pool, key=lambda p: distance(p, near)) if near else pool[0]
        return selected

    async def route(self, a: dict, b: dict, city: str, mode: str) -> dict:
        key = (a["id"], b["id"], mode)
        if key in self.cache:
            return self.cache[key]
        if a["id"] == b["id"]:
            result = {
                "origin_id": a["id"],
                "destination_id": b["id"],
                "duration_minutes": 0,
                "distance_meters": 0,
                "mode": "同一地点",
                "source": "同一POI",
                "queried_at": datetime.now(UTC).isoformat(),
            }
            self.cache[key] = result
            return result
        params = {
            "origin": f"{a['longitude']},{a['latitude']}",
            "destination": f"{b['longitude']},{b['latitude']}",
        }
        walk = await self.request("/v3/direction/walking", params)

        def read_routes(body, field):
            route_data = body.get("route")
            if not isinstance(route_data, dict):
                raise MapError("地图路线结构异常，无法校验交通衔接")
            candidates = route_data.get(field, [])
            if not isinstance(candidates, list):
                raise MapError("地图路线列表格式异常，无法校验交通衔接")
            valid = []
            for item in candidates:
                try:
                    seconds, meters = float(item["duration"]), float(item["distance"])
                    if not (math.isfinite(seconds) and math.isfinite(meters)):
                        raise ValueError("non-finite route")
                    if seconds < 0 or meters < 0:
                        raise ValueError("negative route")
                    valid.append((seconds, meters))
                except KeyError, ValueError, TypeError:
                    continue
            if candidates and not valid:
                raise MapError("地图路线缺少有效耗时或距离，无法校验衔接")
            return valid

        paths = read_routes(walk, "paths")
        best, transport = min(paths) if paths else None, "步行"
        if mode == "mixed" and (best is None or best[0] > 1800):
            transit = await self.request(
                "/v3/direction/transit/integrated",
                {**params, "city": city, "cityd": city, "extensions": "base"},
            )
            alternatives = read_routes(transit, "transits")
            if alternatives:
                candidate = min(alternatives)
                if best is None or candidate[0] < best[0]:
                    best, transport = candidate, "公共交通"
        if best is None:
            raise MapError(f"未取得「{a['name']}」至「{b['name']}」的可用路线，不能据此安排交通")
        duration, meters = math.ceil(best[0] / 60), round(best[1])
        result = {
            "origin_id": a["id"],
            "destination_id": b["id"],
            "duration_minutes": duration,
            "distance_meters": meters,
            "mode": transport,
            "source": "高德路线查询（估算）",
            "queried_at": datetime.now(UTC).isoformat(),
        }
        self.cache[key] = result
        return result
