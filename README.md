# TripWeave · 旅行规划工作台

面向 Agent 应用工程的最小可规划实验。v0.2 将固定数据回放改为 **真实高德地图＋基础排程 / DeepSeek 工具循环**，提供中文分页工作台、1–3 天行程、住宿选择、餐饮与交通安排、约束检查、候选接受和规划历史。

**当前没有真实服务凭据，尚未验证高德和 DeepSeek 在线效果。** HTTP 合同测试使用模拟响应；产品无 Key 时阻止生成，不会回退到演示行程。v0.1 离线实验保留在 Git 历史中。

## 启动

Windows PowerShell，在项目目录执行：

```powershell
uv --cache-dir .runtime/uv-cache sync --locked
.\start.ps1
```

打开 [本地工作台](http://127.0.0.1:8765/)。终端 Ctrl+C 停止。系统脚本策略阻止时，可运行 `.\.venv\Scripts\python.exe -m tripweave.launcher --port 8765`。

本机验证环境 Python 3.14.7、uv 0.12.5。新机器需先安装 uv/Python 3.14。启动器核对工作区指纹：同目录已有服务时复用地址，其他项目占用端口则退出，不结束其他进程。检查用 `.\start.ps1 -CheckOnly`，换端口用 `.\start.ps1 -Port 8766`。

## 配置与使用

首次将 `.env.example` 复制为 `.env`（已有文件不要覆盖），在本机填写：

| 配置 | 用途 |
| --- | --- |
| `AMAP_API_KEY` | 所有规划必需；高德 **Web 服务** Key，需具有 POI 搜索及路线权限 |
| `DEEPSEEK_API_KEY` | DeepSeek 模式必需；不在网页输入、不发到聊天 |
| `LLM_BASE_URL` / `LLM_MODEL` | 默认 DeepSeek 官方地址与 `deepseek-flash`；模型别名可修改 |
| `TRIPWEAVE_DATABASE` | 默认 `.runtime/tripweave.sqlite3`，本地无需额外数据库服务 |
| `DATABASE_URL` | 可选 PostgreSQL 连接串，优先于本地路径；详见部署说明 |
| `TRIPWEAVE_MAX_REQUESTS` / `TRIPWEAVE_RUN_TIMEOUT` | 默认 6 次模型请求 / 整次规划 90 秒 |

页面设置中的“已配置”只代表存在 Key，不代表权限、额度或连通性已验证。修改 API 配置后刷新页面配置状态；修改数据库连接后重启服务。

1. **旅行偏好**：城市、日期、人数、天数、出发点、节奏、每天时间边界、预算和固定预约，分三步填写。
2. **住宿方案**：查询真实酒店 POI，按与出发点/预约片区的距离推荐，或手选。房价、房型和空房均待确认。
3. **每日安排**：基础排程按已查询路线、时间和预约安排活动；DeepSeek 模式还能解释额外的自然语言要求。基础排程只使用明确字段和简单兴趣关键词。
4. 检查每天景点、餐饮、路段耗时、住宿与待确认项，再接受候选。接受只保存本应用版本，不会订房或预约。
5. 修改出发时间或节奏并重新规划；旧候选保留原需求快照，过时版本不可接受。规划记录可以查看最近候选和失败；已接受方案刷新后恢复。

示例输入：上海、两天一晚、两人、人民广场 09:00 出发，每天 20:30 前结束、第一天上海博物馆 14:00–15:00 固定活动。真实地点由高德检索，不预先承诺输出某个景点或酒店；把出发改为 14:30 后固定预约会产生衔接冲突，不会偷偷改预约时间。

## 技术与数据

```text
分步偏好 / 历史页面 → FastAPI → 读取需求快照
                              ↓
                    高德地点、附近餐饮、路线
                              ↓
                   基础排程 / DeepSeek 工具循环
                              ↓
               Pydantic 合同＋确定性约束检查＋有限反馈
                              ↓
               候选与证据入库 → 用户接受 → 事务检查版本
```

- `domain.py`：结构化需求与行程合同。
- `maps.py`：高德 HTTP 适配、来源时间、单次运行路线缓存、超时及响应校验。
- `real_planner.py`：基础排程、DeepSeek 工具与检查器；模型只提候选。
- `storage.py`：SQLAlchemy Core，SQLite 默认 / PostgreSQL 可选，事务内记录需求、候选和接受事件。
- `app.py`：API 编排、同源限制、业务错误响应；外部调用期间不持有数据库事务。
- `static/`：原生 HTML/CSS/JS，不需 Node 构建链；顶部五页与分步卡片。

需求与接受版本以数据库为事实源，不从模型对话摘要恢复。提交使用需求修订号和已接受版本共同检查，存储更新另有原子比较写保护。历史事件与变更在同一事务提交。当前聚合仍以 JSON 文本保存，未实现复杂跨行程分析或全套 schema 迁移系统；旧本地表和记录保留。

选型依据：[Agent 运行时](docs/adr/ADR-001-prototype-stack.md)、[真实接口与数据库](docs/adr/ADR-002-real-planning-storage.md)。[后续部署要求](docs/DEPLOYMENT.md)。

## 边界

- 酒店位置推荐不等于低价保证；住宿按用户上限预留，餐饮按 65 元/人/餐预留，门票、交通等实际费用为未知，不能宣称总费用已满足。
- 路线是查询时的估算；未验证未来日期营业、节假日、排队、公交班次和景区内步行。地点同名或分馆需人工确认。
- 排程是有限候选的启发式方案，不是全局最优；冲突或请求耗尽不意味着数学上无解。最后一天不默认回酒店或返回出发站。
- 无提醒、自动订单、支付、MCP、RAG、持久任务恢复或多用户鉴权；只监听 127.0.0.1，供本地单用户实验。仅配置 PostgreSQL 不等于可公开部署。
- `.env`、`.runtime`、私人记录不进入仓库；尚未指定项目开源许可证。未复制其他旅游项目实现代码，第三方依赖遵循其许可证。

## 检查

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check tripweave tests
.\.venv\Scripts\ruff.exe format --check tripweave tests
Get-Content tripweave/static/app.js -Raw | node --check
Get-Content tests/frontend_state.cjs -Raw | node
```

测试与真实服务验证分开记录，最新结果见 [v0.2 验证报告](docs/evaluations/V02_QA.md)。
