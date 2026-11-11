# 航价记 FlightPriceRecord

记录携程机票价格、找弹性日期里最便宜的往返组合、低价时微信提醒。

- **价格走势**：每天自动记录，看近期波动和历史低点
- **低价日历**：某方向每天的最低含税价
- **弹性行程**：比如"12 月出发、玩 25–35 天"，算出最便宜的去回组合，并告诉你"多玩/少玩几天能省多少"
- **微信提醒**：低于目标价、历史新低、大幅降价时推送

> 往返价目前是"去程单程 + 回程单程"的估价（携程往返列表页不允许自动化访问）。下单前请点"去携程核价"确认。详见 [docs/architecture.md](docs/architecture.md)。

UI 原型：[prototype/ui-demo.html](prototype/ui-demo.html)

## 开发状态

见 [TASKS.md](TASKS.md)。完整安装与使用说明会在 M6 补齐。

## 快速开始（开发中）

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
cp config.example.yaml config.yaml
cp .env.example .env
.venv/Scripts/python -m pytest -q
```

## 声明

仅供个人查询价格参考，请遵守携程服务条款，控制抓取频率，不要用于商业用途。
