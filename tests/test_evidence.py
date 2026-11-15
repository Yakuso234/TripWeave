"""Selection-to-planning handoffs are local immutable projections, never network calls."""

import copy
import json

import pytest

from tripweave.evidence import freeze_evidence, load_evidence, rebind_evidence

QUERIED_AT = "2026-11-13T05:30:00+00:00"


def draft_fixture():
    def place(place_id, name, group_id, members):
        return {
            "id": place_id,
            "name": name,
            "experience_group": group_id,
            "experience_members": members,
            "web_summary": f"{name}的游玩介绍",
            "audience": "喜欢历史与建筑的人",
            "recommendation_reason": "符合用户的人文兴趣",
            "web_sources": [
                {
                    "id": 1,
                    "title": f"{name}介绍",
                    "url": f"https://official.example/{place_id}",
                    "excerpt": "原始网页片段不进入交接上下文",
                }
            ],
            "photos": [f"https://photos.example/{place_id}.jpg"],
        }

    return {
        "picker_version": 3,
        "selection_revision": 4,
        "queried_at": QUERIED_AT,
        "selected_ids": ["garden-alias", "museum-east"],
        "candidate_pool": [
            place("garden", "园林主体", "garden", ["garden", "garden-alias"]),
            place("museum-east", "博物馆东馆", "museum-east", ["museum-east"]),
            place("museum-old", "博物馆老馆", "museum-old", ["museum-old"]),
        ],
        # The original chosen identity can differ from the group's ranked representative.
        "selected_items": [
            place("garden-alias", "用户选定的园林建筑群", "garden", ["garden-alias", "garden"])
        ],
    }


def freeze(draft=None, **overrides):
    return freeze_evidence(
        draft_fixture() if draft is None else draft,
        **{
            "city": "上海",
            "source_requirements_revision": 2,
            "requirements_revision": 3,
            **overrides,
        },
    )


def requirements_fixture():
    return {
        "city": "上海",
        "selected_attractions": [
            {"id": "garden-alias", "name": "用户选定的园林建筑群"},
            {"id": "museum-east", "name": "博物馆东馆"},
        ],
        "date": "2026-11-20",
        "notes": "希望慢游",
    }


def test_freezing_preserves_user_identity_and_only_projects_selected_grounded_text():
    draft = draft_fixture()
    original = copy.deepcopy(draft)
    snapshot = freeze(draft)
    assert snapshot["version"] == 1
    assert snapshot["source_requirements_revision"] == 2
    assert snapshot["selection_revision"] == 4
    assert snapshot["requirements_revision"] == 3
    assert [(place["place_id"], place["name"]) for place in snapshot["selected"]] == [
        ("garden-alias", "用户选定的园林建筑群"),
        ("museum-east", "博物馆东馆"),
    ]
    assert snapshot["selected"][0] == {
        "place_id": "garden-alias",
        "name": "用户选定的园林建筑群",
        "summary": "用户选定的园林建筑群的游玩介绍",
        "audience": "喜欢历史与建筑的人",
        "reason": "符合用户的人文兴趣",
        "sources": [
            {"title": "用户选定的园林建筑群介绍", "url": "https://official.example/garden-alias"}
        ],
        "queried_at": QUERIED_AT,
    }
    assert {group["group_id"] for group in snapshot["experience_groups"]} == {
        "garden",
        "museum-east",
        "museum-old",
    }
    assert set(snapshot["experience_groups"][0]["members"]) == {"garden", "garden-alias"}
    encoded = json.dumps(snapshot, ensure_ascii=False)
    assert all(
        text not in encoded for text in ["photos.example", "excerpt", "原始网页", "老馆的游玩介绍"]
    )
    assert draft == original


