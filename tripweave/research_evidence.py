"""Traceable evidence acceptance, not verification of real-world availability."""

import re
from collections import defaultdict
from datetime import date
from unicodedata import normalize

FIELD_NAMES = {
    "opening_hours": "开放时段",
    "last_admission": "最晚入场时间",
    "reservation_rules": "预约规则",
    "reservation_channel": "预约入口",
    "reservation_requirement": "当天是否必须预约",
    "ticket_availability": "当日余票或可预约名额",
}

_ENTRY = r"(?:入场|入馆|入园|进馆|入内|检票)"
_CLOCK = (
    r"(?<![\d:])(?:(?P<period_cn>凌晨|早上|上午|中午|下午|傍晚|晚上)\s*)?"
    r"(?P<hour>[01]?\d|2[0-3])(?::(?P<minute>[0-5]\d)|[点时](?:(?P<minute_cn>[0-5]?\d)分?|(?P<half>半))?)"
    r"(?![\d:])(?:\s*(?P<period_en>[ap]\.?m\.?)(?![a-z]))?"
)
_CLOCK_IN_TEXT = re.compile(_CLOCK, re.IGNORECASE)
_ADMISSION_PATTERNS = (
    re.compile(
        rf"(?:最晚{_ENTRY}|(?:停止|截止){_ENTRY}|{_ENTRY}(?:时间)?截止|"
        rf"last\s+(?:entry|admission))(?:时间)?\s*(?:[:：为是]|(?:is|at)\b)?\s*{_CLOCK}",
        re.IGNORECASE,
    ),
    re.compile(rf"{_CLOCK}\s*(?:后|以后|起)?(?:停止|截止|不再(?:允许)?)\s*{_ENTRY}", re.IGNORECASE),
)
_RESERVATION_EXEMPT = re.compile(
    r"(?:无需|无须|不用|不必|不再需要|不再要求|不需要|不需|不强制)(?:提前|事先|实名|线上)*预约|"
    r"免(?:提前)?预约|取消(?:了)?(?:实名|提前)?预约(?:制|要求)|预约(?:不是|并非)必须|"
    r"(?:no\s+(?:advance\s+)?(?:reservation|booking)"
    r"\s+(?:is\s+)?required|(?:reservation|booking)\s+(?:is\s+)?not\s+required)",
    re.IGNORECASE,
)
_RESERVATION_REQUIRED = re.compile(
    r"(?:必须|务必|需要|须|需)\s*(?:事先|提前(?:[0-9一二三四五六七八九十]+天)?|实名|线上|进行|"
    r"(?:通过|在)[^，,。；;]{1,18})*\s*预约|"
    r"实行预约制|(?:仅限|限)已预约(?:的)?(?:观众|游客|人员)?(?:入场|入馆|参观)|"
    r"(?:advance\s+)?(?:reservation|booking)\s+(?:is\s+)?required",
    re.IGNORECASE,
)
_CONDITIONAL_SCOPE = re.compile(
    r"仅部分|特殊情况|除外|平日|工作日|周末|周[一二三四五六日天]|星期[一二三四五六日天]|"
    r"节假日|假期|寒暑假|特展|高峰|视情况|如遇|若遇|(?:如|若)\s*(?:需|须)"
)
_GROUP_ONLY = re.compile(r"团队|团体|旅行团|group\s+(?:visits?|tours?|bookings?)", re.IGNORECASE)
_CLAUSE_BREAK = re.compile(r"[，,。！？!?；;\n]+")


def _time_of_day(match: re.Match[str]) -> str | None:
    hour = int(match["hour"])
    minute = 30 if match["half"] else int(match["minute"] or match["minute_cn"] or 0)
    period = None
    if match["period_cn"]:
        period = "am" if match["period_cn"] in ("凌晨", "早上", "上午") else "pm"
    if match["period_en"]:
        suffix = match["period_en"][0].lower() + "m"
        if not 1 <= hour <= 12 or (period and period != suffix):
            return None
        period = suffix
    if period == "am":
        if hour > 12:
            return None
        hour %= 12
    elif period == "pm" and hour < 12:
        hour += 12
    return f"{hour:02d}:{minute:02d}"


def admission_supported(value: str, quote: str, target_date: str | None = None) -> bool:
    clocks = []
    for clause in _CLAUSE_BREAK.split(normalize("NFKC", quote)):
        stated = [
            _time_of_day(match)
            for pattern in _ADMISSION_PATTERNS
            for match in pattern.finditer(clause)
        ]
        if not stated or _GROUP_ONLY.search(clause) or conflicting_dates(target_date, clause):
            continue
        if _CONDITIONAL_SCOPE.search(clause) or re.search(r"夜场|夜游|night\s+session", clause):
            return False
        clocks.extend(stated)
    return bool(clocks) and all(clock == value for clock in clocks)


