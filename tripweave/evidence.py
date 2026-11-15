"""Small, version-bound evidence passed from trusted picker drafts to planning.

Only server-owned v3 drafts may be frozen. URLs are references, never fetched by
this module. The handoff is useful context, not proof of current opening hours.
"""

from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EVIDENCE_VERSION = 1
PICKER_VERSION = 3
MAX_SELECTED = 5
MAX_KNOWN_PLACES = 65  # 60 recommendations plus at most 5 retained user choices.

PlaceID = Annotated[str, Field(min_length=1, max_length=64)]
PositiveRevision = Annotated[int, Field(strict=True, gt=0)]


def _identity_text(value, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("选景证据的地点或城市标识无效")
    if any(ord(char) < 32 for char in value):
        raise ValueError("选景证据的地点或城市标识无效")
    return value


def _place_id(value) -> str:
    value = _identity_text(value, limit=64)
    if any(char.isspace() for char in value):
        raise ValueError("选景证据的地点标识无效")
    return value


def _revision(value) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("选景证据修订号须为正整数")
    return value


def _queried_at(value) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("选景证据查询时间无效")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError
    except TypeError, ValueError, OverflowError:
        raise ValueError("选景证据查询时间无效") from None
    return value


def _source_url(value) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("选景证据来源链接无效")
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("选景证据来源链接无效")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError
    except ValueError:
        raise ValueError("选景证据来源链接无效") from None
    return value


class _EvidenceDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EvidenceSource(_EvidenceDTO):
    title: str = Field(min_length=1, max_length=160)
    url: str = Field(min_length=1, max_length=2048)

    @field_validator("title")
    @classmethod
    def valid_title(cls, value):
        return _identity_text(value, limit=160)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        return _source_url(value)


class SelectedEvidence(_EvidenceDTO):
    place_id: PlaceID
    name: str = Field(min_length=1, max_length=200)
    summary: str = Field(max_length=160)
    audience: str = Field(max_length=80)
    reason: str = Field(max_length=100)
    sources: list[EvidenceSource] = Field(max_length=3)
    queried_at: str = Field(max_length=64)

    @field_validator("place_id")
    @classmethod
    def valid_id(cls, value):
        return _place_id(value)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        return _identity_text(value, limit=200)

    @field_validator("queried_at")
    @classmethod
    def valid_time(cls, value):
        return _queried_at(value)

    @model_validator(mode="after")
    def grounded_description(self):
        if (self.summary or self.audience) and not self.sources:
            raise ValueError("没有对应来源的景点介绍不能作为可靠证据")
        if len({source.url for source in self.sources}) != len(self.sources):
            raise ValueError("选景证据来源重复")
        return self


class ExperienceGroup(_EvidenceDTO):
    group_id: PlaceID
    members: list[PlaceID] = Field(min_length=1, max_length=MAX_KNOWN_PLACES)

    @field_validator("group_id")
    @classmethod
    def valid_group_id(cls, value):
        return _place_id(value)

    @field_validator("members")
    @classmethod
    def valid_members(cls, values):
        return [_place_id(value) for value in values]

    @model_validator(mode="after")
    def contains_subject_once(self):
        if self.group_id not in self.members or len(set(self.members)) != len(self.members):
            raise ValueError("选景证据的体验组成员无效")
        return self


class PlanningEvidence(_EvidenceDTO):
    version: Literal[1]
    city: str = Field(min_length=1, max_length=40)
    source_requirements_revision: PositiveRevision
    selection_revision: PositiveRevision
    requirements_revision: PositiveRevision
    selected: list[SelectedEvidence] = Field(max_length=MAX_SELECTED)
    experience_groups: list[ExperienceGroup] = Field(max_length=MAX_KNOWN_PLACES)

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != EVIDENCE_VERSION:
            raise ValueError("选景证据版本无效")
        return value

    @field_validator("city")
    @classmethod
    def valid_city(cls, value):
        return _identity_text(value, limit=40)

    @model_validator(mode="after")
    def consistent_groups_and_choices(self):
        group_ids = set()
        membership = {}
        for group in self.experience_groups:
            if group.group_id in group_ids:
                raise ValueError("选景证据的体验组重复")
            group_ids.add(group.group_id)
            for member in group.members:
                if member in membership:
                    raise ValueError("选景证据的成员不能跨体验组")
                membership[member] = group.group_id
        if len(membership) > MAX_KNOWN_PLACES:
            raise ValueError("选景证据的地点过多")
        ids, chosen_groups = set(), set()
        for place in self.selected:
            group = membership.get(place.place_id)
            if place.place_id in ids or group is None or group in chosen_groups:
                raise ValueError("选景证据缺少地点分组或选择了重复游玩内容")
            ids.add(place.place_id)
            chosen_groups.add(group)
        return self


def _short_text(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _sources(value) -> list[dict]:
    sources = []
    seen = set()
    for source in value[:3] if isinstance(value, list) else []:
        if not isinstance(source, dict):
            continue
        title = _short_text(source.get("title"), 160)
        if not title:
            continue
        try:
            url = _source_url(source.get("url"))
            item = EvidenceSource(title=title, url=url).model_dump()
        except ValueError:
            continue
        if url not in seen:
            sources.append(item)
            seen.add(url)
    return sources


def freeze_evidence(
    draft: dict, *, city: str, source_requirements_revision: int, requirements_revision: int
) -> dict:
    """Project a trusted v3 server draft; reject broken grouping or stale metadata."""
    if not isinstance(draft, dict) or type(draft.get("picker_version")) is not int:
        raise ValueError("需要当前版本的服务端选景草稿")
    if draft["picker_version"] != PICKER_VERSION:
        raise ValueError("需要当前版本的服务端选景草稿")
    _identity_text(city, limit=40)
    source_revision = _revision(source_requirements_revision)
    target_revision = _revision(requirements_revision)
    selection_revision = _revision(draft.get("selection_revision"))
    draft_time = _queried_at(draft.get("queried_at"))
    selected_ids = draft.get("selected_ids")
    pool, retained = draft.get("candidate_pool"), draft.get("selected_items", [])
    if (
        not isinstance(selected_ids, list)
        or len(selected_ids) > MAX_SELECTED
        or not isinstance(pool, list)
        or len(pool) > MAX_KNOWN_PLACES
        or not isinstance(retained, list)
        or len(retained) > MAX_SELECTED
    ):
        raise ValueError("选景草稿的地点集合无效")
    selected_ids = [_place_id(place_id) for place_id in selected_ids]
    if len(set(selected_ids)) != len(selected_ids):
        raise ValueError("选景草稿包含重复选择")
    known, groups = {}, {}
    for record in pool + retained:
        if not isinstance(record, dict):
            raise ValueError("选景草稿的地点记录无效")  # noqa: TRY004 - public freeze contract
        place_id = _place_id(record.get("id"))
        name = _identity_text(record.get("name"), limit=200)
        group_id = _place_id(record.get("experience_group"))
        raw_members = record.get("experience_members")
        if not isinstance(raw_members, list) or not 1 <= len(raw_members) <= MAX_KNOWN_PLACES:
            raise ValueError("选景草稿缺少可信的体验组成员")
        members = list(dict.fromkeys(_place_id(member) for member in raw_members))
        if place_id not in members or group_id not in members:
            raise ValueError("选景草稿的地点或主体不属于其体验组")
        if group_id in groups and set(groups[group_id]) != set(members):
            raise ValueError("选景草稿同一体验组的成员不一致")
        groups.setdefault(group_id, members)
        if place_id in known:
            if known[place_id]["name"] != name:
                raise ValueError("选景草稿同一地点的名称不一致")
            if known[place_id]["experience_group"] != group_id:
                raise ValueError("选景草稿同一地点的分组不一致")
            # The pool normally has newer research; only fill absent fields.
            known[place_id] = {**record, **known[place_id]}
        else:
            known[place_id] = dict(record)
    selected = []
    for place_id in selected_ids:
        if place_id not in known:
            raise ValueError("选中的地点不在服务端选景草稿中")
        place = known[place_id]
        sources = _sources(place.get("web_sources"))
        selected.append(
            {
                "place_id": place_id,
                "name": place["name"],
                "summary": _short_text(place.get("web_summary"), 160) if sources else "",
                "audience": _short_text(place.get("audience"), 80) if sources else "",
                "reason": _short_text(place.get("recommendation_reason"), 100),
                "sources": sources,
                "queried_at": _queried_at(place.get("queried_at", draft_time)),
            }
        )
    return PlanningEvidence.model_validate(
        {
            "version": EVIDENCE_VERSION,
            "city": city,
            "source_requirements_revision": source_revision,
            "selection_revision": selection_revision,
            "requirements_revision": target_revision,
            "selected": selected,
            "experience_groups": [
                {"group_id": group_id, "members": members} for group_id, members in groups.items()
            ],
        }
    ).model_dump(mode="json")


def _matches_requirements(evidence: PlanningEvidence, requirements: dict) -> bool:
    if not isinstance(requirements, dict) or requirements.get("city") != evidence.city:
        return False
    selected = requirements.get("selected_attractions")
    if not isinstance(selected, list) or len(selected) > MAX_SELECTED:
        return False
    identity = set()
    for place in selected:
        if not isinstance(place, dict):
            return False
        place_id = _place_id(place.get("id"))
        name = _identity_text(place.get("name"), limit=200)
        if any(existing_id == place_id for existing_id, _ in identity):
            return False
        identity.add((place_id, name))
    return identity == {(place.place_id, place.name) for place in evidence.selected}


def rebind_evidence(
    body_or_none: dict | None, requirements_dict: dict, revision: int
) -> dict | None:
    """Carry evidence across a revision only when selected identities and city agree."""
    try:
        revision = _revision(revision)
        evidence = PlanningEvidence.model_validate(body_or_none)
        if not _matches_requirements(evidence, requirements_dict):
            return None
    except ValueError, TypeError:
        return None
    return {**evidence.model_dump(mode="json"), "requirements_revision": revision}


def load_evidence(body_or_none: dict | None, requirements_dict: dict, revision: int) -> dict | None:
    """Use only a complete snapshot bound to this exact requirements revision."""
    try:
        revision = _revision(revision)
        evidence = PlanningEvidence.model_validate(body_or_none)
        if evidence.requirements_revision != revision:
            return None
        if not _matches_requirements(evidence, requirements_dict):
            return None
    except ValueError, TypeError:
        return None
    return evidence.model_dump(mode="json")
