# ADR-002 真实规划接入与最小存储增强

日期：2026-11-07。状态：采用高德/DeepSeek 接入代码及 SQLAlchemy Core；SQLite 本地默认；PostgreSQL 为待真实环境验收的可选后端。取代 ADR-001 中只支持离线产品的范围，保留其框架比较结论。

## 需求、选择与替代方案

| 当前问题 | 决策与理由 | 代价、边界及验证 |
| --- | --- | --- |
| 用户需要实际规划而非固定输出 | 高德地点/路线作为证据，基础排程和 DeepSeek 共用检查器 | 依赖接口权限；无 Key 或失败不能生成样例冒充。HTTP 模拟检查，在线待验 |
| 默认使用用户常用的 DeepSeek | 兼容 SDK＋PydanticAI 工具输出、ModelRetry；使用官方非思考工具调用协议 | 有请求/时间上限；工具格式支持不等于旅行质量已通过 |
| 保留需求与接受事实、处理重复和过时结果 | 数据库保存聚合与事件，版本检查＋原子比较更新；外部调用不占事务 | 当前 JSON 文本聚合便于兼容；复杂分析需未来结构迁移 |
| 本机无需部署就能开发 | 继续 SQLite 文件库 | 单机写并发有限；没有证据表明当前已需要外部数据库 |
| 为后续部署和并发请求留出实代码 | SQLAlchemy Core 2.0 维护线＋可选 Psycopg 3，适配 PostgreSQL | 增加依赖和数据库差异验证成本；不是仅替换配置就宣称已验收 |

不采用完整 ORM 对象关系层：当前主要存取类型化行程聚合，需要显式事务与比较写，Core 足够；不建立第二套领域对象。SQLAlchemy 2.0.57 是当前维护发行，本次只用其成熟 Core API，没有为了版本新而引入 2.1 特性。

PostgreSQL 适合服务端多个连接下的事务型业务；MySQL 同样可满足这些条件，此处没有性能或正确性胜负结论。当前工程选择 PostgreSQL 是为了收敛到一个明确服务端后端及其官方 Psycopg 适配，减少两套维护；用户尚无需部署它。采用 PostgreSQL 18 的受支持稳定线作为后续验收目标，不使用 19 RC。正式部署时安装该主版本最新修订版。

## 暂不引入的组件

- Redis：没有跨进程共享缓存或高频热点的测量证据。当前路线仅做一次规划内缓存，避免过期地理数据长期复用。
- 消息队列：本轮是有总时限的交互请求，没有需要可靠投递的通知/订单消费者。进程崩溃后任务恢复尚未实现，明确记录此限制。
- 向量库/RAG：没有经过整理的旅游资料库和检索评测；地点检索由地图供应商承担。
- MCP：工具协议不能替代实际数据。先验证 HTTP 数据和业务合同，再评估是否需要可替换的 MCP 适配。

撤回条件：真实 DeepSeek 工具协议不兼容时，先以同一测试重现和修适配，基础排程仍可独立使用；PostgreSQL 未通过并发与历史测试前保留 SQLite。需求没有后台持久执行场景时不引入任务引擎。

## 官方依据与本机证据

核对日期 2026-11-06 至 2026-11-07；以下是接口/发行依据，不是产品效果评测：

