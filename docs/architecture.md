# 航价记 · 项目架构

## 目标

给自己（以及需要的人）一个订机票前的价格参考工具：

1. 持续记录携程价格，看**近期波动**和**历史低点**
2. **弹性行程**：给定出发窗口（如 12 月）和游玩天数范围（如 25–35 天），找出最便宜的去回组合，并告诉你"微调天数能省多少"
3. 低价时**微信提醒**

## 数据源调研结论（2026-11-10 实测）

| 接口 | 结果 | 用途 |
|---|---|---|
| 携程 H5 低价日历 `soa2/15380 FlightIntlAndInlandLowestPriceSearch`（单程） | ✅ 可用。一次请求返回某方向未来约 366 天每天的最低含税价 | **主数据源**：去程、回程各 1 次请求即可覆盖全部日期 |
| 同一接口（往返模式） | ⚠️ 只返回缓存的短途组合（停留 ≤ 14 天），不包含长途组合；请求里的日期不影响结果，换 flag / searchType / calendarSelections 也拿不到长途（2026-11-11 复测） | 短途行程直接用真实往返价；同时用来校准长途组合的往返预估 |
| 携程网页版往返订票页 `flights.ctrip.com/online/list/round-...` | ⚠️ 无头浏览器被风控拦截（whaleguard block）；普通浏览器窗口（WebView2，不改任何指纹）能正常打开，页面上直接显示这两个日期的最低往返含税价（2026-11-11 实测 12/28→1/20：¥7,139 土耳其航空，约 8 秒加载完） | **固定行程的真实往返价**：只打开用户固定的那一个行程的页面，读页面上已显示的价格；不调用它背后的搜索接口（那需要签名）。要求登录或出现验证码时停止 |
| 携程 H5 往返列表（Playwright） | ❌ 风控 wasm + "服务器发生异常" | 不使用 |

原则：**不做反爬绕过、不登录账号、不用代理池**。读往返订票页用的是和用户自己打开页面一样的浏览器窗口，页面在窗口不可见时不会加载，所以窗口放在屏幕外而不是隐藏。默认每小时抓取，每条航线读取去程、回程和往返缓存。航司识别按需额外读取候选航司日历，每个方向默认最多 8 家。

除了固定行程以外，往返价格采用**单程组合估价**：`去程单程最低价(出发日) + 回程单程最低价(返回日)`。界面上始终标注"估价"，并提供"去携程核价"链接和"手动录入真实往返价"的入口。短途组合如果在往返日历缓存里存在，就同时展示真实往返价。