def test_without_matching_sources_descriptions_are_cleared_instead_of_becoming_evidence():
    draft = draft_fixture()
    draft["selected_items"][0]["web_sources"] = []
    # Do not borrow the representative's sourced text for a different selected ID.
    original = copy.deepcopy(draft)
    snapshot = freeze(draft)
    selected = snapshot["selected"][0]
    assert selected["place_id"] == "garden-alias"
    assert selected["summary"] == selected["audience"] == ""
    assert selected["sources"] == []
    assert selected["reason"] == "符合用户的人文兴趣"
    assert snapshot["selected"][1]["summary"]
    assert draft == original


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///C:/private.txt",
        "https://user:secret@example.com/page",
        "https://:secret@example.com/page",
        "https://example.com/a b",
        "https://example.com/a\nb",
        "//example.com/page",
        "https:///missing-host",
        "https://[invalid",
        "https://example.com/" + "x" * 2048,
    ],
)
def test_unsafe_source_urls_never_make_descriptions_look_verified(url):
    draft = draft_fixture()
    draft["selected_items"][0]["web_sources"] = [{"title": "来源", "url": url}]
    selected = freeze(draft)["selected"][0]
    assert selected["sources"] == []
    assert selected["summary"] == selected["audience"] == ""


def test_sources_and_members_are_deduplicated_and_model_text_is_bounded():
    draft = draft_fixture()
    selected = draft["selected_items"][0]
    selected["web_summary"] = "  " + "介" * 200 + "  "
    selected["audience"] = "  " + "人" * 100 + "  "
    selected["recommendation_reason"] = "理" * 130
    selected["web_sources"] = [
        {"title": "源" * 200, "url": "http://official.example/intro", "excerpt": "discard"},
        {"title": "重复来源", "url": "http://official.example/intro"},
        {"title": "第二来源", "url": "https://official.example/intro"},
    ]
    selected["experience_members"].append("garden-alias")
    snapshot = freeze(draft)
    selected = snapshot["selected"][0]
    assert len(selected["summary"]) == 160
    assert len(selected["audience"]) == 80
    assert len(selected["reason"]) == 100
    assert len(selected["sources"]) == 2
    assert len(selected["sources"][0]["title"]) == 160
    assert len(snapshot["experience_groups"][0]["members"]) == 2


@pytest.mark.parametrize("field", ["source_requirements_revision", "requirements_revision"])
@pytest.mark.parametrize("revision", [0, -1, True, "3", 2.5, None])
def test_freeze_requires_positive_integer_versions(field, revision):
    with pytest.raises(ValueError):
        freeze(**{field: revision})


@pytest.mark.parametrize("revision", [0, -1, False, "4", None])
def test_freeze_requires_positive_integer_selection_revision(revision):
    draft = draft_fixture()
    draft["selection_revision"] = revision
    with pytest.raises(ValueError):
        freeze(draft)


@pytest.mark.parametrize("timestamp", [None, "", "yesterday", "2026-11-13", "2026-11-13T05:30:00"])
def test_freeze_rejects_missing_or_ambiguous_time_metadata(timestamp):
    draft = draft_fixture()
    draft["queried_at"] = timestamp
    with pytest.raises(ValueError):
        freeze(draft)


def test_selected_place_time_is_preserved_when_available():
    draft = draft_fixture()
    earlier = "2026-11-12T09:30:00+08:00"
    draft["selected_items"][0]["queried_at"] = earlier
    assert freeze(draft)["selected"][0]["queried_at"] == earlier


@pytest.mark.parametrize(
    "change",
    [
        lambda draft: draft.update(picker_version=2),
        lambda draft: draft.update(picker_version="3"),
        lambda draft: draft.update(selected_ids=["not-recorded"]),
        lambda draft: draft.update(selected_ids=["garden-alias", "garden"]),
        lambda draft: draft.update(selected_ids=["garden-alias", "garden-alias"]),
        lambda draft: draft.update(selected_ids=[f"place{i}" for i in range(6)]),
        lambda draft: draft["candidate_pool"][0].update(experience_members=["garden-alias"]),
        lambda draft: draft["candidate_pool"][0].update(experience_group="unknown-subject"),
        lambda draft: draft["candidate_pool"][1].update(
            experience_members=["museum-east", "garden-alias"]
        ),
        lambda draft: draft["selected_items"][0].update(experience_members=["garden-alias"]),
        lambda draft: draft["selected_items"][0].update(name=""),
        lambda draft: draft["selected_items"][0].update(id=" "),
        lambda draft: draft["selected_items"][0].pop("experience_members"),
    ],
)
def test_freeze_rejects_missing_identity_duplicate_experiences_and_invalid_groups(change):
    draft = draft_fixture()
    change(draft)
    with pytest.raises(ValueError):
        freeze(draft)