def reservation_in_quote(quote: str, target_date: str | None = None) -> bool | None:
    """Recognize explicit visitor rules and leave conditional wording unresolved."""
    rules = set()
    for clause in _CLAUSE_BREAK.split(normalize("NFKC", quote)):
        if _GROUP_ONLY.search(clause) or conflicting_dates(target_date, clause):
            continue
        exempt = _RESERVATION_EXEMPT.search(clause)
        required = _RESERVATION_REQUIRED.search(_RESERVATION_EXEMPT.sub("", clause))
        if (exempt or required) and _CONDITIONAL_SCOPE.search(clause):
            return None
        if exempt:
            rules.add(False)
        if required:
            rules.add(True)
    return next(iter(rules)) if len(rules) == 1 else None


def reservation_value(required: bool) -> str:
    return "当天必须提前预约" if required else "当天不要求提前预约"


def carry_research_fact(fact: dict) -> dict:
    """Check legacy web claims again when restarting from a saved draft."""
    if fact.get("evidence_status") != "web_reported" or fact.get("status") != "found":
        return fact
    quote = fact.get("quote") or ""
    field = fact["field"]
    if field == "reservation_requirement":
        stated = reservation_in_quote(quote, fact.get("date"))
        reported = fact.get("reservation_required", stated)
        if stated is not None and reported is stated:
            return {**fact, "reservation_required": stated, "value": reservation_value(stated)}
    elif field == "last_admission":
        time = fact.get("last_admission")
        if time and admission_supported(time, quote, fact.get("date")):
            return fact
    else:
        return fact
    return {
        **fact,
        "status": "unknown",
        "evidence_status": "unknown",
        "value": "",
        "quote": "",
        "sources": [],
        "queried_at": None,
        "reservation_required": None,
        "last_admission": None,
    }


def usable_research_fact(fact):
    if fact["status"] != "found" or fact["field"] == "ticket_availability":
        return False
    if fact["field"] == "opening_hours":
        return bool(fact.get("windows") or fact.get("closed") is True)
    if fact["field"] == "last_admission":
        return bool(fact.get("last_admission"))
    if fact["field"] == "reservation_requirement":
        return isinstance(fact.get("reservation_required"), bool)
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


def conflicting_dates(target: str | None, text: str) -> list[str]:
    if not target:
        return []
    compact = "".join(ch for ch in text if not ch.isspace())
    dates = {
        f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        for year, month, day in re.findall(
            r"(\d{4})(?:-|年)(\d{1,2})(?:-|月)(\d{1,2})(?:日)?", compact
        )
    }
    if target in dates or date_in_explicit_range(target, compact):
        return []
    return sorted(dates)


def clock_in_quote(value: str, quote: str) -> bool:
    return any(
        _time_of_day(match) == value for match in _CLOCK_IN_TEXT.finditer(normalize("NFKC", quote))
    )


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
        target_date = empty["date"]
        if other_dates := conflicting_dates(target_date, source_quote):
            reject(
                "evidence",
                "explicit_date_mismatch",
                "引文明示日期或直接给出的完整日期范围不包含旅行日期；不能据此确认适用性。",
                expected=target_date,
                actual=other_dates,
            )
            continue
        item = fact.model_dump(mode="json", exclude={"unknown_reason", "unknown_detail"})
        if fact.field == "reservation_requirement":
            stated = reservation_in_quote(source_quote, target_date)
            supporting = {
                sid: span
                for sid, span in spans.items()
                if stated is not None
                and reservation_in_quote(by_id[sid]["excerpt"], target_date) is stated
            }
            if not supporting:
                reject(
                    "evidence",
                    "reservation_requirement_not_supported",
                    "引文及其上下文没有针对本次普通游客明确说明必约或免约；预约入口、团体和特殊时段规则不能替代。",
                )
                continue
            if fact.reservation_required is not stated:
                reject(
                    "extraction",
                    "reservation_requirement_conflicts_with_quote",
                    "须填写reservation_required并与引文的必约或免约结论一致。",
                    actual=fact.reservation_required,
                    expected=stated,
                )
                continue
            item["value"] = reservation_value(stated)
            spans = supporting
            source_quote = next(iter(spans.values()))[0]
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
        if item["field"] == "last_admission":
            cutoff = item.get("last_admission")
            if not cutoff:
                reject("extraction", "admission_structure_missing", "缺少明确的最晚入场钟点。")
                continue
            if not clock_in_quote(cutoff, source_quote):
                reject(
                    "evidence",
                    "admission_time_not_in_quote",
                    "最晚入场钟点没有对应原文依据。",
                    actual=cutoff,
                )
                continue
            supporting = {
                sid: span
                for sid, span in spans.items()
                if admission_supported(cutoff, span[0], target_date)
                and admission_supported(cutoff, by_id[sid]["excerpt"], target_date)
            }
            if not supporting:
                reject(
                    "evidence",
                    "admission_meaning_not_supported",
                    "引文及其上下文未明确给出适用的最晚入场；闭馆、停止售票和冲突钟点不能用。",
                    actual=cutoff,
                )
                continue
            spans = supporting
            source_quote = next(iter(spans.values()))[0]
            item["value"] = f"最晚 {cutoff} 入场"
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