参考项目：
- [davidwushi1145/flightAlert](https://github.com/davidwushi1145/flightAlert)：使用同一个 15380 日历接口，单航线 + 桌面 GUI，不可商用
- [zhanglong-ustc/flight-price-monitor](https://github.com/zhanglong-ustc/flight-price-monitor)：Playwright 多平台 + GitHub Actions + Server酱，国内单程为主

## 总体结构

```
          ┌──────────────┐   默认每小时（桌面定时器 / Windows 任务计划）
          │  collector   │──────────────────────────────┐
          └──────┬───────┘                              │
                 │ sources/ctrip_calendar.py            │
                 ▼                                      ▼
        ┌─────────────────┐   写入            ┌──────────────────┐
        │  携程 15380 接口 │ ───────────────▶ │  SQLite (data/)   │
        └─────────────────┘                   └────────┬─────────┘
                                                       │ 读取
                       ┌───────────────────────────────┼──────────────────┐
                       ▼                               ▼                  ▼
               ┌──────────────┐               ┌───────────────┐   ┌──────────────┐
               │ analysis/    │               │ alerts/       │   │ api/ FastAPI │
               │ flex · stats │◀──────────────│ rules + 推送   │   │ /api/*       │
               └──────────────┘               └──────┬────────┘   └──────┬───────┘
                                                     ▼                   ▼
                                              WxPusher / PushPlus   web/ 静态前端
                                                (微信)              (黑白玻璃 UI + ECharts)
```

## 技术选型

| 部分 | 选择 | 理由 |
|---|---|---|
| 语言 | Python 3.12 | 抓取和数据处理简单，参考项目也是 Python |
| HTTP | `httpx` | 同步/异步都支持，方便测试时 mock |
| 存储 | SQLite（标准库 `sqlite3`） | 单文件、零运维，数据量很小 |
| Web | FastAPI + Uvicorn | 同时提供 JSON API 和静态前端 |
| 前端 | 原生 ES Modules + ECharts（本地 vendor） | 不需要构建步骤，和 UI demo 一脉相承 |
| 通知 | Windows 桌面通知，可叠加 WxPusher 或 PushPlus | 在提醒页调整规则 |
| 配置 | `config.yaml`（航线、提醒规则）+ `.env`（密钥） | 密钥不进仓库 |
| 调度 | Windows 任务计划程序；可选 GitHub Actions | 本地优先，云端可选 |
| 质量 | pytest + ruff，GitHub Actions CI | 每个 PR 自动检查 |

## 目录结构

```
FlightPriceRecord/
├── farerecord/                  # Python 包
│   ├── config.py                # 读取 config.yaml / .env
│   ├── db.py                    # 建表、迁移、连接
│   ├── models.py                # dataclass：Quote、Run、Trip …
│   ├── repository.py            # 数据读写（查询最新价、历史序列）
│   ├── sources/
│   │   ├── base.py              # PriceSource 接口
│   │   ├── ctrip_calendar.py    # 携程 15380 日历（单程 + 往返缓存）
│   │   └── ctrip_page.py        # 携程往返订票页：在浏览器窗口里读页面上的往返价
│   ├── collector.py             # 一次抓取：所有关注航线 → 入库 → 触发提醒
│   ├── realprice.py             # 固定行程：到点判断、核价并记录、桌面端后台监测线程
│   ├── analysis/
│   │   ├── flex.py              # 弹性行程：窗口 × 天数 → 组合 / 排名 / 微调建议
│   │   └── stats.py             # 历史分位、最低点、24h 变化
│   ├── alerts/
│   │   ├── rules.py             # 目标价、历史新低、单日跌幅、免打扰、去重
│   │   └── notifiers.py         # WxPusher / PushPlus / 控制台
│   ├── api/app.py               # FastAPI：/api/* + 挂载 web/
│   ├── desktop.py               # WebView2 窗口和本机 HTTP 服务
│   ├── scheduler.py             # 桌面窗口打开期间按间隔抓取
│   └── cli.py                   # farerecord app | collect | serve | flex | notify-test | init-db
├── web/                         # 前端（index.html, app.js, styles.css, vendor/echarts）
├── tests/                       # pytest（接口响应用录制的 JSON fixture）
├── scripts/                     # Windows 任务计划注册脚本等
├── docs/                        # architecture.md、data-source.md
├── prototype/ui-demo.html       # UI 原型
├── config.example.yaml
├── .env.example
├── TASKS.md                     # 任务清单（随进度勾选）
└── CLAUDE.md                    # 开发规则
```

## 数据模型

```sql
fetch_run      (id, source, started_at, finished_at, status, error, n_quotes)
oneway_quote   (run_id, origin, dest, depart_date, price, observed_at, airline, airline_price)
rt_quote       (run_id, origin, dest, depart_date, return_date, price, observed_at)  -- 往返缓存
manual_quote   (id, origin, dest, depart_date, return_date, price, note, observed_at) -- 手动核价
real_quote     (id, origin, dest, depart_date, return_date, price, airline, observed_at, seats_left, next_price) -- 携程往返订票页读到的实价、余票提示、下一档价格
alert_log      (id, rule, dedupe_key, price, channel, status, message, sent_at)
price_watch    (id, origin, dest, depart_date, return_date, target_price, enabled, created_at)
setting        (key, value)   -- 界面覆盖的配置（JSON）
daily_price    (kind, origin, dest, depart_date, return_date, observed_date, low, high, n) -- 每日留档
```

- 价格统一为**人民币含税总价、1 位成人、经济舱**
- `observed_at` 统一存 UTC ISO 字符串，展示时转北京时间
- 任意组合的历史走势 = 对每次抓取（run）取 `(out[d] + ret[r]) × 校准比例`，不需要单独存组合
- `rt_calibration (run_id, origin, dest, ratio, samples)`：每次抓取算出的"真实往返价 ÷ 两张单程之和"（只用停留 ≥ 10 天的缓存组合，至少 8 个样本）
- 界面里添加的航线存在 `setting`：`routes.extra → [{origin, dest}]`，和 config.yaml 的航线合并（`settings.effective_config`），弹性窗口沿用第一条航线，最多共 4 条；删除只停止抓取、取消固定行程、停用到价提醒，已抓到的价格保留
- 固定的行程存在 `setting`：`pinned_trip:CHI-SHA → {depart, return}`；最近一次核价的结果存在 `real_check:CHI-SHA → {at, trip, status: ok|blocked|empty|error|running, message, price}`
- `fetch_run` 超过 `retention_days` 的记录连同报价一起清理（`real_quote` 也按同样的期限清理）；停在 running 超过 15 分钟的记录在下次抓取时标为 failed
- `daily_price` 是每天的价格留档：`kind=oneway` 为日历单程最低价，`kind=real` 为携程往返实价；`observed_date` 是北京时间日期，`low/high/n` 是当天的最低、最高、次数。写报价时顺手合并，**不随旧抓取清理**，用于和往年同期对比（v7 升级时用已有报价回填）
- 航司日历价不高于当日最低价的 101% 时视为匹配。仅查到更高的单家报价时，API 返回 `exact: false` 和该航司起价，估价仍使用当日最低价。v3 数据库升级保留已知航司，原先找不到匹配的日期可重新查询。

## 核心算法

### 购买建议（`analysis/advice.py`）

携程不公开历史价格，也没有免费的往年机票价格数据源，所以不是训练出来的预测模型，而是透明的经验规则 + 本机数据：

- **离出发天数**：≥ 90 天且非旺季 → 可以再等；出发前 21 / 14 / 7 天（以及旺季的 42 天、平季的 28 天）是常见的涨价节点；不到 21 天 → 尽快买
- **旺季**：出发或返回落在 12/15–1/5、春节前 14 天到后 7 天、6/10–8/31 → 建议最晚购买日为出发前 42 天，否则 28 天
- **价格位置**：现价不高于监测以来最低价的 101%，且离出发不到 63 天 → 建议现在买；否则给"值得等的价位"（最低价与现价 × 97% 取低）和最晚购买日
- **跳变**：相邻两次读数变化 ≥ 1.5% 且 ≥ ¥80 记为一次跳涨/跳水
- **往年同期**：`daily_price` 里去年出发日 ±3 天的去程，从"离出发同样天数"到出发前 21 天的涨跌
- **余票**：携程往返页只在座位很少时显示"剩 N 张"（v8 起存入 `real_quote.seats_left`，没提示为空），同时记下页面上比最低价高的下一档价格 `next_price`（优先同一家航司）。现价这一档剩 ≤ 3 张时直接建议现在买；首页走势用红点标出有余票提示的读数、用点线画出下一档价格
- **附近日期**：出发、返回各 ±3 天，按"现价 ÷ 两张单程之和"折算其他组合，省 ≥ max(¥100, 1.5%) 才列出（每个出发日一条，最多 3 条）


**弹性行程搜索**（`analysis/flex.py`）

```
输入：出发窗口 [start, end]、天数范围 [min, max]、理想天数 ideal
对每个出发日 d、每个天数 s：
    r = d + s
    估价 = out_latest[d] + ret_latest[r]
    若往返缓存中有 (d, r) 的真实价，也一起返回
输出：全部组合矩阵（热力图）、Top N、最低组合、
      "正好 ideal 天最低价" vs "全局最低价"的差额（微调建议）
```

最多 31 × 31 ≈ 1000 个组合，纯内存计算，毫秒级。

**往返预估**（`analysis/calibration.py`）

两张单程永远买得到，但往返票通常更便宜（实测 12/28→1/19：两张单程 ¥8,539，携程真实往返 ¥7,132）。长途真实往返价拿不到，于是用比例校准：

```
比例来源（按优先级）：
  1. 用户 3 天内手动录入的真实往返价 ÷ 当时的两张单程之和（中位数）
  2. 最近 48 次抓取里 rt_calibration 的中位数
  3. 没有数据时为 1（不校准）
比例限制在 [0.6, 1.0]；Trip.estimate = round(两张单程 × 比例)
```

比例对所有日期一视同仁且变化很慢，走势图的起伏只反映单程价的真实变化。提醒按预估价判断，但去重键用不含校准的价格（`Trip.raw_price`），比例漂移不会让同一个报价重复提醒。

**固定行程的往返实价**（`realprice.py` + `sources/ctrip_page.py`）

```
到点判断：上次核价之后过了 max(15 分钟, 抓取间隔)；上次被要求登录/验证则要过 6 小时；换了日期立即核
核价：屏幕外开一个浏览器窗口 → 打开往返订票页 → 每 2 秒读一次页面，等搜索进度条走完且连续两次读到的价格相同（最多 45 秒）。
      携程边搜边显示，先出的价格不是最终价（实测先 ¥7,133，6 秒后变 ¥7,125）
取价：日期条上当前日期的"低"价（整页最低）；没有就取已渲染航班行里的最低价；航司取最低价那一行的
结果：成功 → real_quote 加一条；失败 → real_check 记下原因（blocked / empty / error），首页显示
```

桌面端开着时由 `RealPriceMonitor` 线程每 30 秒看一眼是否到点；没开着时任务计划里的 `collect` 会临时启动一个屏幕外窗口核价（`realprice.run_standalone`，180 秒看门狗防止卡住任务计划）。两边用 `real_check` 里的时间互相避让。读到的实价 3 小时内视为"当前价"，到价提醒直接按它判断。实价只用于这个行程本身，不参与其它组合的预估校准（单个日期组合的比例波动太大）。

**价格位置**：当前价在近 N 天所有记录里的分位，"比 X% 的时间便宜"。不做价格预测。

**提醒规则**：目标价、历史新低、24h 跌幅超过阈值；同一条规则 + 同一组合 + 同一价格只推一次；夜间免打扰时延后到早上。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/routes` | 关注的航线与配置 |
| GET | `/api/cities` | 界面可选的城市（携程城市三字码，按北美 / 中国分组） |
| POST / DELETE | `/api/routes`、`/api/routes/{origin}/{dest}` | 添加航线（换个出发地）；删除界面里添加的航线（config.yaml 里的删不了） |
| GET | `/api/calendar?origin&dest&month` | 某方向某月每天最新价 + 当月分档 |
| GET | `/api/flex?origin&dest&start&end&min&max&ideal` | 弹性行程矩阵、Top N、微调建议 |
| GET | `/api/history?origin&dest&depart&return&days` | 组合（或单程）价格历史序列 + 统计；`real` 里是携程往返实价的序列、统计和最近一次核价结果 |
| GET | `/api/advice?origin&dest&depart&ret` | 买还是等：结论（buy / watch / wait）、理由、建议最晚购买日、涨价节点、值得等的价位、监测期间的跳变、附近更便宜的日期、去年同期（有留档时） |
| PUT | `/api/pinned` | 固定（或取消固定）首页的行程 |
| POST | `/api/real-check` | 让桌面端马上去携程往返订票页核一次价（没有桌面端时返回 409） |
| GET | `/api/status` | 最近抓取记录、成功率、下次抓取时间 |
| POST | `/api/manual-quote` | 手动录入真实往返价 |
| POST | `/api/collect` | 立即抓取一次 |
| GET / PUT | `/api/alerts`、`/api/alerts/settings` | 读取提醒状态、保存界面设置 |
| POST | `/api/alerts/test` | 测试当前通知通道 |
| PUT | `/api/alerts/wechat` | 界面里配置微信推送；口令写入 `.env`，不存数据库、不经接口返回 |
| PUT | `/api/settings/collector` | 修改抓取间隔 |
| POST / PATCH / DELETE | `/api/watches`、`/api/watches/{id}` | 创建、修改、删除组合到价提醒 |

## 部署方式

1. **本地**：`farerecord app` 在回环地址的随机端口启动服务，WebView2 和浏览器共用 `web/` 页面。桌面定时器与「立即抓取」共用进程锁。桌面定时器和 Windows 任务计划的 `collect --if-due` 在 SQLite 写事务内检查间隔并登记抓取，网络请求在提交之后进行。浏览器模式使用 `farerecord serve --open`。
2. **云端（可选规划 M7）**：GitHub Actions 定时抓取、独立数据分支和 Pages 静态快照见任务清单。

## 风险

- 携程接口变更或开始限流 → 抓取失败时微信提醒，解析逻辑集中在一个文件里，方便修
- 携程往返订票页改版（类名变化）或开始要求登录/验证 → 读不到时如实显示原因并退回预估，不绕过；解析集中在 `ctrip_page.py`（`EXTRACT_JS` + `parse_snapshot`）
- 往返预估 ≠ 真实往返价 → 界面标注"预估"并展示两张单程的原始合计和校准比例，提供核价链接；手动录入的实价会反过来校准预估
- 接口只返回每天的最低价，不含航班号和中转信息 → 在"去携程核价"时查看具体航班
