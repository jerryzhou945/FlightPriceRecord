# CLAUDE.md — 航价记开发规则

项目：携程机票价格记录与弹性行程比价工具。架构见 [docs/architecture.md](docs/architecture.md)，任务进度见 [TASKS.md](TASKS.md)。

## 常用命令

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev,desktop]"   # 安装（Windows）
.venv/Scripts/python -m pytest -q                                 # 测试
.venv/Scripts/python -m ruff check . && .venv/Scripts/python -m ruff format --check .
.venv/Scripts/farerecord collect          # 抓取一次
.venv/Scripts/farerecord app              # 打开桌面窗口（开着时每小时自动抓取）
.venv/Scripts/farerecord serve            # 浏览器里看 http://127.0.0.1:8765
```

## Git 工作流（必须遵守）

1. **大功能 = 一个分支 + 一个 PR**。TASKS.md 里每个里程碑（M1、M2…）是一个大功能。
   - 分支命名：`feat/m1-collector`、`fix/<简述>`、`docs/<简述>`、`chore/<简述>`
   - 不直接往 `main` 推功能代码（仓库初始化提交除外）
2. **小功能 = 一个 commit**。完成一个可独立验证的小步骤（一个模块、一个接口、一组测试）就提交一次，不要攒很多改动一起提交。
3. **提交信息**用 Conventional Commits：`feat: …`、`fix: …`、`test: …`、`docs: …`、`refactor: …`、`chore: …`，描述可用中文。结尾带协作者署名行。
4. **提交前**必须通过：`pytest -q`、`ruff check .`、`ruff format --check .`。测试不过不提交。
5. **PR 描述**包含：做了什么、怎么验证的、对应 TASKS.md 哪一项、已知限制。
6. **合并由用户完成**：Claude 开 PR 并确认 CI 通过，由用户审阅后 squash merge 并删除分支。Claude 不自行合并。
   后续里程碑可以基于未合并的分支继续开发，PR 的 base 设为上一个里程碑的分支（stacked PR）；上一个合并后 GitHub 会自动把 base 改为 `main`。
7. 合并后在 TASKS.md 勾选对应任务（可以包含在该 PR 的最后一个 commit 里）。

## 代码规则

- Python 3.12，类型标注，`ruff` 默认规则 + isort；行宽 100
- 标识符用英文；用户可见文字（UI、推送、CLI 输出）用中文
- 业务逻辑（flex、stats、rules、解析）必须有单元测试；网络请求一律可注入/可 mock，**测试不访问真实网络**
- 接口解析用录制的真实响应作为 fixture（`tests/fixtures/`），去掉无关字段
- 时间：数据库存 UTC ISO 字符串；日期（出发/返回日）存 `YYYY-MM-DD`；展示用北京时间
- 价格：统一人民币、含税总价、1 位成人、经济舱。往返价是按单程价推算的，必须标注为"预估"，并能看到"两张单程"的原始合计

## 抓取规则（重要）

- 只使用公开、无需登录的接口（当前是携程 15380 低价日历，含它自带的航司筛选）
- **不做反爬绕过**：不伪造设备指纹、不破解签名、不用代理池、不处理验证码
- 频率：定时抓取最快每 15 分钟 1 次（`collector.interval_minutes` ≥ 15，默认 30），每次请求之间间隔 ≥ 2 秒；航司识别是额外请求，按 `airline_refresh_minutes`（默认 120）节流，不随抓取频率增加
- 抓取失败要记录到 `fetch_run` 并可触发微信提醒，不能静默失败

## 安全与隐私

- `.env`、`config.yaml`、`data/`（数据库）不进仓库；只提交 `*.example`
- 推送 token、UID 只从环境变量读取，日志中不打印
- 仓库默认私有；改为公开前检查历史提交里没有密钥

## 文档

- 改动架构、数据模型、API 时同步更新 `docs/architecture.md`
- 新增命令或配置项时同步更新 `README.md` 和 `config.example.yaml`
