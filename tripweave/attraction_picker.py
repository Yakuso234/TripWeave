"""Preference-aware batches with a bounded, persisted recommendation pool."""

import json
import re
from datetime import UTC, datetime

import httpx2
from pydantic import BaseModel, Field, ValidationError

from .attraction_research import DEEPSEEK_API, research_attractions
from .domain import Requirements
from .maps import MapClient
from .settings import Settings

BATCH_SIZE = 5
PICKER_VERSION = 3
MAX_POOL_SIZE = 60


class AttractionRankingError(Exception):
    """Do not expose ungrouped map results as deduplicated recommendations."""


class RankedAttraction(BaseModel):
    place_id: str
    reason: str = Field(max_length=100)
    experience_group: str = Field(min_length=1, max_length=80)


class AttractionRanking(BaseModel):
    attractions: list[RankedAttraction] = Field(max_length=MAX_POOL_SIZE)


async def discover_candidates(req: Requirements, maps: MapClient) -> list[dict]:
    custom = re.split(r"[、，,;；\n]+", req.custom_interests)
    themes = re.split(r"[、，,;；\n]+", req.interests)
    aliases = {
        "历史人文": "博物馆",
        "室内展览": "展览馆",
        "自然公园": "公园",
        "江景夜色": "风景名胜",
    }
    keywords = list(dict.fromkeys(term.strip() for term in custom + themes if term.strip()))[:3]
    keywords = list(dict.fromkeys([aliases.get(term, term) for term in keywords] + ["著名景点"]))
    groups = []
    for keyword in keywords:
        groups.append(await maps.search(keyword, req.city, "110000|140100", 20, detailed=True))
    candidates = {}
    for index in range(20):
        for group in groups:
            if index < len(group):
                place = group[index]
                candidates.setdefault(place["id"], place)
    return list(candidates.values())[:MAX_POOL_SIZE]


