"""Evidence tolerance and bounded recovery, exclusively synthetic HTTP responses."""

import json

import httpx2
import pytest
from draft_helpers import packet
from pydantic_ai.usage import RunUsage
from test_planning_research import QUOTE, completion, fact, final, need, run_research, sources
from test_planning_workflow import gap, itinerary, map_response, requirements, tool_names
from test_real_planner import configured
from test_research_planner_context import completion as planner_completion

from tripweave.planning_contracts import ResearchFact
from tripweave.planning_research import _unknown
from tripweave.real_planner import run_plan
from tripweave.research_evidence import match_quote, validate_evidence
from tripweave.storage import Store


@pytest.mark.parametrize(
    "dates,accepted",
    [
        ("2026年11月1日至2026年11月30日", True),
        ("2026-11-01—2026-11-30", True),
        ("2026-11-01 ~ 2026-11-30", True),
        ("2026年10月1日至2026年10月30日", False),
        ("2026年11月1日、2026年11月30日", False),
        ("2026年11月31日至2026年12月30日", False),
    ],
)
def test_quote_date_range_contains_trip_date_without_accepting_unrelated_dates(dates, accepted):
    quote = dates + "：开放时间09:00–17:00。"
    facts, diagnostics = evidence([fact(quote=quote)], excerpt=quote)
    assert (facts[0]["status"] == "found") == accepted
    assert (
        not any(d["code"] == "explicit_date_mismatch" for d in diagnostics)
        if accepted
        else any(d["code"] == "explicit_date_mismatch" for d in diagnostics)
    )


def evidence(proposals, *, excerpt=QUOTE, extra_sources=()):
    return validate_evidence(
        [_unknown("p0", 1, "opening_hours", requirements())],
        [ResearchFact.model_validate(p) for p in proposals],
        {
            ("p0", 1): {
                "error": None,
                "queried_at": "2026-11-15T00:00:00Z",
                "sources": [
                    {
                        "id": 1,
                        "url": "https://museum.example/visit",
                        "title": "参观说明",
                        "excerpt": excerpt,
                    },
                    *extra_sources,
                ],
            }
        },
    )


def test_whitespace_matching_preserves_actual_source_wording():
    excerpt = QUOTE.replace("开放时间", "开放\n 时间 ").replace("，", "，\n")
    facts, diagnostics = evidence([fact()], excerpt=excerpt)
    assert not diagnostics
    assert facts[0]["quote"] == excerpt
    assert facts[0]["citation_match"] == "whitespace"
    assert facts[0]["windows"][0]["start_time"] == "09:00"


@pytest.mark.parametrize(
    "quote",
    [QUOTE.replace("17:00", "18:00"), QUOTE.replace("开放", "不开放"), QUOTE.replace("，", "。")],
)
def test_tolerance_does_not_accept_changed_numbers_negation_or_punctuation(quote):
    facts, diagnostics = evidence([fact(quote=quote)])
    assert facts[0]["status"] == "unknown"
    assert diagnostics[0]["code"] == "quote_not_in_source"


def test_extra_incorrect_references_do_not_discard_a_supported_fact():
    extra = {
        "id": 2,
        "url": "https://museum.example/other",
        "title": "展品介绍",
        "excerpt": "只有展品介绍。",
    }
    facts, diagnostics = evidence([fact(source_ids=[1, 2, 3])], extra_sources=[extra])
    assert facts[0]["status"] == "found" and facts[0]["windows"]
    assert facts[0]["sources"] == [{"title": "参观说明", "url": "https://museum.example/visit"}]
    assert {d["code"] for d in diagnostics} == {
        "extra_source_id_ignored",
        "unsupported_reference_ignored",
    }
    assert all(d["severity"] == "warning" for d in diagnostics)


def test_concatenating_separate_passages_is_not_whitespace_tolerance():
    assert match_quote("开放09:00至17:00。", "开放09:00，有其他重要说明，至17:00。") is None


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"source_ids": [3]}, "source_id_invalid"),
        ({"windows": []}, "opening_structure_missing"),
        (
            {"windows": [{"day": 1, "start_time": "10:00", "end_time": "17:00"}]},
            "opening_time_not_in_quote",
        ),
    ],
)
def test_diagnostics_locate_field_and_actual_rejection(changes, code):
    _, diagnostics = evidence([fact(**changes)])
    d = diagnostics[0]
    assert (d["place_id"], d["day"], d["field"]) == ("p0", 1, "opening_hours")
    assert d["origin"] == "program" and d["code"] == code


