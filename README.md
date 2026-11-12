# 航价记 FlightPriceRecord

记录携程机票价格，找出弹性日期里最便宜的往返组合，低价时推送到微信。

- **固定行程的真实往返价**：把你打算买的去回日期固定在首页，定时读取携程往返订票页上的往返含税价
- **价格走势**：每 30 分钟自动记录一次（最快 15 分钟），看近期波动和历史低点
- **低价日历**：某个方向每天的最低含税价，点日期直达携程
- **弹性行程**：比如"12 月出发、玩 25–35 天"，算出最便宜的去回组合，并告诉你多玩或少玩几天能省多少
- **提醒**：低于目标价、出现历史新低、24 小时大幅降价、某个组合到价时，桌面弹窗并可推送到微信

> **关于往返价**：有两种。
> 1. **携程往返实价**（只有固定在首页的那个行程有）：在首页点"改日期"，填去程和回程日期并勾选固定。之后桌面端会按抓取间隔（默认 30 分钟，最快 15 分钟）在后台打开一次携程的往返订票页，读出页面上显示的最低往返含税价——和你自己打开携程看到的是同一个数。这一步用的是普通的浏览器窗口（WebView2），不调用携程的搜索接口；如果携程要求登录或弹出验证码，程序会停下来并在首页提示，不做任何绕过，这时请点"去携程核价"自己看。需要安装桌面组件（`.[desktop]`）。
> 2. **往返预估**（其它所有组合）：携程公开的往返日历只有停留 14 天以内的缓存，拿不到长途行程的真实往返价，所以先把去程、回程的单程最低价相加，再乘上一个校准比例——这个比例来自携程近期的真实往返票（停留 10–14 天）和同一天两张单程的对比，实测在 0.85 左右。你在携程核过价后点"录入实价"，接下来 3 天预估会改按你的实价校准。下单前请以携程为准。细节见 [docs/architecture.md](docs/architecture.md)。

UI 原型：[prototype/ui-demo.html](prototype/ui-demo.html)

## 安装（Windows）

需要 Python 3.12 或以上。

```bash
git clone https://github.com/jerryzhou945/FlightPriceRecord.git
cd FlightPriceRecord
python -m venv .venv
.venv\Scripts\pip install -e ".[desktop]"
copy config.example.yaml config.yaml
copy .env.example .env
```

只使用网页看板和命令行时可用 `pip install -e .`。macOS / Linux 把 `.venv\Scripts\` 换成 `.venv/bin/`，`copy` 换成 `cp`。

## 配置

编辑 `config.yaml`：

```yaml
routes:
  - name: 芝加哥 ⇄ 上海
    origin: CHI          # 携程城市码：CHI 芝加哥、SHA 上海、BJS 北京、NYC 纽约、LAX 洛杉矶 …
    dest: SHA
    flex:
      start: 2026-12-01  # 出发窗口
      end: 2026-12-31
      min_days: 25       # 游玩天数范围
      max_days: 35
      ideal_days: 30     # 理想天数
    target_price: 7000   # 往返估价低于这个价就提醒
