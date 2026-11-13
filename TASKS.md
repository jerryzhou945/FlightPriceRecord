# 任务清单

每个里程碑 = 一个分支 + 一个 PR；每个小任务 = 至少一个 commit。

## M0 项目初始化（直接提交到 main）
- [x] 调研开源项目与携程接口（结论见 docs/architecture.md）
- [x] UI 原型 `prototype/ui-demo.html`
- [x] 架构文档、开发规则 CLAUDE.md、任务清单
- [x] pyproject、ruff、pytest、GitHub Actions CI、配置模板

## M1 数据采集 `feat/m1-collector`
- [x] 数据库建表与连接（`db.py`、`models.py`）
- [x] 携程 15380 日历数据源：单程 + 往返缓存，解析与错误分类
- [x] 录制响应 fixture 与解析测试
- [x] 数据读写层 `repository.py`（最新价、历史序列）
- [x] `collector.py` + CLI `farerecord collect`（记录 fetch_run，失败不中断其他航线）
- [x] 配置加载 `config.py`（config.yaml + .env）

## M2 分析 `feat/m2-analysis`
- [x] 弹性行程搜索：窗口 × 天数矩阵、Top N、理想天数微调建议
- [x] 组合历史走势（按 run 计算 out+ret）
- [x] 统计：近 N 天分位、最低点、24h 变化
- [x] CLI `farerecord flex`（终端输出最便宜组合）

## M3 API `feat/m3-api`
- [x] FastAPI 应用与静态文件挂载，CLI `farerecord serve`
- [x] `/api/routes`、`/api/calendar`、`/api/flex`、`/api/history`、`/api/status`
- [x] `/api/manual-quote`、`/api/collect`
- [x] API 测试（TestClient + 临时数据库）

## M4 前端 `feat/m4-web`
- [x] 从原型拆出 `web/`（index.html、styles.css、app.js），ECharts 本地化
- [x] 接入真实 API：总览 KPI、走势、低价日历、弹性行程热力图与 Top 列表
- [x] 航线 / 窗口 / 天数可调；选中组合联动走势
- [x] 空数据、抓取失败、加载中状态；深浅色；手机布局
- [x] 手动核价录入、立即抓取按钮

## M5 微信提醒 `feat/m5-alerts`
- [x] 推送通道：WxPusher、PushPlus、控制台（测试用）
- [x] 规则：目标价、历史新低、24h 跌幅、抓取连续失败
- [x] 去重与夜间免打扰
- [x] CLI `farerecord notify-test`；collector 抓取后自动评估规则

## M6 调度与使用文档 `feat/m6-schedule`
- [x] Windows 任务计划注册/删除脚本（每天 4 次）
- [x] README：安装、配置、微信绑定、日常使用

## M7 云端运行（可选）`feat/m7-cloud`
- [ ] GitHub Actions 定时抓取，数据库存 `data` 分支
- [ ] 导出静态快照，GitHub Pages 发布只读看板
