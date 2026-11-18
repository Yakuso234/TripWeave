"""Traceable evidence acceptance, not verification of real-world availability."""

import re
from collections import defaultdict
from datetime import date

FIELD_NAMES = {
    "opening_hours": "开放时段",
    "last_admission": "最晚入场时间",
    "reservation_rules": "预约规则",
    "reservation_channel": "预约入口",
    "reservation_requirement": "当天是否必须预约",
    "ticket_availability": "当日余票或可预约名额",
}


def usable_research_fact(fact):
    if fact["status"] != "found" or fact["field"] == "ticket_availability":
        return False
    if fact["field"] == "opening_hours":
        return bool(fact.get("windows") or fact.get("closed") is True)
    if fact["field"] == "last_admission":
        return bool(fact.get("last_admission"))
    return True


def date_in_explicit_range(target: str, text: str) -> bool:
    """Recognize directly joined full-date endpoints, not unrelated date lists."""
    full = r"(\d{4})(?:-|年)(\d{1,2})(?:-|月)(\d{1,2})(?:日)?"
    compact = "".join(ch for ch in text if not ch.isspace())
    for match in re.finditer(full + r"(?:至|到|[-~～—–－])" + full, compact):
        values = list(map(int, match.groups()))
        try:
            start, end = date(*values[:3]), date(*values[3:])
            if start <= date.fromisoformat(target) <= end:
                return True
        except ValueError:
            continue
    return False


def clock_in_quote(value: str, quote: str) -> bool:
    hour, minute = map(int, value.split(":"))
    for match in re.finditer(r"(?<!\d)(\d{1,2})[:：](\d{2})(?!\d)", quote):
        if (int(match[1]), int(match[2])) == (hour, minute):
            return True
    for match in re.finditer(r"(?<!\d)(\d{1,2})[点时](?:(\d{1,2})分?)?", quote):
        if (int(match[1]), int(match[2] or 0)) == (hour, minute):
            return True
    return False


def match_quote(quote: str, excerpt: str) -> tuple[str, str] | None:
    """Match a continuous span, permitting whitespace changes only.

    Keep a character-to-source offset map so the saved quote is actual source
    text. Punctuation, numbers, negation and ordering are never fuzzy-matched.
    Clock checks still use the original source span, not model-normalized text.
    """
    if quote in excerpt:
        return quote, "exact"
    needle = "".join(ch for ch in quote if not ch.isspace())
    offsets = [i for i, ch in enumerate(excerpt) if not ch.isspace()]
    haystack = "".join(excerpt[i] for i in offsets)
    start = haystack.find(needle) if needle else -1
    if start < 0:
        return None
    return excerpt[offsets[start] : offsets[start + len(needle) - 1] + 1], "whitespace"


def diagnostic(fact, stage, code, message, *, origin="program", severity="error", **details):
    return {
        **{key: fact[key] for key in ("place_id", "day", "date", "field")},
        "stage": stage,
        "code": code,
        "origin": origin,
        "severity": severity,
        "message": message,
        **details,
    }


