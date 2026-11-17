"""Amap Web Service adapter. Never emits the credential-bearing request URL."""

import asyncio
import math
from datetime import UTC, datetime
from unicodedata import normalize
from urllib.parse import urlsplit

import httpx2


class MapError(Exception):
    pass


def transport_allowed(preference: str, requested: str) -> bool:
    return requested == preference or preference == "mixed" and requested == "walking"


def cached_route(cache, origin_id, destination_id, mode):
    """A verified walking route also satisfies mixed travel, never the reverse."""
    exact = cache.get((origin_id, destination_id, mode))
    if exact is not None:
        return exact
    return cache.get((origin_id, destination_id, "walking")) if mode == "mixed" else None


def distance(a: dict, b: dict) -> float:
    return (float(a["longitude"]) - float(b["longitude"])) ** 2 + (
        float(a["latitude"]) - float(b["latitude"])
    ) ** 2


class MapClient:
    def __init__(
        self, client: httpx2.AsyncClient, key: str, max_calls: int = 50, *, interval: float = 0
    ):
        self.client, self.key, self.max_calls = client, key, max_calls
        self.calls = 0
        self.cache = {}
        self.places = {}
        self.interval = interval
        self.next_request_at = 0.0
        self.request_slot = asyncio.Lock()

    async def request(self, path: str, params: dict) -> dict:
        for attempt in range(2):
            async with self.request_slot:
                if self.calls >= self.max_calls:
                    raise MapError("本次地图请求已达到上限，请减少活动或重试")
                loop = asyncio.get_running_loop()
                delay = self.next_request_at - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                self.next_request_at = loop.time() + self.interval
                self.calls += 1
            try:
                response = await self.client.get(
                    "https://restapi.amap.com" + path,
                    params={**params, "key": self.key, "output": "JSON"},
                )
            except httpx2.DecodingError:
                raise MapError(f"高德 {path} 响应编码异常") from None
            except httpx2.RequestError as exc:
                if attempt == 0:
                    await asyncio.sleep(0.3)
                    continue
                if isinstance(exc, httpx2.TimeoutException):
                    reason = "请求超时"
                elif isinstance(exc, httpx2.ConnectError):
                    reason = "无法建立连接，请检查服务进程的网络"
                else:
                    reason = "网络请求失败"
                raise MapError(f"高德 {path} {reason}（{type(exc).__name__}）") from None

            if response.status_code >= 500 and attempt == 0:
                await asyncio.sleep(0.3)
                continue
            if response.status_code != 200:
                raise MapError(f"高德 {path} 返回 HTTP {response.status_code}")
            try:
                body = response.json()
            except ValueError:
                raise MapError(f"高德 {path} 响应不是有效 JSON") from None
            if not isinstance(body, dict):
                raise MapError(f"高德 {path} 响应格式异常，JSON 根节点应为对象")
            status = body.get("status")
            if status not in ("0", "1"):
                raise MapError(f"高德 {path} 响应格式异常，status 缺失或无效")
            if status == "0":
                code = str(body.get("infocode", "unknown"))
                code = code if code.isascii() and code.isdigit() and len(code) <= 8 else "unknown"
                if code == "10003":
                    raise MapError(f"高德 {path} 调用额度不足（{code}）")
                if code in {"10014", "10019", "10020", "10021"}:
                    if attempt == 0:
                        await asyncio.sleep(1.1)
                        continue
                    raise MapError(f"高德 {path} 每秒请求数超限（{code}），请稍后重试")
                raise MapError(f"高德 {path} 请求失败，错误码 {code}。请检查 Web 服务 Key 与权限")
            return body
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
        photos = raw.get("photos")
        place["photos"] = []
        if isinstance(photos, list):
            for photo in photos:
                if not isinstance(photo, dict) or not isinstance(photo.get("url"), str):
                    continue
                url = photo["url"].strip()
                try:
                    parsed = urlsplit(url)
                    valid = (
                        parsed.scheme in {"http", "https"}
                        and parsed.hostname
                        and parsed.username is None
                        and parsed.password is None
                        and not any(ch.isspace() or ord(ch) < 32 for ch in url)
                    )
                except ValueError:
                    valid = False
                if valid and url not in place["photos"]:
                    place["photos"].append(url)
                if len(place["photos"]) == 5:
                    break
        details = raw.get("biz_ext")
        try:
            rating = float(details["rating"]) if isinstance(details, dict) else None
        except ValueError, TypeError, KeyError:
            rating = None
        place["rating"] = rating if rating is not None and 0 <= rating <= 5 else None
        self.places[place["id"]] = place
        return place

    def read_places(self, data: dict, *, path: str) -> list[dict]:
        raw_places = data.get("pois")
        if not isinstance(raw_places, list):
            raise MapError(f"高德 {path} 响应格式异常，pois 缺失或不是列表")
        places = [place for raw in raw_places if (place := self.normalize(raw))]
        if raw_places and not places:
            raise MapError(f"高德 {path} 的 pois 列表没有包含有效 ID、名称和坐标的地点")
        return places

    async def search(
        self, query: str, city: str, types: str = "", limit: int = 8, *, detailed: bool = False
    ) -> list[dict]:
        path = "/v3/place/text"
        data = await self.request(
            path,
            {
                "keywords": query,
                "city": city,
                "citylimit": "true",
                "types": types,
                "offset": limit,
                "page": 1,
                "extensions": "all" if detailed else "base",
            },
        )
        return self.read_places(data, path=path)

    async def nearby(self, center: dict) -> list[dict]:
        path = "/v3/place/around"
        data = await self.request(
            path,
            {
                "location": f"{center['longitude']},{center['latitude']}",
                "types": "050000",
                "radius": 800,
                "offset": 3,
                "sortrule": "distance",
            },
        )
        return self.read_places(data, path=path)

    async def resolve(self, query: str, city: str, *, label: str = "地点") -> dict:
        name = normalize("NFKC", query).strip().casefold()
        matches = await self.search(query.strip(), city, limit=5)
        exact = {
            place["id"]: place
            for place in matches
            if normalize("NFKC", place["name"]).strip().casefold() == name
        }
        if not exact:
            raise MapError(
                f"高德未找到匹配的{label}「{query}」。请检查是否输错，或填写高德上的完整名称。"
            )
        if len(exact) > 1:
            raise MapError(f"高德找到多个同名{label}「{query}」。请填写含分馆或分店的完整名称。")
        return next(iter(exact.values()))

    async def route(self, a: dict, b: dict, city: str, mode: str) -> dict:
        key = (a["id"], b["id"], mode)
        known = cached_route(self.cache, a["id"], b["id"], mode)
        if known is not None:
            return known
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
        walk_path = "/v3/direction/walking"
        walk = await self.request(walk_path, params)

        def read_routes(body, field, path):
            route_data = body.get("route")
            if not isinstance(route_data, dict):
                raise MapError(f"高德 {path} 响应格式异常，route 缺失或不是对象")
            candidates = route_data.get(field)
            if not isinstance(candidates, list):
                raise MapError(f"高德 {path} 响应格式异常，route.{field} 缺失或不是列表")
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
                raise MapError(f"高德 {path} 的 route.{field} 缺少有效耗时或距离")
            return valid

        paths = read_routes(walk, "paths", walk_path)
        best, transport = min(paths) if paths else None, "步行"
        if mode == "mixed" and (best is None or best[0] > 1800):
            transit_path = "/v3/direction/transit/integrated"
            transit = await self.request(
                transit_path,
                {**params, "city": city, "cityd": city, "extensions": "base"},
            )
            alternatives = read_routes(transit, "transits", transit_path)
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