async def rank_candidates(
    req: Requirements, candidates: list[dict], settings: Settings, client: httpx2.AsyncClient
) -> tuple[list[dict], str | None]:
    if not candidates:
        return [], None
    if not settings.configured or settings.base_url.rstrip("/") not in {
        "https://api.deepseek.com",
        DEEPSEEK_API,
    }:
        raise AttractionRankingError("请先配置官方 DeepSeek API Key，才能整理和去重景点推荐。")
    # Photos stay out of the model context. POI ratings are evidence, not invented scores.
    places = [
        {key: place.get(key) for key in ("id", "name", "type", "area", "rating")}
        for place in candidates
    ]
    try:
        response = await client.post(
            f"{DEEPSEEK_API}/chat/completions",
            headers={"Authorization": f"Bearer {settings.api_key.get_secret_value()}"},
            json={
                "model": settings.model,
                "thinking": {"type": "disabled"},
                "max_tokens": 4800,
                "response_format": {"type": "json_object"},
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "为旅行选景排序。用户本次兴趣与自定义需求优先，再结合地图真实评分、城市代表性和知名度，"
                            "选出值得推荐的候选并按推荐顺序列出。每批展示5个，后续按同一排序继续展示。"
                            "不要为了凑数推荐无关地点、餐饮商铺、出入口或其他景点内部的小设施。"
                            "优先适合该用户的知名景点，结合已有知识定性判断代表性，知名度未知时不要断言。"
                            "未知评分保持未知。不同馆区可分别保留，重复名称下相同景点只保留一次。"
                            "同一景点与其组成建筑群、内部设施或别名，属于同一游玩体验。"
                            "为每项填写experience_group，必须引用输入中该主体的地点ID，同一体验使用相同ID。"
                            "同一体验的不同成员都可列出并排序，程序会只保留排序第一项。独立馆区可各用自己的ID。"
                            "用户已选地点也必须列出并分组，不能把已有选择遗漏或改成别的地点。"
                            "只选输入ID，每个推荐理由约20字。不要研究预约、票价或营业时间。"
                            '输出 JSON {"attractions":[{"place_id":"ID","reason":"推荐理由",'
                            '"experience_group":"输入中的主体地点ID"}]}。'
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "city": req.city,
                                "interests": req.interests,
                                "custom_interests": req.custom_interests,
                                "pace": req.pace,
                                "custom_pace": req.custom_pace,
                                "additional_requirements": req.notes,
                                "selected_ids": [place.id for place in req.selected_attractions],
                                "places": places,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
            },
        )
        response.raise_for_status()
        ranked = AttractionRanking.model_validate_json(
            response.json()["choices"][0]["message"]["content"]
        )
    except httpx2.HTTPError, ValueError, KeyError, IndexError, TypeError, ValidationError:
        raise AttractionRankingError(
            "景点推荐整理失败，暂时无法核对重复景点，请稍后重试。"
        ) from None
    known = {place["id"]: place for place in candidates}
    valid = [item for item in ranked.attractions if item.place_id in known]
    if ranked.attractions and not valid:
        raise AttractionRankingError("推荐地点无法与地图结果核对，请重新查询。")
    if not {place.id for place in req.selected_attractions} <= {item.place_id for item in valid}:
        raise AttractionRankingError("未能核对已有选择的游玩内容，已保留原草稿，请重新查询。")
    parents = {place_id: place_id for place_id in known}

    def root(place_id):
        while parents[place_id] != place_id:
            parents[place_id] = parents[parents[place_id]]
            place_id = parents[place_id]
        return place_id

    for item in valid:
        group = item.experience_group.strip()
        if group not in known:
            raise AttractionRankingError("景点主体分组无法核对，已保留原草稿，请重新查询。")
        parents[root(item.place_id)] = root(group)
    members = {}
    for place_id in known:
        members.setdefault(root(place_id), []).append(place_id)
    ordered = {}
    experiences = set()
    for item in valid:
        group = root(item.place_id)
        if group not in experiences:
            experiences.add(group)
            ordered.setdefault(
                item.place_id,
                {
                    **known[item.place_id],
                    "recommendation_reason": item.reason,
                    "experience_group": group,
                    "experience_members": members[group],
                },
            )
    return list(ordered.values()), None


async def make_batch(
    req: Requirements, draft: dict, settings: Settings, client: httpx2.AsyncClient
) -> dict:
    pool = [dict(place) for place in draft.get("candidate_pool", [])]
    seen = list(dict.fromkeys(draft.get("seen_ids", [])))
    seen_ids = set(seen)
    seen_groups = set(draft.get("seen_groups", []))
    for place in pool + draft.get("selected_items", []):
        if place["id"] in seen_ids or place["id"] in draft.get("selected_ids", []):
            seen_groups.add(place.get("experience_group", place["id"]))
    for place in pool:
        if set(place.get("experience_members", [])) & set(draft.get("selected_ids", [])):
            seen_groups.add(place["experience_group"])
    remaining = [
        place
        for place in pool
        if place["id"] not in seen_ids
        and place.get("experience_group", place["id"]) not in seen_groups
    ]
    batch = remaining[:BATCH_SIZE]
    notice = await research_attractions(req, batch, settings, client) if batch else None
    seen.extend(place["id"] for place in batch)
    seen_groups.update(place.get("experience_group", place["id"]) for place in batch)
    enriched = {place["id"]: place for place in batch}
    pool = [enriched.get(place["id"], place) for place in pool]
    return {
        **draft,
        "picker_version": PICKER_VERSION,
        "candidate_pool": pool,
        "options": batch,
        "seen_ids": seen,
        "seen_groups": sorted(seen_groups),
        "batch_number": draft.get("batch_number", 0) + bool(batch),
        "exhausted": len(remaining) <= BATCH_SIZE,
        "remaining_count": max(0, len(remaining) - len(batch)),
        "web_notice": notice,
        "queried_at": datetime.now(UTC).isoformat(),
    }
