# QMT 通道桥策略（双桥：仿真 / 实盘）

在 QMT 通道客户端内以**策略**方式运行的本地 HTTP 桥：Core（main/）通过 HTTP
调 `/order` `/positions` `/account` `/orders` `/deals` `/quote` 实现实盘下单与行情拉取。

- 设计依据：[docs/plans/0009-iquant-http-bridge.md](../../docs/plans/0009-iquant-http-bridge.md)
- 订单状态机 / 成交回报回填：[docs/plans/0011-order-sync-deal-backfill.md](../../docs/plans/0011-order-sync-deal-backfill.md)

## 双桥结构

实盘分两个正交维度，桥只承载其中一个：

| 维度 | 取值 | 由谁决定 | 说明 |
|---|---|---|---|
| **仿真 / 实盘**（账号环境） | 仿真(虚拟资金) / 实盘(真实资金) | **账号 / 桥** | 一桥一账号一端口，Core 按 `session.mode` 路由 |
| **模拟 / 实盘**（下单与否） | 模拟(只出信号) / 实盘(真实下单) | **QMT 客户端启动按钮** | 与桥、与 `DRY_RUN` 无关 |

两个维度正交，形成 2×2 组合：仿真·模拟、仿真·实盘(虚拟资金交易)、实盘·模拟、实盘·实盘(真实资金交易)。

| 桥文件 | 端口 | 账号 | session.mode |
|---|---|---|---|
| `iquant_bridge.py` | 8790 | 仿真账号（虚拟资金） | `simulation` |
| `iquant_bridge_live.py` | 8791 | 实盘槽账号（**当前仍是 QMT 纸面账号，非真实资金**） | `live` |

> **注意**：当前部署两个桥用的都是 QMT 纸面账号（`ACCOUNT_TYPE` 一 `STOCK` 一 `CREDIT`），
> 文件头注释里也写明「LIVE 只是桥的槽位名，不代表账户有真实资金」。换真实资金账户时只需
> 改对应桥文件顶部的 `ACCOUNT` 一行并重启策略，其余代码不动。

> **两文件是独立自包含拷贝**：QMT 通过把文件内容粘进客户端策略编辑器加载（非文件路径 import），故两桥不能 import 共享模块——`DRY_RUN`/白名单/限额/HTTP 层逻辑在两份文件中各有一份，**逻辑改动须手动同步两处**。
>
> 允许的差异**仅限**：`PORT`、`ACCOUNT`、`ACCOUNT_TYPE`、`TICK_TAG`、`/ping` 的 `service` 字段、控制台日志前缀（`[BRIDGE]` / `[BRIDGE-LIVE]`）与 `passorder` 的 strategyName（`iquant_bridge` / `iquant_bridge_live`），外加文件头注释里的账号/端口说明。除此之外逐字一致，由 `tests/test_bridge_tick.py::test_two_bridge_files_are_mirror_copies` 守护（去掉 config 区后按日志名归一化比对）。

## ⚠️ 模拟/实盘由 QMT 启动按钮控制（与桥无关）

桥策略**必须用 QMT 的「实盘交易」模式启动**才会真实下单——**模拟模式下 `passorder` 只出策略
信号、不发真实委托**（迅投硬规则，2026-08-10 真机验证）。用模拟模式跑起来一切正常、
行情/查询都能用，但 `/order` 永远不下单。上线前务必核对 QMT 客户端右下角的交易模式。

此开关与桥文件内的 `DRY_RUN` **无关**：`DRY_RUN` 仅是开发期打印开关，即便 `DRY_RUN=False`，
以模拟模式启动仍只出信号。

## 部署步骤

1. 在 QMT 客户端**新建两个策略**：
   - 策略 A：把 `iquant_bridge.py` 内容粘进去 → 仿真桥，监听 `127.0.0.1:8790`。
   - 策略 B：把 `iquant_bridge_live.py` 内容粘进去 → 实盘桥，监听 `127.0.0.1:8791`。
   - 两个文件均为**纯 ASCII、GBK 编辑器兼容**，勿加非 ASCII 字符。
2. 各自配置账号（见下「配置」）。
3. 两个策略**各以「实盘交易」模式运行**。`init` 只做「绑定端口 + 注册
   `C.schedule_run(tick, ...)`」并**立即返回**，由定时回调（默认 1s）驱动有界
   accept/读写循环。控制台应依次看到 `bind ok` → `init done: tick scheduled ...`
   → `tick#N alive ...`（带时间戳与 tick 计数，持续递增才算定时器在跑）。
   > 2026-11-06 变更：旧模型（`init` 内 `while True` 阻塞主循环）在新版客户端里
   > 会「端口 LISTEN、握手成功，但一个字节都不读」——请求全挂 CLOSE_WAIT。改为
   > 官方支持的 `init` 返回 + `schedule_run` 模型（与 QMT 官方示例同形）。
   > 改完桥文件必须在客户端**停止并重新启动策略**才生效（跑着的策略仍是旧代码）。
