"""An explicit approving reviewer for older tests focused on planner/research.

This still goes through the real SDK, graph, usage recorder and shared budget.
Negotiation, rejection and reviewer failures are covered in test_planning_review.
"""

import json

import httpx2


def with_approved_review(provider):
    def wrapped(request):
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content)
            names = {t["function"]["name"] for t in body.get("tools", [])}
            if names == {"review_report"}:
                return httpx2.Response(
                    200,
                    json={
                        "id": "mock-review",
                        "object": "chat.completion",
                        "created": 1,
                        "model": body["model"],
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "tool_calls",
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "mock-review-result",
                                            "type": "function",
                                            "function": {
                                                "name": "review_report",
                                                "arguments": json.dumps(
                                                    {
                                                        "summary": "合成校验通过，仅用于HTTP模拟。",
                                                        "suggestions": [],
                                                    }
                                                ),
                                            },
                                        }
                                    ],
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 42, "completion_tokens": 20, "total_tokens": 62},
                    },
                )
        return provider(request)

    return wrapped