```

可以配置多条航线。城市码用的是**城市**而不是机场（CHI 包含 ORD 和 MDW）。

## 日常使用

```bash
.venv\Scripts\farerecord app          # Windows 桌面窗口，打开期间会自动抓取
.venv\Scripts\farerecord collect      # 手动抓取一次
.venv\Scripts\farerecord serve --open # 打开网页看板 http://127.0.0.1:8765
.venv\Scripts\farerecord flex         # 在终端里看最便宜的组合
```

桌面窗口使用 Microsoft Edge WebView2。运行 `powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1` 可在桌面和开始菜单创建「航价记」快捷方式。也可以双击 `scripts\serve.cmd` 在浏览器中打开看板。

| 命令 | 作用 |
|---|---|
| `farerecord app` | 打开桌面窗口并启动定时抓取 |
| `farerecord collect [--if-due] [--no-alerts] [--no-real-check]` | 抓取所有航线；可按间隔跳过，或关闭本次提醒。固定行程到点时顺便读一次携程往返实价（`--no-real-check` 可跳过） |
| `farerecord serve [--port 8765] [--open]` | 启动网页看板 |
| `farerecord flex [--route CHI-SHA] [--start --end --min --max --ideal --top]` | 终端里的弹性行程搜索 |
| `farerecord notify-test` | 发一条测试提醒 |
| `farerecord init-db` | 创建数据库（一般不需要手动运行） |

## 买还是等

首页走势下面的"买还是等"卡片会根据离出发的天数、是否旺季（圣诞元旦、春节、暑假）、监测以来的价格位置和跳涨，给出 **建议现在买 / 设提醒限期内买 / 可以再等等**，并标出建议的最晚购买日和常见的涨价节点（出发前 42 / 28 / 21 / 14 / 7 天）。点"降到 ¥… 时提醒我"可一键设到价提醒。卡片还会列出前后挪几天可能更便宜的日期（按单程日历推算，点开到携程核实）。

携程不公开往年价格，所以这不是基于往年数据的预测，而是经验规则；本机从安装起每天留档，第二年同期起会加上"去年同期"的对比。

## 定时抓取

抓取间隔默认 30 分钟，可以在界面"抓取"页改成 15 / 30 / 60 / 120 分钟（或改 `config.yaml` 的 `collector.interval_minutes`）。桌面窗口打开时会自己按间隔抓取；需要关掉窗口后继续记录时，在 PowerShell 里运行一次：

```bash
powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1
```

任务计划每 15 分钟运行一次 `collect --if-due`：到了设定的间隔才真的抓，否则立刻退出。日志写到 `logs\collect.log`。桌面端和任务计划共用数据库，同一时刻到点只抓一次。电脑关机错过的时间点会在开机后补跑。

固定在首页的行程：桌面窗口开着时由窗口在后台读取携程往返实价；窗口关着时由任务计划来读（临时开一个看不见的浏览器窗口，读完就关）。间隔跟抓取间隔走，最快 15 分钟。

每次抓取后会清理 30 天以前的记录（`collector.retention_days`，写 0 则一直保留）。每天的最低/最高价另外留档（`daily_price`），不会被清理，攒满一年后首页的购买建议会参考去年同期。

删除任务：

```bash
powershell -ExecutionPolicy Bypass -File scripts\unregister-task.ps1
```

## 微信提醒

默认使用 Windows 桌面通知。要让手机也收到，打开「提醒」页，在"微信推送"一行点**设置**：

- **WxPusher 扫码（推荐）**：用微信扫界面里的二维码，把收到的 `SPT_` 口令粘贴进去即可，不用注册或建应用
- **PushPlus**：在 [pushplus.plus](https://www.pushplus.plus) 微信登录后复制 token
- **WxPusher 应用**：在 [WxPusher 后台](https://wxpusher.zjiecode.com/admin/) 建应用，填 APP_TOKEN 和 UID

保存后会立刻发一条测试消息。口令只写进本机的 `.env`（不进仓库），也可以手动编辑 `.env`（见 `.env.example`）并在 `config.yaml` 里设置 `alerts.channel`。命令行测试：`farerecord notify-test`。

在「提醒」页可以为某个去回组合设目标价，也可以调整桌面通知、整条航线的目标价和免打扰时段。界面设置保存在本机数据库中，优先于 `config.yaml` 的默认值。

| 配置 | 默认 | 说明 |
|---|---|---|
| `target_price`（航线下） | — | 弹性窗口内最低估价低于它就提醒 |
| `new_low` | `true` | 弹性窗口最低价创历史新低 |
| `drop_percent` | `5` | 和约 24 小时前相比跌幅超过 5%；`0` 关闭 |
| `fail_streak` | `2` | 连续 2 次抓取失败；`0` 关闭 |
| `quiet_hours` | `23:00-08:00` | 本机时间免打扰，期间的提醒在之后的抓取里补发 |

同一个价格只会提醒一次，不会重复轰炸。

## 网页看板

- **总览**：以价格走势为主——所选组合的往返预估、较上次和 24 小时的涨跌、近 14 天的高低点；曲线可在"这个组合 / 窗口最低"之间切换，手动录入的实价显示为菱形点
- **行程明细**：去程单程 + 回程单程 = 两张单程合计，以及它是怎么折算成往返预估的；各段标出航司
- **低价日历**：去程、回程每天的单程最低价，最便宜的 20% 标绿；色标是出这个最低价的航司（淡色表示只是参考航司）
- **弹性行程**：出发日 × 游玩天数的热力图（绿便宜、红贵），点格子看该组合的走势；Top 列表里如果携程有缓存的真实往返价，会一起显示
- **提醒**、**抓取记录**：规则状态、最近推送、每次抓取结果

## 开发

```bash
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m ruff check . && .venv\Scripts\python -m ruff format --check .
```

开发规则见 [CLAUDE.md](CLAUDE.md)，任务进度见 [TASKS.md](TASKS.md)，架构见 [docs/architecture.md](docs/architecture.md)。

## 常见问题

**抓取失败 / 返回不是 JSON**：携程可能调整了接口或暂时限流。隔几个小时再试；连续失败会收到微信提醒。本工具不做任何反爬绕过。

**首页显示"携程要求登录或验证，已暂停自动核价"？** 携程对这次访问做了拦截。程序不会尝试绕过，6 小时后才再试一次；这期间首页显示的是上一次读到的价格和推算的预估，要看最新价请点"去携程核价"。

**为什么没有航班号和中转信息？** 低价日历接口只返回每天的最低价。点"去携程核价"可以看到具体航班。

**数据存在哪？** 本地 `data/farerecord.db`（SQLite），不会上传到任何地方。

## 声明

仅供个人查询价格参考。请遵守携程服务条款，保持较低的抓取频率。固定行程的往返实价每次只打开一个订票页，最快每 15 分钟一次。每条航线每次抓取有 3 个基础日历请求；航司识别是额外请求，默认每 2 小时才重新识别一次（`collector.airline_refresh_minutes`），每个方向最多查 `collector.airline_lookups` 家，设为 0 可关闭。