def validate_evidence(empty_facts, proposals, searches):
    """Return accepted/partial facts and bounded, per-field diagnostics."""
    reported = defaultdict(list)
    requested = {(f["place_id"], f["day"], f["field"]) for f in empty_facts}
    diagnostics, accepted = [], []
    for fact in proposals:
        key = (fact.place_id, fact.day, fact.field)
        reported[key].append(fact)
    for empty in empty_facts:
        key = (empty["place_id"], empty["day"], empty["field"])
        search = searches.get(key[:2])
        matches = reported.get(key, [])

        def reject(stage, code, message, _fact=empty, **details):
            diagnostics.append(diagnostic(_fact, stage, code, message, **details))

        accepted.append(empty)
        if not search:
            reject(
                "search",
                "search_not_performed",
                "研究Agent未对该地点和日期执行搜索，不能采信模型记忆。",
            )
            continue
        if search["error"]:
            messages = {
                "search_http_error": "网页检索返回HTTP错误，未取得可采信资料。",
                "search_timeout": "网页检索超时，不能当作资料不存在。",
                "search_connection_error": "网页检索连接失败，不能当作资料不存在。",
                "search_response_format": "检索响应不是预期JSON或缺少results列表。",
                "search_sources_format": "返回非空条目，但未取得含有效网址、标题和摘要的来源。",
            }
            reject(
                "search",
                search["error"],
                messages.get(search["error"], "网页检索异常。"),
                http_status=search.get("http_status"),
            )
            continue
        if not search["sources"]:
            reject("search", "search_empty", "本次搜索未返回可用来源，不代表互联网上没有资料。")
            continue
        if len(matches) != 1:
            reject(
                "extraction",
                "field_missing" if not matches else "field_duplicated",
                "研究报告漏回该字段。"
                if not matches
                else "研究报告对同一字段返回多份结果，含义不明确。",
                expected=1,
                actual=len(matches),
            )
            continue
        fact = matches[0]
        if fact.status == "unknown":
            reject(
                "extraction",
                fact.unknown_reason or "model_unknown_unspecified",
                fact.unknown_detail
                or "研究Agent返回未知，未说明具体原因；不能据此判断来源没有该信息。",
                origin="model",
                source_count=len(search["sources"]),
            )
            continue
        by_id = {source["id"]: source for source in search["sources"]}
        invalid = [sid for sid in fact.source_ids if sid not in by_id]
        valid_ids = [sid for sid in fact.source_ids if sid in by_id]
        if not valid_ids:
            reject(
                "evidence",
                "source_id_invalid",
                "引用的来源编号不属于该地点、日期的实际搜索结果。",
                expected=sorted(by_id),
                actual=fact.source_ids,
            )
            continue
        if invalid:
            reject(
                "evidence",
                "extra_source_id_ignored",
                "部分来源编号无效，已忽略；继续核对其余真实来源。",
                severity="warning",
                actual=invalid,
            )
        quote = fact.quote.strip()
        spans = {sid: match_quote(quote, by_id[sid]["excerpt"]) for sid in valid_ids}
        missing = [sid for sid, span in spans.items() if span is None]
        supported = {sid: span for sid, span in spans.items() if span is not None}
        if len(quote) < 2 or not supported:
            reject(
                "evidence",
                "quote_not_in_source",
                "引文无法在引用来源中连续匹配；仅允许空白格式差异，不接受改写或拼接。",
                source_ids=missing or fact.source_ids,
            )
            continue
        if missing:
            reject(
                "evidence",
                "unsupported_reference_ignored",
                "部分来源未包含引文，已移除这些引用；保留确实支持引文的来源。",
                severity="warning",
                source_ids=missing,
            )
        spans = supported
        # Use actual source wording, with whitespace removed only for date detection.
        source_quote = next(iter(spans.values()))[0]
        explicit_dates = {
            f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
            for year, month, day in re.findall(
                r"(\d{4})(?:-|年)(\d{1,2})(?:-|月)(\d{1,2})(?:日)?",
                "".join(ch for ch in source_quote if not ch.isspace()),
            )
        }
        if (
            explicit_dates
            and empty["date"] not in explicit_dates
            and not date_in_explicit_range(empty["date"], source_quote)
        ):
            reject(
                "evidence",
                "explicit_date_mismatch",
                "引文明示日期或直接给出的完整日期范围不包含旅行日期；不能据此确认适用性。",
                expected=empty["date"],
                actual=sorted(explicit_dates),
            )
            continue
        item = fact.model_dump(mode="json", exclude={"unknown_reason", "unknown_detail"})
        windows = item.get("windows", [])
        invalid_times = [
            value
            for window in windows
            for value in (window["start_time"], window["end_time"])
            if not clock_in_quote(value, source_quote)
        ]
        if invalid_times:
            item["windows"] = []
            reject(
                "evidence",
                "opening_time_not_in_quote",
                "开放窗口中的钟点没有对应原文依据，未采用该时间结构。",
                actual=invalid_times,
            )
        if item.get("last_admission") and not clock_in_quote(item["last_admission"], source_quote):
            reject(
                "evidence",
                "admission_time_not_in_quote",
                "最晚入场钟点没有对应原文依据，未采用该钟点。",
                actual=item["last_admission"],
            )
            item["last_admission"] = None
        if item.get("closed") is True and not any(
            word in source_quote for word in ("闭馆", "关闭", "暂停开放", "不开放", "休馆")
        ):
            item["closed"] = None
            reject("evidence", "closure_not_in_quote", "闭馆标识缺少明确文字依据，未采用该标识。")
        if (
            item["field"] == "opening_hours"
            and not item["windows"]
            and item.get("closed") is not True
            and not invalid_times
        ):
            reject(
                "extraction",
                "opening_structure_missing",
                "有带来源的描述，但缺少完整开放窗口或明确闭馆标识，无法计算可用时段。",
            )
        if (
            item["field"] == "last_admission"
            and not item.get("last_admission")
            and not fact.last_admission
        ):
            reject(
                "extraction",
                "admission_structure_missing",
                "有带来源的描述，但没有明确最晚入场钟点。",
            )
        item.pop("source_ids", None)
        item.update(
            date=empty["date"],
            quote=source_quote,
            sources=[{"title": by_id[sid]["title"], "url": by_id[sid]["url"]} for sid in spans],
            queried_at=search["queried_at"],
            evidence_status="web_reported",
            citation_match="whitespace"
            if any(span[1] == "whitespace" for span in spans.values())
            else "exact",
        )
        accepted[-1] = item
    # Unexpected identities are not silently rebound to a requested field.
    for pid, day, field in reported.keys() - requested:
        diagnostics.append(
            {
                "place_id": pid,
                "day": day,
                "field": field,
                "stage": "extraction",
                "code": "unrequested_fact",
                "origin": "program",
                "message": "模型返回了补查任务之外的地点、日期或字段，未采信。",
            }
        )
    return accepted, diagnostics