4. Core 侧 `config.yaml` 的 `iquant_bridge.simulation.base_url` / `iquant_bridge.live.base_url`
   分别指向两个桥地址（默认 8790 / 8791，可配）。
5. 用 `live/scripts/` 下的验证脚本或直接 `curl /ping` 确认两个桥在线后再启动实盘 session。
   `GET /ping` 返回的 `account` 字段可用于核对各自绑定的账号。

## 配置

| 项 | 桥 | 说明 | 配置方式 |
|---|---|---|---|
| 账号 ID | 仿真桥 | `passorder`/查询用的仿真券商账号（虚拟资金） | 文件顶部 `ACCOUNT` 常量，换账号改这一行 |
| 账号 ID | 实盘桥 | `passorder`/查询用的真实券商账号（真实资金） | 文件顶部 `ACCOUNT` 常量，换账号改这一行 |
| 股票白名单 | 两桥 | `ALLOWED_STOCKS`（空 = 不限制） | 策略代码内配置（生产建议配齐） |
| 试运行 | 两桥 | `DRY_RUN`（默认 False） | 策略代码内配置；True 只打印不发单（**开发开关，非模拟/实盘控制**） |
| 端口 | 仿真桥 | `PORT = 8790` | 写死在文件顶部 config 区 |
| 端口 | 实盘桥 | `PORT = 8791` | 写死在文件顶部 config 区 |

> **账号为何写死在桥文件**：QMT 客户端内运行无环境变量、无外部文件路径机制，且 `ContextInfo`
> 只有 `set_account`（写入），没有读取当前登录账号的 API——`passorder`/`get_trade_detail_data`
> 均需显式传账号。故账号只能作为常量写进桥文件。换账号时直接编辑 `ACCOUNT` 那一行即可，
> 两桥各改一处。`GET /ping` 返回的 `account` 字段可用于部署后核对各自绑定的账号。

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
| `GET /calendar?market=SH` | 交易日列表 `["YYYYMMDD", ...]`（缓存） |
| `GET /shutdown` | 释放端口自行退出（供下一次策略启动时接管） |

## 环境约束（已真机验证）

- QMT 客户端线程不可靠、`handlebar` 仅启动回放驱动、`run_time` 不被调用
  → 桥用 `init` **返回 + `C.schedule_run` 定时回调**（默认 1s），回调内跑有界
  accept/读写循环。**`init` 内不得再放 `while` 常驻循环**：新版客户端下阻塞的
  `init` 循环体不会执行，端口虽 LISTEN 但一个字节都不读，请求全挂 CLOSE_WAIT。
- 若该客户端版本不接受秒级 `schedule_run`：把 `TICK_INTERVAL` 改成
  `timedelta(minutes=1)`、`TICK_BUDGET_SECONDS` 改成 `55`（回调内自循环 55s），
  其余代码不动。
- Python 3.6，纯标准库实现 HTTP 层，无第三方依赖。
- 5m 是桥端原生周期可直接拉；1w/1mon 桥端 `xtdata` 拉不到，由 Core 侧通达信注入。

## 掘金量化桥（`goldminer_bridge.py`）

掘金量化桥与 QMT 双桥**同构协议**（HTTP JSON），Core 的 `HttpBridgeDispatcher`
零改动接入；区别是它跑在**掘金终端进程内**，端口 `127.0.0.1:7801`。

- 策略格式：标准掘金策略（`init` + `run()` 事件循环 + 后台线程跑 `ThreadingHTTPServer`）。
- 端点与 QMT 桥一致（`/ping` `/positions` `/account` `/orders` `/deals` `/quote`
  `/calendar` `POST /order`），另有 `POST /cancel`（撤单）与 `POST /shutdown`（自退）。
- 状态码/字段向平台口径映射：委托状态转 QMT `m_nOrderStatus` 数值（平台
  `order_machine` 直接消费）；`/orders` 与 `/deals` 的 `direction` 由 gm `side`
  推导（买 48 / 卖 49）。行为注意：`side` 缺失/无法识别时 `_is_buy` 返回
  `False`，即按**卖**兜底（不默认买）。

### 运行方式（强制）

**桥策略只能由掘金终端点「运行」启动**（终端须已登录）。桥依赖的 gm SDK
（`gm` 包）装在**系统 Python 的 site-packages**——掘金终端正是用系统解释器执行策略，
与终端本地服务 `gmterm-serv` 协议天然匹配。

