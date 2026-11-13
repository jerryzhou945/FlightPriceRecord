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
- [x] Windows 任务计划注册/删除脚本（M8 起默认每 60 分钟检查）
- [x] README：安装、配置、微信绑定、日常使用

## M7 云端运行（可选）`feat/m7-cloud`
- [ ] GitHub Actions 定时抓取，数据库存 `data` 分支
- [ ] 导出静态快照，GitHub Pages 发布只读看板

## M8 Windows 桌面端 `feat/m8-desktop`
- [x] WebView2 独立窗口、`farerecord app` 和桌面快捷方式安装脚本
- [x] 窗口打开时自动抓取，和 Windows 任务计划同时到点时仅登记一次
- [x] 组合到价提醒、桌面通知、界面设置与提醒测试入口
- [x] 日历、弹性行程和提醒区分匹配航司与参考航司起价，兼容旧数据库
- [x] 更新安装、定时抓取和数据模型说明

## M9 往返预估与首页重排 `feat/m9-roundtrip`
- [x] 往返预估：用携程短途真实往返票校准"两张单程之和"，手动核价优先；提醒按预估判断
- [x] 抓取间隔默认 30 分钟、最快 15 分钟，界面可改；航司识别单独节流
- [x] 自动清理 30 天以前的抓取；被中断的抓取不再卡在"进行中"
- [x] 首页以价格走势为主，行程明细 / 低价日历可切换
- [x] 弹性行程热力图红绿配色；航司中文简称色标
- [x] 界面内扫码配置微信推送（WxPusher 极简推送 / PushPlus）

## M10 固定行程与携程往返实价 `feat/m10-pinned-real-price`
- [x] 首页可固定一个具体的去回日期（不受弹性窗口限制），随时改日期或取消固定
- [x] 固定的行程定时从携程往返订票页读真实往返价（真实浏览器窗口，不调搜索接口；遇登录/验证码即停）
- [x] 首页大数字和走势用携程往返实价，推算的预估和两张单程合计留作对照
- [x] 实价用于到价提醒；任务计划在桌面端没开时也会核价
- [x] 等携程页面搜索完成、价格稳定后才读取；实价读取间隔最快 15 分钟

## M11 购买建议与价格留档 `feat/m11-buy-advice`
- [x] 每日价格留档 `daily_price`（单程 + 往返实价），不随旧抓取清理，旧数据回填
- [x] 购买建议：离出发天数、旺季、监测以来的位置与跳涨 → 买 / 限期等 / 再等，最晚购买日与涨价节点
- [x] 附近 ±3 天更便宜的组合（按现价折算）、一键按建议价位设到价提醒
- [x] 首页"买还是等"卡片，手机布局
- [ ] 攒满一年留档后验证"去年同期"对比（2027 年 11 月）

## M12 可选出发地 `feat/m12-origins`
- [x] 界面添加 / 删除航线（城市列表 + 自填三字码），和 config.yaml 合并，最多 4 条
- [x] 新航线参与抓取、固定行程、携程实价、到价提醒；删除后停用相关提醒
- [x] 微信推送默认 PushPlus（微信里收），注明 WxPusher 需要装 App

## M13 余票提示 `feat/m13-seats`
- [x] 从携程往返页读"剩 N 张"和下一档价格（只读页面已显示的内容），存入 `real_quote`
- [x] 购买建议参考余票：剩 ≤ 3 张建议现在买，说明下一档价格
- [x] 首页价格旁显示余票；走势图标出余票提示、画出下一档价格
