# iQuant 桥策略（双桥：仿真 / 实盘）

在国信 iQuant 客户端内以**策略**方式运行的本地 HTTP 桥：Core（main/）通过 HTTP
调 `/order` `/positions` `/account` `/orders` `/deals` `/quote` 实现实盘下单与行情拉取。

- 设计依据：[docs/plans/0009-iquant-http-bridge.md](../../docs/plans/0009-iquant-http-bridge.md)
- 订单状态机 / 成交回报回填：[docs/plans/0011-order-sync-deal-backfill.md](../../docs/plans/0011-order-sync-deal-backfill.md)

## 双桥结构

实盘分两个正交维度，桥只承载其中一个：

| 维度 | 取值 | 由谁决定 | 说明 |
|---|---|---|---|
| **仿真 / 实盘**（账号环境） | 仿真(虚拟资金) / 实盘(真实资金) | **账号 / 桥** | 一桥一账号一端口，Core 按 `session.mode` 路由 |
| **模拟 / 实盘**（下单与否） | 模拟(只出信号) / 实盘(真实下单) | **iQuant 客户端启动按钮** | 与桥、与 `DRY_RUN` 无关 |

两个维度正交，形成 2×2 组合：仿真·模拟、仿真·实盘(虚拟资金交易)、实盘·模拟、实盘·实盘(真实资金交易)。

| 桥文件 | 端口 | 账号 | session.mode |
|---|---|---|---|
| `iquant_bridge.py` | 8790 | 仿真账号（虚拟资金） | `simulation` |
| `iquant_bridge_live.py` | 8791 | 实盘账号（真实资金） | `live` |

> **两文件是独立自包含拷贝**：iQuant 通过把文件内容粘进客户端策略编辑器加载（非文件路径 import），故两桥不能 import 共享模块——`DRY_RUN`/白名单/限额/HTTP 层逻辑在两份文件中各有一份，**逻辑改动须手动同步两处**。差异仅在顶部 config 区（`PORT` / `ACCOUNT_DEFAULT`）。

## ⚠️ 模拟/实盘由 iQuant 启动按钮控制（与桥无关）

桥策略**必须用 iQuant 的「实盘交易」模式启动**才会真实下单——**模拟模式下 `passorder` 只出策略
信号、不发真实委托**（迅投硬规则，2026-08-10 真机验证）。用模拟模式跑起来一切正常、
行情/查询都能用，但 `/order` 永远不下单。上线前务必核对 iQuant 客户端右下角的交易模式。

此开关与桥文件内的 `DRY_RUN` **无关**：`DRY_RUN` 仅是开发期打印开关，即便 `DRY_RUN=False`，
以模拟模式启动仍只出信号。

## 部署步骤

1. 在 iQuant 客户端**新建两个策略**：
   - 策略 A：把 `iquant_bridge.py` 内容粘进去 → 仿真桥，监听 `127.0.0.1:8790`。
   - 策略 B：把 `iquant_bridge_live.py` 内容粘进去 → 实盘桥，监听 `127.0.0.1:8791`。
   - 两个文件均为**纯 ASCII、GBK 编辑器兼容**，勿加非 ASCII 字符。
2. 各自配置账号（见下「配置」）。
3. 两个策略**各以「实盘交易」模式运行**。`init` 阻塞主循环启动 HTTP 服务（`init` 不返回）。
4. Core 侧 `config.yaml` 的 `iquant_bridge.simulation.base_url` / `iquant_bridge.live.base_url`
   分别指向两个桥地址（默认 8790 / 8791，可配）。
5. 用 `live/scripts/` 下的验证脚本或直接 `curl /ping` 确认两个桥在线后再启动实盘 session。
   `GET /ping` 返回的 `account` 字段可用于核对各自绑定的账号。

## 配置

| 项 | 桥 | 说明 | 配置方式 |
|---|---|---|---|
| 账号 ID | 仿真桥 | `passorder`/查询用的仿真券商账号（虚拟资金） | 环境变量 `IQUANT_BRIDGE_ACCOUNT`，否则同目录 `.bridge_account` 文件（首行），否则回退 `ACCOUNT_DEFAULT` |
| 账号 ID | 实盘桥 | `passorder`/查询用的真实券商账号（真实资金） | 同上（实盘桥文件内的 `ACCOUNT_DEFAULT` 应为真实账号） |
| 股票白名单 | 两桥 | `ALLOWED_STOCKS`（空 = 不限制） | 策略代码内配置（生产建议配齐） |
| 试运行 | 两桥 | `DRY_RUN`（默认 False） | 策略代码内配置；True 只打印不发单（**开发开关，非模拟/实盘控制**） |
| 端口 | 仿真桥 | `PORT = 8790` | 写死在文件顶部 config 区 |
| 端口 | 实盘桥 | `PORT = 8791` | 写死在文件顶部 config 区 |

`IQUANT_BRIDGE_ACCOUNT` 环境变量与 `.bridge_account` 文件均可覆盖文件内写死的 `ACCOUNT_DEFAULT`，
便于换账号时不改代码。`.bridge_account` 文件放桥脚本同目录，部署时手动落盘（已被 `.gitignore` 忽略）。

> 账号写死在桥文件是为双桥文件自包含、避免移植路径问题（iQuant 粘贴加载，无环境变量/路径机制）。
> 仍保留 `load_account()` 覆盖能力，默认值仅为双桥各自自包含。

> 鉴权：两桥均绑 `127.0.0.1` loopback，单用户本机部署，不设 token 鉴权（防御在机器边界：
> 仅本机进程可达端口）。白名单、单笔限额、频率限制、审计日志仍强制执行。

### config.yaml 示例

```yaml
iquant_bridge:
  simulation:
    base_url: http://127.0.0.1:8790
  live:
    base_url: http://127.0.0.1:8791
```

Core 按 `session.mode` 选桥：`simulation` → 8790，`live` → 8791。端口在此可配（如改用其他端口，
需同步改对应桥文件顶部 `PORT`）。

## 接口（两桥一致）

| 端点 | 说明 |
|---|---|
| `GET /ping` | 心跳 + 桥状态（含 `port`/`account`/`dry_run`，可核对账号归属） |
| `POST /order` | 下单（幂等 `order_id` + 白名单 + 限额 + 频率限制 + 审计） |
| `GET /positions` | 持仓（`instrument`/`exchange`/`volume`/`available`/`yesterday`/`on_road`/`market_value`） |
| `GET /account` | 资金 |
| `GET /orders?order_id=` | 委托查询 |
| `GET /deals?order_id=` | 成交回报（回填用 `m_strOrderRef`） |
| `GET /quote?code=&period=&count=` | 1m/5m/1d bar（缓存） |

## 环境约束（已真机验证）

- iQuant 客户端线程不可靠、`handlebar` 仅启动回放驱动 → 桥用 `init` 阻塞主循环
  单线程事件循环（24-39ms 响应）。
- Python 3.6，纯标准库实现 HTTP 层，无第三方依赖。
- 5m 是桥端原生周期可直接拉；1w/1mon 桥端 `xtdata` 拉不到，由 Core 侧通达信注入。
