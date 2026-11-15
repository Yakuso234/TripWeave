"""Versioned CNY estimates from observed usage, never a provider invoice.

Decimal arithmetic; missing or contradictory usage is never priced as free.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

PRICE_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
SEARCH_SOURCE = "https://api-docs.deepseek.com/zh-cn/guides/search/"
CALENDAR_SOURCE = "https://www.beijing.gov.cn/cs/gncs/zcwj/202603/t20260327_4568275.html"
TARIFF = {
    "version": "deepseek-cny-20261115",
    "verified_date": "2026-11-15",
    "currency": "CNY",
    "source": PRICE_SOURCE,
    "search_source": SEARCH_SOURCE,
    "calendar_source": CALENDAR_SOURCE,
    "unit_tokens": 1000000,
    "search_per_successful_nonempty_request": "0.005",
    "models": {
        "deepseek-flash": {"off_peak": ["0.02", "1", "4"], "peak": ["0.04", "2", "8"]},
        "deepseek-v4-pro": {"off_peak": ["0.15", "4.5", "13.5"], "peak": ["0.30", "9", "27"]},
    },
}
HOLIDAYS_2026 = (
    ("01-01", "01-03"),
    ("02-15", "02-23"),
    ("04-04", "04-06"),
    ("05-01", "05-05"),
    ("06-19", "06-21"),
    ("09-25", "09-27"),
    ("10-01", "10-07"),
)


def integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def money(value):
    return format(value, ".8f")


def period(timestamp):
    try:
        instant = datetime.fromisoformat(timestamp)
        if instant.tzinfo is None:
            return None
        instant = instant.astimezone(timezone(timedelta(hours=8)))
    except ValueError, TypeError:
        return None
    minute = instant.hour * 60 + instant.minute
    if instant.weekday() >= 5 or not (540 <= minute < 720 or 840 <= minute < 1080):
        return "off_peak"
    if instant.year != 2026:
        return None  # No unverified future holiday calendar.
    day = instant.strftime("%m-%d")
    if any(start <= day <= end for start, end in HOLIDAYS_2026):
        return "off_peak"
    return "peak"


def model_cost(row, tariff):
    base = {"number": row.get("number"), "role": row.get("role"), "status": "unavailable"}
    rates = tariff["models"].get(row.get("model"))
    input_tokens, output_tokens = row.get("input_tokens"), row.get("output_tokens")
    if not rates or not integer(input_tokens) or not integer(output_tokens):
        return {**base, "reason": "模型价格或输入输出用量未完整记录"}
    hit, miss = row.get("cache_read_tokens"), row.get("cache_miss_tokens")
    if hit is not None and (not integer(hit) or hit > input_tokens):
        return {**base, "reason": "缓存命中数量与输入不一致"}
    if miss is not None and (not integer(miss) or miss > input_tokens):
        return {**base, "reason": "缓存未命中数量与输入不一致"}
    if hit is not None and miss is not None and hit + miss != input_tokens:
        return {**base, "reason": "命中与未命中之和不等于输入"}
    if hit is not None:
        miss = input_tokens - hit
    elif miss is not None:
        hit = input_tokens - miss
    elif input_tokens == 0:
        hit = miss = 0
    tier = period(row.get("started_at"))
    totals, input_costs, output_costs = [], [], []
    for selected in [tier] if tier else ["off_peak", "peak"]:
        hit_rate, miss_rate, output_rate = map(Decimal, rates[selected])
        split = [(hit, miss)] if hit is not None else [(input_tokens, 0), (0, input_tokens)]
        for hit_count, miss_count in split:
            input_cost = (hit_count * hit_rate + miss_count * miss_rate) / tariff["unit_tokens"]
            output_cost = output_tokens * output_rate / tariff["unit_tokens"]
            totals.append(input_cost + output_cost)
            input_costs.append(input_cost)
            output_costs.append(output_cost)
    return {
        **base,
        "status": "estimated" if tier and hit is not None else "range",
        "model": row["model"],
        "period": tier or "unknown",
        "min_cny": money(min(totals)),
        "max_cny": money(max(totals)),
        "input_min_cny": money(min(input_costs)),
        "input_max_cny": money(max(input_costs)),
        "output_min_cny": money(min(output_costs)),
        "output_max_cny": money(max(output_costs)),
    }


def aggregate(parts):
    if any(part["status"] == "unavailable" for part in parts):
        return {"status": "unavailable", "reason": "部分用量或价格未知，无法给出完整小计"}
    lower = sum((Decimal(part["min_cny"]) for part in parts), Decimal(0))
    upper = sum((Decimal(part["max_cny"]) for part in parts), Decimal(0))
    return {
        "status": "estimated" if all(p["status"] == "estimated" for p in parts) else "range",
        "min_cny": money(lower),
        "max_cny": money(upper),
    }


def estimate_cost(calls, search_billable, *, official=True, basis="recorded_run"):
    tariff = deepcopy(TARIFF)
    model_parts = (
        [model_cost(row, tariff) for row in calls]
        if official or not calls
        else [{"status": "unavailable", "reason": "未确认使用官方DeepSeek价格的接口"}]
    )
    models = aggregate(model_parts)
    lower = sum(value is True for value in search_billable)
    upper = lower + sum(value is None for value in search_billable)
    unit = Decimal(tariff["search_per_successful_nonempty_request"])
    search = {
        "status": "estimated" if lower == upper else "range",
        "min_cny": money(lower * unit),
        "max_cny": money(upper * unit),
        "confirmed_billable": lower,
        "unknown_billable": upper - lower,
    }
    return {
        "basis": basis,
        "currency": "CNY",
        "tariff": tariff,
        "models": models,
        "model_calls": model_parts,
        "search": search,
        "subtotal": aggregate([models, search]),
        "excluded": ["地图服务费用", "本轮之前的选景推荐", "未观察到的账户其他调用"],
        "note": "按所示价格版本估算，未核对账单；价格变化、账户优惠及供应商计费边界可能影响实扣。",
    }


def legacy_cost_preview(candidate):
    """Read-only current-tariff range, explicitly conditional, without rewriting history."""
    usage = candidate.get("usage") or {}
    if candidate.get("model") not in TARIFF["models"] or not all(
        integer(usage.get(key)) for key in ("input_tokens", "output_tokens")
    ):
        return None
    searches = candidate.get("research_search_calls")
    if not integer(searches) or searches > 5:
        return None
    return estimate_cost(
        [
            {
                "number": None,
                "role": "combined",
                "model": candidate["model"],
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
            }
        ],
        [None] * searches,
        basis="current_tariff_legacy_assumption",
    )


def candidate_cost_view(candidate):
    if not candidate or candidate.get("api_cost_estimate") is not None:
        return candidate
    preview = legacy_cost_preview(candidate)
    return {**candidate, "api_cost_preview": preview} if preview else candidate