- **禁止**用 `uv run` / `main/.venv` 启动本文件：那会把 gm 拉进项目 venv，
  下次 `uv sync` 会把 gm 删掉，桥随即真断（Core 只走 HTTP，从不 import gm，
  平台 venv 无 gm 不影响桥）。
- **禁止**把 gm SDK 装进 `main/.venv`：PyPI 版 gm 的 C SDK（NATS 协议）与
  goldminer3 终端的服务协议不匹配（实测 `get_cash` 无限挂起），且依赖链会污染平台环境。
- 终端关闭 = 桥停 = 平台报单 `rejected`（超时 expire 兜底）。

### 配置（文件顶部）

| 常量 | 说明 |
|---|---|
| `TOKEN` | 掘金账户 token |
| `ACCOUNT` | 掘金资金账号 GUID |
| `STRATEGY_ID` | **必须替换**：仓库内是占位符「填入终端分配的策略ID」，新建策略后由终端分配 |
| `HOST` / `PORT` | 默认 `127.0.0.1:7801`，须与 `config.yaml` 的 `goldminer.base_url` 一致 |
| `DRY_RUN` | 开发期打印开关，实盘保持 `False` |
| `CONFIRM_REJECT` / `CONFIRM_DELAY_SECONDS` | 下单后是否短等一次委托查询确认柜台拒单 |

文件编码：UTF-8（无 BOM）+ CRLF，首行声明 `# -*- coding: utf-8 -*-`。

## 换机部署清单

拷到新机器后，按下面逐项核对；标 **必改** 的不能沿用旧机的值。

### A. QMT 双桥

| 项 | 位置 | 换机动作 |
|---|---|---|
| `ACCOUNT` | 两文件顶部 config 区 | **必改** 为 QMT 客户端登录的券商账号（两桥各一处） |
| `ACCOUNT_TYPE` | 两文件顶部 | **必改** 与真实账户类型一致：`STOCK` 普通 / `CREDIT` 两融；不一致会导致查询全空、委托被柜台拒绝 |
| `PORT` | 两文件顶部 | 默认 8790/8791；改端口须同步 `config.yaml` 的 `iquant_bridge.simulation/live.base_url` |
| `TICK_TAG` | 两文件顶部 | 两桥必须不同（同 tag 的定时回调会互相覆盖）；保持默认即可 |
| `DRY_RUN` | 两文件顶部 | 实盘保持 `False` |
| QMT 客户端 | — | 策略须以「**实盘交易**」模式启动；模拟模式只出信号不下单 |
| 文件编码 | — | 两文件必须保持**纯 ASCII**（QMT 编辑器按 GBK 存储，粘中文会乱码） |

验证：`curl http://127.0.0.1:8790/ping` 与 `http://127.0.0.1:8791/ping`，
核对返回的 `account` / `port` / `service` / `dry_run`。

### B. 掘金桥

| 项 | 位置 | 换机动作 |
|---|---|---|
| `TOKEN` | 文件顶部 | **必改** 为新账户 token |
| `ACCOUNT` | 文件顶部 | **必改** 为新账户 GUID |
| `STRATEGY_ID` | 文件顶部 | **必改** 为掘金终端新建策略后分配的 ID（仓库内是占位符） |
| `PORT` | 文件顶部 | 默认 7801；改端口须同步 `config.yaml` 的 `goldminer.base_url` |
| gm SDK | 系统 Python | 在**系统 Python**（掘金终端使用的解释器）里安装 gm SDK；**不要**装进 `main/.venv` |
| 掘金终端 | — | 与 `gmterm-serv` 服务协议匹配的终端版本（本机实证 v5.2.158）；终端须登录后点运行 |

验证：`curl http://127.0.0.1:7801/ping`，`service` 应为 `goldminer-bridge`。

### C. 平台配置（`config.yaml`，项目根目录）

```yaml
iquant_bridge:
  simulation:
    base_url: http://127.0.0.1:8790
  live:
    base_url: http://127.0.0.1:8791
goldminer:
  base_url: http://127.0.0.1:7801
```

### D. 目录内容

| 文件 | 用途 |
|---|---|
| `iquant_bridge.py` | QMT 仿真桥（8790） |
| `iquant_bridge_live.py` | QMT 实盘槽桥（8791，当前仍是纸面账号） |
| `goldminer_bridge.py` | 掘金量化桥（7801，终端内运行） |
| `tests/test_bridge_tick.py` | 桥生命周期/端点/双桥镜像护栏（无终端依赖） |
| `tests/test_iquant_side.py` | DEAL/ORDER 方向归一（两腿一致） |
