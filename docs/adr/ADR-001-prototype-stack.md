# ADR-001 初版运行时与技术栈

2026-11-06；状态：采用用于离线原型，真实模型适配待验证。

采用本机Python3.14.7、PydanticAI Slim2.66.0、Pydantic2.14.3、FastAPI0.162.0、SQLite、原生HTML/CSS/JavaScript、uv、pytest和Ruff。完整版本由uv.lock固定。

## 为什么采用

本版需要单Agent工具循环、类型化行程、检查反馈、有限重试。PydanticAI的output_type、output_validator、ModelRetry、FunctionModel直接提供这些实验入口。LangGraph同样能实现，也是DG已使用的备选；当前没有跨进程图恢复需求，暂不使用checkpoint。没有通过实验声称PydanticAI速度或成功率优于LangGraph。

SQLite业务表负责需求、候选与接受版本，事务检查版本；不增加第二事实源。原生页面足够承载本版表单和结果，不引入Node构建链。以后根据实际交互复杂度评估React。PostgreSQL、AG-UI、MCP、DBOS没有安装验证，也不列为当前能力。

## 可复查依据

- 官方输出校验：https://pydantic.dev/docs/ai/core-concepts/output/
- 固定发行源码：https://raw.githubusercontent.com/pydantic/pydantic-ai/v2.66.0/pydantic_ai_slim/pyproject.toml
- 发行元数据：https://pypi.org/pypi/pydantic-ai-slim/2.66.0/json
- 备选框架：https://docs.langchain.com/oss/python/langgraph/overview
- uv锁定：https://docs.astral.sh/uv/concepts/projects/sync/

本机uv sync实际解析45包、安装43包，并生成锁文件。SDK使用httpx2；独立httpx仅用作FastAPI测试客户端，没有混用注入。

实际tests/test_prototype.py运行PydanticAI：调用lookup_map工具、固定脚本注入不合法初稿、ModelRetry反馈后修订成功；一直错误则有限退出。这验证运行时控制流程与本机兼容，不验证真实LLM能力。真实模型和地图凭据缺失，用户明确选择先离线实验。

Google Travel Concierge仅作结构化交互思路参考：https://github.com/google/adk-samples/tree/main/python/agents/travel-concierge 。未复制代码或宣称已跑通其样例。

后续得到凭据后，先验证目标模型的工具调用与结构化输出，再决定是否保留主运行时。选择有实验边界，不以网页宣传作为效果证明。
