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
| 同一接口（往返模式） | ⚠️ 只返回缓存的短途组合（停留 ≤ 14 天），不包含长途组合 | 辅助数据：短途行程直接用真实往返价 |
| 携程网页版往返列表 `flights.ctrip.com/online/list/...` | ❌ 无头浏览器被风控拦截（whaleguard block） | 不使用；只生成链接让用户手动核价 |
| 携程 H5 往返列表（Playwright） | ❌ 风控 wasm + "服务器发生异常" | 不使用 |

原则：**不做反爬绕过、不登录账号、不用代理池**。默认每小时抓取，每条航线读取去程、回程和往返缓存。航司识别按需额外读取候选航司日历，每个方向默认最多 8 家。

因此往返价格采用**单程组合估价**：`去程单程最低价(出发日) + 回程单程最低价(返回日)`。界面上始终标注"估价"，并提供"去携程核价"链接和"手动录入真实往返价"的入口。短途组合如果在往返日历缓存里存在，就同时展示真实往返价。

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
│   │   └── ctrip_calendar.py    # 携程 15380 日历（单程 + 往返缓存）
│   ├── collector.py             # 一次抓取：所有关注航线 → 入库 → 触发提醒
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
alert_log      (id, rule, dedupe_key, price, channel, status, message, sent_at)
price_watch    (id, origin, dest, depart_date, return_date, target_price, enabled, created_at)
setting        (key, value)   -- 界面覆盖的配置（JSON）
```

- 价格统一为**人民币含税总价、1 位成人、经济舱**
- `observed_at` 统一存 UTC ISO 字符串，展示时转北京时间
- 任意组合的历史走势 = 对每次抓取（run）取 `out[d] + ret[r]`，不需要单独存组合
- 航司日历价不高于当日最低价的 101% 时视为匹配。仅查到更高的单家报价时，API 返回 `exact: false` 和该航司起价，估价仍使用当日最低价。v3 数据库升级保留已知航司，原先找不到匹配的日期可重新查询。

## 核心算法

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

**价格位置**：当前价在近 N 天所有记录里的分位，"比 X% 的时间便宜"。不做价格预测。

**提醒规则**：目标价、历史新低、24h 跌幅超过阈值；同一条规则 + 同一组合 + 同一价格只推一次；夜间免打扰时延后到早上。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/routes` | 关注的航线与配置 |
| GET | `/api/calendar?origin&dest&month` | 某方向某月每天最新价 + 当月分档 |
| GET | `/api/flex?origin&dest&start&end&min&max&ideal` | 弹性行程矩阵、Top N、微调建议 |
| GET | `/api/history?origin&dest&depart&return&days` | 组合（或单程）价格历史序列 + 统计 |
| GET | `/api/status` | 最近抓取记录、成功率、下次抓取时间 |
| POST | `/api/manual-quote` | 手动录入真实往返价 |
| POST | `/api/collect` | 立即抓取一次 |
| GET / PUT | `/api/alerts`、`/api/alerts/settings` | 读取提醒状态、保存界面设置 |
| POST | `/api/alerts/test` | 测试当前通知通道 |
| POST / PATCH / DELETE | `/api/watches`、`/api/watches/{id}` | 创建、修改、删除组合到价提醒 |

## 部署方式

1. **本地**：`farerecord app` 在回环地址的随机端口启动服务，WebView2 和浏览器共用 `web/` 页面。桌面定时器与「立即抓取」共用进程锁。桌面定时器和 Windows 任务计划的 `collect --if-due` 在 SQLite 写事务内检查间隔并登记抓取，网络请求在提交之后进行。浏览器模式使用 `farerecord serve --open`。
2. **云端（可选规划 M7）**：GitHub Actions 定时抓取、独立数据分支和 Pages 静态快照见任务清单。

## 风险

- 携程接口变更或开始限流 → 抓取失败时微信提醒，解析逻辑集中在一个文件里，方便修
- 单程组合 ≠ 真实往返价 → 界面明确标注，提供核价链接和手动录入
- 接口只返回每天的最低价，不含航班号和中转信息 → 在"去携程核价"时查看具体航班