def test_empty_choice_is_a_valid_explicit_selection():
    draft = draft_fixture()
    draft["selected_ids"] = []
    snapshot = freeze(draft)
    assert snapshot["selected"] == []
    assert load_evidence(snapshot, {"city": "上海", "selected_attractions": []}, 3) == snapshot


def test_load_and_rebind_survive_json_storage_without_mutating_the_original():
    frozen = freeze()
    original = copy.deepcopy(frozen)
    requirements = requirements_fixture()
    requirements["selected_attractions"].reverse()
    stored = json.loads(json.dumps(frozen))
    assert load_evidence(stored, requirements, 3) == frozen
    requirements.update(notes="新增一次午休", date="2026-11-21")
    rebound = rebind_evidence(stored, requirements, 4)
    assert rebound == {**frozen, "requirements_revision": 4}
    assert load_evidence(rebound, requirements, 4) == rebound
    assert load_evidence(rebound, requirements, 3) is None
    assert load_evidence(stored, requirements, 4) is None
    assert stored == frozen == original
    rebound["selected"][0]["sources"].clear()
    assert frozen == original


@pytest.mark.parametrize(
    "change",
    [
        lambda req: req.update(city="北京"),
        lambda req: req["selected_attractions"].pop(),
        lambda req: req["selected_attractions"][0].update(id="garden"),
        lambda req: req["selected_attractions"][0].update(name="改名后的地点"),
        lambda req: req["selected_attractions"].append(req["selected_attractions"][0]),
        lambda req: req.pop("selected_attractions"),
    ],
)
def test_identity_changes_invalidate_old_evidence_instead_of_rebinding_it(change):
    requirements = requirements_fixture()
    change(requirements)
    assert load_evidence(freeze(), requirements, 3) is None
    assert rebind_evidence(freeze(), requirements, 4) is None


@pytest.mark.parametrize(
    "change",
    [
        lambda body: body.update(version=2),
        lambda body: body.update(version=True),
        lambda body: body.update(source_requirements_revision=0),
        lambda body: body.update(selection_revision=True),
        lambda body: body.update(requirements_revision="3"),
        lambda body: body.update(experience_groups=[]),
        lambda body: body["experience_groups"][0]["members"].append("museum-east"),
        lambda body: body["experience_groups"][0].update(group_id="unknown"),
        lambda body: body["selected"][0].update(sources=[]),
        lambda body: body["selected"][0].update(photos=["https://photos.example/private.jpg"]),
        lambda body: body["selected"][0]["sources"][0].update(excerpt="unexpected raw material"),
        lambda body: body["selected"][0]["sources"][0].update(url="javascript:alert(1)"),
    ],
)
def test_malformed_stored_evidence_is_ignored_by_both_consumers(change):
    body = freeze()
    change(body)
    assert load_evidence(body, requirements_fixture(), 3) is None
    assert rebind_evidence(body, requirements_fixture(), 4) is None


@pytest.mark.parametrize("body", [None, {}, [], "old-data"])
def test_missing_or_legacy_evidence_is_not_used(body):
    assert load_evidence(body, requirements_fixture(), 3) is None
    assert rebind_evidence(body, requirements_fixture(), 4) is None


@pytest.mark.parametrize("revision", [0, True, "3", None])
def test_loading_and_rebinding_never_coerce_revision_types(revision):
    assert load_evidence(freeze(), requirements_fixture(), revision) is None
    assert rebind_evidence(freeze(), requirements_fixture(), revision) is None
