"""Bounded, numeric-only observations of non-streaming model HTTP requests.

No headers, prompts, tool arguments, response text or thinking content are retained.
Provider counts are observations, not local token estimates or a billing ledger.
"""

import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime


def planning_budget(settings) -> dict:
    return {
        "max_model_requests": settings.max_requests,
        "run_timeout_seconds": settings.run_timeout,
        "planner_max_output_tokens": settings.planner_max_tokens,
        "research_max_output_tokens": settings.research_max_tokens,
        "reviewer_max_output_tokens": settings.reviewer_max_tokens,
        "input_token_limit": None,
    }


def count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class ModelUsageRecorder:
    def __init__(self):
        self.started = time.monotonic()
        self.calls = []
        self._starts = {}
        self._role = ContextVar("tripweave_usage_role", default="planner")
        self._stage = ContextVar("tripweave_usage_stage", default=None)

    @contextmanager
    def stage(self, stage):
        token = self._stage.set(stage)
        try:
            yield
        finally:
            self._stage.reset(token)

    @contextmanager
    def role(self, role):
        token = self._role.set(role)
        try:
            yield
        finally:
            self._role.reset(token)

    async def request(self, request):
        if not request.url.path.endswith("/chat/completions"):
            return
        try:
            payload = json.loads(request.content)
        except ValueError, UnicodeDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        number = len(self.calls) + 1
        row = {
            "number": number,
            "started_at": datetime.now(UTC).isoformat(),
            "model": payload.get("model") if isinstance(payload.get("model"), str) else None,
            "role": self._role.get(),
            "phase": self._stage.get(),
            "status": "pending",
            "max_output_tokens": count(
                payload.get("max_completion_tokens", payload.get("max_tokens"))
            ),
            "input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_miss_tokens": None,
            "finish_reason": None,
            "http_status": None,
            "elapsed_ms": None,
        }
        self.calls.append(row)
        self._starts[number] = time.monotonic()
        request.extensions["tripweave_usage_number"] = number

    async def response(self, response):
        number = response.request.extensions.get("tripweave_usage_number")
        if number is None:
            return
        row = self.calls[number - 1]
        row["http_status"] = response.status_code
        row["status"] = "reading_response"
        # Agent.run uses non-streaming Chat Completions. Read once and let the SDK
        # reuse the buffered body; only explicitly allowed numeric fields survive.
        await response.aread()
        row["status"] = "response" if response.is_success else "http_error"
        row["elapsed_ms"] = round((time.monotonic() - self._starts[number]) * 1000)
        try:
            body = response.json()
        except ValueError, UnicodeDecodeError:
            return
        if not isinstance(body, dict):
            return
        usage = body.get("usage")
        if isinstance(usage, dict):
            row["input_tokens"] = count(usage.get("prompt_tokens"))
            row["output_tokens"] = count(usage.get("completion_tokens"))
            detail = usage.get("completion_tokens_details")
            if isinstance(detail, dict):
                row["reasoning_tokens"] = count(detail.get("reasoning_tokens"))
            detail = usage.get("prompt_tokens_details")
            if isinstance(detail, dict):
                row["cache_read_tokens"] = count(detail.get("cached_tokens"))
            if row["cache_read_tokens"] is None:
                row["cache_read_tokens"] = count(usage.get("prompt_cache_hit_tokens"))
            row["cache_miss_tokens"] = count(usage.get("prompt_cache_miss_tokens"))
        choices = body.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            reason = choices[0].get("finish_reason")
            if reason in (
                "stop",
                "tool_calls",
                "length",
                "content_filter",
                "function_call",
                "insufficient_system_resource",
            ):
                row["finish_reason"] = reason
            elif reason is not None:
                row["finish_reason"] = "other"

    def snapshot(self, settings) -> dict:
        for row in self.calls:
            if row["status"] in ("pending", "reading_response"):
                row["status"] = "no_complete_response"
                row["elapsed_ms"] = round((time.monotonic() - self._starts[row["number"]]) * 1000)
        return {
            "version": 1,
            "scope": "this_planning_run",
            "elapsed_seconds": round(time.monotonic() - self.started, 3),
            "budget": planning_budget(settings),
            "model_http_attempts": len(self.calls),
            "usage_complete": all(
                row["input_tokens"] is not None and row["output_tokens"] is not None
                for row in self.calls
            ),
            "model_calls": [dict(row) for row in self.calls],
        }