def test_model_unknown_reason_is_an_attributed_claim_not_program_proof():
    _, diagnostics = evidence(
        [
            {
                "place_id": "p0",
                "day": 1,
                "field": "opening_hours",
                "status": "unknown",
                "unknown_reason": "excerpt_insufficient",
                "unknown_detail": "返回的摘要没有完整开放窗口。",
            }
        ]
    )
    assert diagnostics[0]["origin"] == "model"
    assert diagnostics[0]["code"] == "excerpt_insufficient"


async def test_reextract_only_bad_field_without_researching_or_replacing_accepted_fact():
    calls, searches = [], []
    snapshots = []

    def provider(request):
        if request.url.path.endswith("/tools/search"):
            searches.append(json.loads(request.content))
            return sources()
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return completion([("search_place_information", {"place_id": "p0", "day": 1})])
        if len(calls) == 2:
            return final(
                body, [fact(), fact(field="last_admission", windows=[], last_admission="18:00")]
            )
        prompt = json.loads(body["messages"][-1]["content"])
        assert snapshots[0]["facts"][0]["status"] == "found"
        assert snapshots[0]["diagnostics"][0]["code"] == "admission_time_not_in_quote"
        assert [f["field"] for f in prompt["recovery_tasks"]] == ["last_admission"]
        assert prompt["validation_feedback"][0]["code"] == "admission_time_not_in_quote"
        return final(
            body,
            [
                fact(field="last_admission", windows=[], last_admission="16:00"),
                fact(windows=[{"day": 1, "start_time": "10:00", "end_time": "11:00"}]),
            ],
        )

    result, usage, _ = await run_research(
        provider,
        [need(fields=["opening_hours", "last_admission"])],
        on_progress=lambda facts, diagnostics: snapshots.append(
            json.loads(json.dumps({"facts": facts, "diagnostics": diagnostics}))
        ),
    )
    assert result["outcome"] == "complete" and len(searches) == 1 and usage.requests == 3
    assert result["facts"][0]["windows"][0]["start_time"] == "09:00"
    assert result["facts"][1]["last_admission"] == "16:00"
    assert result["diagnostics"][0]["resolved"] is True


