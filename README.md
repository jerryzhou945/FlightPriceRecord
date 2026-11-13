# 航价记 FlightPriceRecord

记录携程机票价格，找出弹性日期里最便宜的往返组合，低价时推送到微信。

- **价格走势**：每天自动记录，看近期波动和历史低点
- **低价日历**：某个方向每天的最低含税价，点日期直达携程
- **弹性行程**：比如"12 月出发、玩 25–35 天"，算出最便宜的去回组合，并告诉你多玩或少玩几天能省多少
- **微信提醒**：低于目标价、出现历史新低、24 小时大幅降价、抓取连续失败时推送

> **关于往返价**：携程的往返列表页不允许自动化访问，所以这里的往返价是"去程单程最低价 + 回程单程最低价"的**估价**。实际往返票可能更便宜，也可能更贵。下单前请点"去携程核价"确认，查到的真实价格可以"录入实价"，会显示在走势图上作对照。原因详见 [docs/architecture.md](docs/architecture.md)。

UI 原型：[prototype/ui-demo.html](prototype/ui-demo.html)

## 安装（Windows）

需要 Python 3.12 或以上。

```bash
git clone https://github.com/jerryzhou945/FlightPriceRecord.git
cd FlightPriceRecord
python -m venv .venv
.venv\Scripts\pip install -e .
copy config.example.yaml config.yaml
copy .env.example .env
```

macOS / Linux 把 `.venv\Scripts\` 换成 `.venv/bin/`，`copy` 换成 `cp`。

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
.venv\Scripts\farerecord collect      # 抓取一次（约 10 秒）
.venv\Scripts\farerecord serve --open # 打开网页看板 http://127.0.0.1:8765
.venv\Scripts\farerecord flex         # 在终端里看最便宜的组合
```

也可以双击 `scripts\serve.cmd` 打开看板。

| 命令 | 作用 |
|---|---|
| `farerecord collect [--no-alerts]` | 抓取所有航线并评估提醒 |
| `farerecord serve [--port 8765] [--open]` | 启动网页看板 |
| `farerecord flex [--route CHI-SHA] [--start --end --min --max --ideal --top]` | 终端里的弹性行程搜索 |
| `farerecord notify-test` | 发一条测试提醒 |
| `farerecord init-db` | 创建数据库（一般不需要手动运行） |

## 每天自动抓取

在 PowerShell 里运行一次：

```bash
powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1
```

默认每天 00:00、06:00、12:00、18:00 各抓一次，日志写到 `logs\collect.log`。电脑关机错过的时间点会在开机后补跑。改时间：

```bash
powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 -Times 08:00,20:00
```

删除任务：

```bash
powershell -ExecutionPolicy Bypass -File scripts\unregister-task.ps1
```

## 微信提醒

默认用 [WxPusher](https://wxpusher.zjiecode.com)（免费）：

1. 微信扫码登录 WxPusher 后台，新建一个应用，复制 **APP_TOKEN**
2. 用微信扫应用的关注二维码，在"用户管理"里复制自己的 **UID**
3. 写进 `.env`：
   ```
   WXPUSHER_APP_TOKEN=AT_xxxxxxxx
   WXPUSHER_UID=UID_xxxxxxxx
   ```
4. `config.yaml` 里设置 `alerts.channel: wxpusher`
5. 运行 `farerecord notify-test`，微信收到测试消息就配置好了

也可以用 [PushPlus](https://www.pushplus.plus)：在 `.env` 填 `PUSHPLUS_TOKEN`，`alerts.channel: pushplus`。

提醒规则在 `config.yaml` 的 `alerts` 里调整：

| 配置 | 默认 | 说明 |
|---|---|---|
| `target_price`（航线下） | — | 弹性窗口内最低估价低于它就提醒 |
| `new_low` | `true` | 弹性窗口最低价创历史新低 |
| `drop_percent` | `5` | 和约 24 小时前相比跌幅超过 5%；`0` 关闭 |
| `fail_streak` | `2` | 连续 2 次抓取失败；`0` 关闭 |
| `quiet_hours` | `23:00-08:00` | 本机时间免打扰，期间的提醒在之后的抓取里补发 |

同一个价格只会提醒一次，不会重复轰炸。

## 网页看板

- **总览**：所选组合的估价和 24 小时变化、在近 14 天里的价格位置、窗口内去程和回程的单段最低价
- **价格走势**：所选组合 / 窗口最低两种曲线，标出最低点和目标价；手动录入的实价显示为菱形点
- **低价日历**：去程、回程每天的最低价，最便宜的 20% 标绿
- **弹性行程**：出发日 × 游玩天数的热力图，点格子看该组合的走势；Top 列表里如果携程有缓存的真实往返价，会一起显示
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

**为什么没有航班号和中转信息？** 低价日历接口只返回每天的最低价。点"去携程核价"可以看到具体航班。

**数据存在哪？** 本地 `data/farerecord.db`（SQLite），不会上传到任何地方。

## 声明

仅供个人查询价格参考。请遵守携程服务条款，保持较低的抓取频率（默认每天 4 次、每次 3 个请求），不要用于商业用途。
