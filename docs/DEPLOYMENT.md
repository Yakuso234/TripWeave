# 后续环境准备与验收

当前不需要部署额外中间件，SQLite 即可运行本地工作台。先接入地图和模型，观察实际规划效果；服务端数据库可以在需要远程部署或并发运行时再配置。

## 现在需要的 API

1. 高德 Web 服务 Key：POI 文本搜索、附近餐饮、地点详情、步行/公交路线的权限和额度。不同账号权限可能不同；必须实际测试，不假设创建 Key 就全部可用。
2. DeepSeek API Key：拥有可用模型及额度；默认 `deepseek-flash`，支持在 `.env` 调整。不要发到聊天或提交仓库。

没有酒店预订、支付或票务 API 的需求。本版不会把高德酒店地点当成实时房价和房量。MCP 后续使用哪个供应商，就仍需要相应供应商权限。

## 地图准入验证

1. 确认实际使用场景与账号类型，查看控制台能否创建凭据以及启用所需接口。高德普通 Web 服务和 MCP 的准入分别确认。高德公开说明列有个人研究学习的基础服务配额，实际账号创建资格和接口权限仍待核实。[高德许可说明](https://lbs.amap.com/faq/advisory/authorization/43168) · [普通 Web 服务申请指南](https://lbs.amap.com/api/webservice/create-project-and-key)
2. 如普通 Web 服务 Key 也被购买提示阻挡，验证百度个人开发者服务端 AK。百度公开列有个人地点检索和路线规划配额，需确认所选接口和本地运行环境可用。百度当前仅为备选，需编写供应商适配并验证坐标与返回格式。[百度开发者权益](https://lbsyun.baidu.com/solutions/privilege) · [服务端 AK 指南](https://lbsyun.baidu.com/faq/details?id=2298&title=2639)
3. 先用一个目标城市实测地点关键词、周边餐饮、地点详情、步行和公交。记录可用服务、错误码、额度与并发限制。再跑一条包含住宿和固定预约的两天行程。账户暂未通过时保留真实地图待验状态，合同测试继续使用标记清楚的模拟响应。

## 可选 PostgreSQL

目标 PostgreSQL 18 稳定线，部署时选最新小版本。提供独立 `tripweave` 数据库和应用角色，权限足以在该库内创建本项目表和读写，不需要超级用户。数据库仅向运行应用的主机开放；连接地址、端口、用户名、密码通过本地配置管理。

本轮不自动启动 Docker、不占用 5432 或其他项目端口。若同机容器部署，请先检查计划映射的端口是否空闲；远端部署以实际地址端口为准。

```powershell
uv --cache-dir .runtime/uv-cache sync --locked --extra postgres
```

在 `.env` 填写（占位符替换成真实信息；密码内特殊字符需 URL 编码）：

```dotenv
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:PORT/tripweave
```

重启应用后使用 PostgreSQL；不填写或留空则使用 `TRIPWEAVE_DATABASE` 的 SQLite 文件。数据不会自动从本地复制到远端；切换前保留本地库。初始化仅创建缺失的表/索引，不是通用 schema 迁移方案；未来表结构变更需补显式迁移。

长期旅行偏好使用同一个数据库中的 `travel_preferences` 表，通过版本号防止旧编辑覆盖新偏好。当前每个本地工作台使用一份档案。新建旅行时复制偏好为当次需求。

## 部署后必须验收

- 配置专用测试数据库的进程环境变量 `TRIPWEAVE_TEST_POSTGRES_URL` 后运行 `.\.venv\Scripts\python.exe -m pytest tests/test_postgres.py -q`。测试账号需 CREATE SCHEMA 权限；每个测试只新建和清理自身 UUID schema，不使用 public。不要把生产库当测试环境。
- 创建与重启读取、需求修订、同一候选重复接受、两个候选竞争接受、过时候选拒绝、失败记录和历史读取。
- 用真实地图与模型跑一条两天行程，再修改出发时间，检查地点/预约/交通/住宿边界。

没有真实服务时这些项保持待验，不因依赖安装成功就写成通过。仅换 PostgreSQL 并未提供登录鉴权、多租户、TLS 网关、备份恢复或后台可靠任务，当前应用仍是本地单用户实验。