- [DeepSeek 首次调用](https://api-docs.deepseek.com/quick_start)：官方地址与 `deepseek-flash` 模型别名。
- [DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)：非思考调用 `thinking.type=disabled`；本版使用非 strict 工具合同，不声称测试了 beta strict 接口。
- [高德 POI 搜索](https://lbs.amap.com/api/webservice/guide/api-advanced/search)：text/around/detail 与 Web 服务 Key；POI 不等于酒店交易库存。
- [高德路径规划](https://lbs.amap.com/api/webservice/guide/api/direction)：步行、公共交通；秒转换分钟，并校验缺失/非有限数值。
- [SQLAlchemy 2.0 连接与事务](https://docs.sqlalchemy.org/en/20/core/connections.html)：Engine、事务和连接生命周期；[PostgreSQL/Psycopg 方言](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#module-sqlalchemy.dialects.postgresql.psycopg)。
- [PostgreSQL 版本维护政策](https://www.postgresql.org/support/versioning/)：稳定线支持期和小版本更新政策。

本机 `uv --cache-dir .runtime/uv-cache sync --extra postgres` 实际成功安装 SQLAlchemy 2.0.57、Psycopg/Psycopg-binary 3.3.8（Python 3.14.7），uv.lock 锁定发行与哈希。第一次下载因 sandbox 网络限制失败，获准下载后成功；不是换库解决业务故障。

真实 SDK/PydanticAI 与 HTTP MockTransport 合同运行验证工具调用、反馈修订及网络失败。SQLite 执行实际读写、旧库兼容、事务和竞争测试；PostgreSQL 集成入口提供但未运行（无专用测试服务，本机 Docker daemon 未运行）。最终命令结果见 V02_QA.md，不能把 SQL 方言编译或驱动导入当 PostgreSQL 实际验证。

## 地图供应商比较

2026-11-08 补充：用户报告在高德遇到技术服务许可提示。此前核对了功能和请求合同，目标账号准入尚未验证。高德普通 Web 服务仍是已有适配，供应商的实际采用取决于账号能否创建凭据，以及必要接口能否在个人项目预算内调用。百度公开有个人开发者配额，作为待验证备选。准入和最小实测步骤见 DEPLOYMENT.md。

百度地图的地点检索与步行/公交路线也覆盖首版所需合同。采用高德的依据是本轮已经完成地图适配及异常合同测试，一家供应商可贯通地点 ID、坐标与路线，官方 MCP 也提供相应查询能力，便于后续在保持数据来源一致的条件下验证工具协议。未做跨供应商在线对照，覆盖、精度、费用及配额优劣未判定。若已有百度 AK 或高德权限/配额不满足，用同一组城市和预约样本实测，再决定是否替换。替换时须重新确认地点标识、坐标格式和路线耗时单位，并校对展示来源；单换 Key 不足以切换。

- [百度地点检索](https://lbsyun.baidu.com/docs/webapi?title=placev3%2Fguide%2Fwebservice-placeapiV3%2FinterfaceDocumentV3) 与 [百度轻量公交路线](https://lbsyun.baidu.com/docs/webapi?title=directionlite%2Fguide%2Fwebservice-lwrouteplanapi%2Ftransit)
- [高德路线](https://lbs.amap.com/api/webservice/guide/api/direction) 与 [高德 MCP](https://lbs.amap.com/api/mcp-server/)

## 无商业地图 Key 的候选路径

- [MyTripPlanner](https://github.com/Prot10/MyTripPlanner) 用 Nominatim 查地点、OSRM 算公路路线，并缓存结果。其做法证明这类原型可以采用开放地图数据。它的自驾场景不直接验证上海步行、公交与餐饮覆盖。
- [TravelPlanner](https://github.com/OSU-NLP-Group/TravelPlanner) 在评测中通过工具查询本地旅行数据库，由 Agent 针对不同请求生成行程。可借鉴固定数据版本和可复查评测的方法。
- TripWeave 优先验证 [Geofabrik 上海 OpenStreetMap 数据](https://download.geofabrik.de/asia/china.html) 的单城市样本。先检查中文地名、酒店、餐饮和景点覆盖，再验证步行路网、缺路段处理和一次带固定预约的重新规划。实际运行前保留候选状态。公交、营业、房价和库存分别标记证据范围。导入时记录来源和快照日期，按 [ODbL](https://www.openstreetmap.org/copyright) 保留署名和数据授权信息。
- 公共 [Nominatim 使用政策](https://operations.osmfoundation.org/policies/nominatim/) 有每秒一次的上限，禁止用系统性请求下载区域内所有地点。区域数据应从开放数据快照获取。
