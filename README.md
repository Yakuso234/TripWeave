# TripWeave · 行程实验室

仓库：[Yakuso234/TripWeave](https://github.com/Yakuso234/TripWeave)。

当前是带中文使用页面的单日离线原型：填写需求、生成候选、检查固定预约和交通、修改出发时间、接受版本并恢复保存状态。

**固定模型脚本＋五个合成地图地点，不是已接通真实模型的旅行产品。** 用户已选择先完成离线实验。真实模型、高德、自由文本理解、两天行程和提醒尚未实现；填写.env不会自动启用真实调用。

## 启动（Windows PowerShell）

本机依赖已安装：

```powershell
cd C:\Users\yujia\Desktop\projects\TripWeave
.\start.ps1
```

打开 http://127.0.0.1:8765 。Ctrl+C停止。若脚本策略阻止执行，可直接运行：

```powershell
.\.venv\Scripts\python.exe -m tripweave.launcher --port 8765
```

重新安装环境：`uv --cache-dir .runtime/uv-cache sync --locked`。本机使用Python3.14.7和uv0.12.5，安装同步已实际运行；新机器需先安装Python3.14和uv，未逐台验证。端口占用时检查是否已有实例，或改用其他端口。仅监听127.0.0.1，本版无账户鉴权，供单用户本地实验。

## 演示

1. 默认上海、人民广场09:00出发、上海博物馆14:00–15:00预约，生成候选。
2. 展开实验过程，查看固定脚本注入错误后被检查器拦截并修订。这是测试注入，不是真实模型失败案例。
3. 接受候选，点击晚出门一小时，重新生成；预约仍是14:00。
4. 接受新版、刷新页面恢复；将出发时间改到14:30可观察冲突。

离线策略只支持页面的两种示例偏好和五个地点。Asia/Shanghai时区；结束边界不含返回出发点。路线为合成步行时间，景区内步行、营业时间、票价和餐饮未知。不会执行外部预约或支付。

## 代码入口

- tripweave/domain.py：数据合同、固定地点路线、规划脚本和确定性检查。
- tripweave/planner.py：PydanticAI工具循环、输出校验、反馈修订和退出上限。
- tripweave/app.py：API、SQLite事务、需求/行程版本检查。
- tripweave/static/：输入表单、行程展示、接受与恢复。
- docs/PROJECT_PLAN.md：规划；docs/adr/ADR-001-prototype-stack.md：选型证据。
- docs/evaluations/PROTOTYPE_QA.md：验证与限制。

数据保存在.runtime/tripweave.sqlite3，可用进程环境变量TRIPWEAVE_DATABASE指定路径。.env.example的其他字段是未来真实接入预留，本版不读取。密钥、个人数据和运行缓存不得提交。

## 检查

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check tripweave tests
.\.venv\Scripts\ruff.exe format --check tripweave tests
Get-Content tripweave/static/app.js -Raw | node --check
```

5个自动场景通过；浏览器实际检查生成、接受、修改。固定模型Token不当成真实用量，未知值为null。

## 端口与重复启动

`start.ps1` 默认使用8765。若该端口已运行同目录的TripWeave，脚本显示已有地址并退出，不再启动第二个进程。其他服务占用时明确报错，不结束其进程。需要换端口时运行 `.\start.ps1 -Port 8766`；仅检查可用性用 `.\start.ps1 -Port 8766 -CheckOnly`。启动检查不会自动扫描或占用其他项目端口。

## 来源与许可

未复制外部旅行项目代码，数据是自行定义的合成样本。PydanticAI、FastAPI等依赖遵循各自许可证，版本在uv.lock。本项目尚未选择对外发布许可证。
