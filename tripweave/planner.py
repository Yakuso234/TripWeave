"""Run the real PydanticAI loop with an explicitly simulated model and map."""

from datetime import UTC, datetime

from pydantic_ai import Agent, ModelRetry
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import UsageLimits

from .domain import INDEX, MATRIX, PLACES, Itinerary, Requirements, fixture_plan, validate_plan


async def plan(req: Requirements, *, always_invalid: bool = False) -> dict:
    trace = []
    attempts = 0
    issues = []
    latest = None

    def scripted_model(messages, info):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return ModelResponse(parts=[ToolCallPart("lookup_map", {})])
        output = fixture_plan(req)
        # An intentional fixture fault proves validator feedback/retry wiring.
        if (attempts == 2 or always_invalid) and output.stops:
            output.stops[0].start_time = "00:00"
            trace.append(
                {
                    "event": "fixture_fault",
                    "detail": "固定测试脚本注入早于出发时间的初稿；不代表真实模型错误",
                }
            )
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, output.model_dump(mode="json"))]
        )

    agent = Agent(FunctionModel(scripted_model), output_type=Itinerary, retries=2)

    @agent.tool_plain
    def lookup_map() -> dict:
        """Read the synthetic Shanghai place and walking-time fixture."""
        trace.append({"event": "tool", "detail": "lookup_map：读取五个固定地点及合成步行时间"})
        return {"places": PLACES, "walking_minutes": MATRIX, "mode": "fixture"}

    @agent.output_validator
    def check(output: Itinerary) -> Itinerary:
        nonlocal issues, latest
        latest = output
        issues = validate_plan(req, output)
        trace.append(
            {
                "event": "validation",
                "detail": "；".join(i["message"] for i in issues) or "已知约束检查通过",
            }
        )
        if issues:
            raise ModelRetry("；".join(i["message"] for i in issues))
        return output

    error, usage = None, None
    try:
        result = await agent.run(
            req.model_dump_json(), usage_limits=UsageLimits(request_limit=5), timeout=15
        )
        latest = result.output
        # FunctionModel usage is synthetic and intentionally not reported as real tokens.
        usage = {"requests": result.usage.requests, "input_tokens": None, "output_tokens": None}
    except (UnexpectedModelBehavior, UsageLimitExceeded, TimeoutError) as exc:
        error = f"实验在有限重试内未得到通过结果（{type(exc).__name__}）；这不证明需求无解。"
    stamp = datetime.now(UTC).isoformat()
    return {
        "status": "ready" if latest and not issues and not error else "conflict",
        "itinerary": latest.model_dump(mode="json") if latest else None,
        "places": [
            {**p, "source": "合成固定样本，非实时高德", "queried_at": stamp} for p in PLACES
        ],
        "routes": [
            {
                "origin_id": a,
                "destination_id": b,
                "duration_minutes": MATRIX[i][j],
                "source": "合成步行时间，未统计景区内步行",
            }
            for a, i in INDEX.items()
            for b, j in INDEX.items()
            if a != b
        ],
        "issues": issues,
        "trace": trace,
        "usage": usage,
        "error": error,
    }