async def test_empty_search_uses_changed_query_once_and_recovers_from_new_sources():
    calls, searches = [], []

    def provider(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/tools/search"):
            searches.append(body)
            return httpx2.Response(200, json={"results": []}) if len(searches) == 1 else sources()
        calls.append(body)
        if len(calls) == 1:
            return completion([("search_place_information", {"place_id": "p0", "day": 1})])
        if len(calls) == 2:
            return final(
                body, [{"place_id": "p0", "day": 1, "field": "opening_hours", "status": "unknown"}]
            )
        assert json.loads(body["messages"][-1]["content"])["recovery_tasks"][0]["sources"]
        return final(body, [fact()])

    result, usage, _ = await run_research(provider)
    assert result["outcome"] == "complete" and usage.requests == 3 and result["search_calls"] == 2
    assert searches[0]["query"] != searches[1]["query"]
    assert "参观须知" in searches[1]["query"]


async def test_bad_extraction_with_insufficient_budget_is_incomplete_and_keeps_good_field():
    calls = []

    def provider(request):
        if request.url.path.endswith("/tools/search"):
            return sources()
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return completion([("search_place_information", {"place_id": "p0", "day": 1})])
        return final(
            body, [fact(), fact(field="last_admission", windows=[], last_admission="18:00")]
        )

    result, usage, _ = await run_research(
        provider, [need(fields=["opening_hours", "last_admission"])], usage=RunUsage(requests=3)
    )
    assert result["outcome"] == "incomplete" and usage.requests == 5 and len(calls) == 2
    assert result["facts"][0]["status"] == "found"
    assert any(d["code"] == "recovery_budget_insufficient" for d in result["diagnostics"])


async def test_recovery_respects_total_search_cap_and_does_not_reset_it():
    calls, searches = [], []
    pairs = [(pid, day) for pid in ("p0", "p1") for day in (1, 2)]

    def provider(request):
        if request.url.path.endswith("/tools/search"):
            searches.append(request)
            return httpx2.Response(200, json={"results": []})
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return completion(
                [("search_place_information", {"place_id": pid, "day": day}) for pid, day in pairs]
            )
        assert len(calls) == 2
        return final(body, [])

    result, usage, _ = await run_research(
        provider, [need(place_id=pid, day=day) for pid, day in pairs]
    )
    assert result["outcome"] == "incomplete" and usage.requests == 2
    assert result["search_calls"] == len(searches) == 4
    assert any(d["code"] == "search_budget_exhausted" for d in result["diagnostics"])


async def test_transient_search_timeout_can_recover_without_claiming_data_is_absent():
    calls, searches = [], []

    def provider(request):
        if request.url.path.endswith("/tools/search"):
            searches.append(request)
            if len(searches) == 1:
                raise httpx2.ReadTimeout("PRIVATE-EXCEPTION-TEXT", request=request)
            return sources()
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return completion([("search_place_information", {"place_id": "p0", "day": 1})])
        return final(body, [] if len(calls) == 2 else [fact()])

    result, usage, _ = await run_research(provider)
    assert result["outcome"] == "complete" and usage.requests == 3 and len(searches) == 2
    assert result["diagnostics"][0]["code"] == "search_timeout"
    assert result["diagnostics"][0]["resolved"] is True
    assert "PRIVATE-EXCEPTION-TEXT" not in json.dumps(result)


@pytest.mark.parametrize("required", [False, True])
async def test_unknown_returns_to_planner_and_cannot_be_hidden_for_required_place(
    required, tmp_path
):
    calls = []

    def provider(request):
        if request.url.host != "api.deepseek.com":
            return map_response(request)
        if request.url.path.endswith("/tools/search"):
            return sources()
        body = json.loads(request.content)
        calls.append(body)
        names = tool_names(body)
        if "review_report" in names:
            return planner_completion(
                "review_report", {"summary": "合成复核通过。", "suggestions": []}, len(calls)
            )
        if "research_report" in names:
            return final(
                body, [{"place_id": "p0", "day": 1, "field": "opening_hours", "status": "unknown"}]
            )
        if len(calls) == 1:
            return planner_completion(
                "draft_proposal", packet(itinerary(), needs=gap()["needs"]), len(calls)
            )
        prompt = json.loads(body["messages"][-1]["content"])
        assert prompt["research_feedback"][0]["code"] == "model_unknown_unspecified"
        plan = itinerary()
        if not required:
            plan["stops"][0]["place_id"] = "p1"
        queries = None if not required and len(calls) == 3 else []
        return planner_completion("draft_proposal", packet(plan, queries=queries), len(calls))

    req = requirements(selected_attractions=[{"id": "p0", "name": "接口景点0"}] if required else [])
    result = await run_plan(req, "deepseek", configured(), transport=httpx2.MockTransport(provider))
    assert result["outcome"] == ("needs_information" if required else "ready"), result
    if required:
        assert result["itinerary"] is None and result["draft"]["stops"][0]["place_id"] == "p0"
        assert result["issues"][0]["code"] == "critical_fact_date_missing"
    else:
        assert result["itinerary"]["stops"][0]["place_id"] == "p1"
    store = Store(str(tmp_path / "diagnostics.db"))
    store.initialize()
    trip = store.create_trip(req.model_dump(mode="json"))
    stored = {
        **result,
        "id": "diagnosed",
        "trip_id": trip["id"],
        "mode": "deepseek",
        "base_version": 0,
        "requirements_revision": 1,
    }
    store.add_candidate(stored)
    store.dispose()
    reopened = Store(str(tmp_path / "diagnostics.db"))
    assert (
        reopened.get_candidate(trip["id"], "diagnosed")["research_diagnostics"]
        == result["research_diagnostics"]
    )
    reopened.dispose()
