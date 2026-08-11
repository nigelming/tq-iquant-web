# iQuant 桥策略（iquant_bridge.py）

在国信 iQuant 客户端内以**策略**方式运行的本地 HTTP 桥：Core（main/）通过
`127.0.0.1:8790` 调 `/order` `/positions` `/account` `/orders` `/deals` `/quote`
实现实盘下单与行情拉取。

- 设计依据：[docs/plans/0009-iquant-http-bridge.md](../../docs/plans/0009-iquant-http-bridge.md)
- 订单状态机 / 成交回报回填：[docs/plans/0011-order-sync-deal-backfill.md](../../docs/plans/0011-order-sync-deal-backfill.md)

## ⚠️ 部署硬要求：必须以「实盘交易」模式运行

桥策略**必须用 iQuant 的「实盘交易」模式启动**——**模拟模式下 `passorder` 只出策略
信号、不发真实委托**（迅投硬规则，2026-08-10 真机验证）。用模拟模式跑起来一切正常、
行情/查询都能用，但 `/order` 永远不下单。上线前务必核对 iQuant 客户端右下角的交易模式。

## 部署步骤

1. 在 iQuant 客户端新建策略，把 `iquant_bridge.py` 内容粘进去（**文件为纯 ASCII，
   GBK 编辑器兼容**，勿加非 ASCII 字符）。
2. 配置账号（见下「配置」）。
3. 以**实盘交易**模式运行策略。`init` 阻塞主循环启动 HTTP 服务（`init` 不返回），
   服务监听 `127.0.0.1:8790`。
4. Core 侧 `config.yaml` 的 `iquant_bridge.base_url` 指向同一地址。
5. 用 `live/scripts/` 下的验证脚本或直接 `curl /ping` 确认桥在线后再启动实盘 session。

## 配置

| 项 | 说明 | 配置方式 |
|---|---|---|
| 账号 ID | `passorder`/查询用的券商账号 | 环境变量 `IQUANT_BRIDGE_ACCOUNT`，否则同目录 `.bridge_account` 文件（首行内容），否则回退 `ACCOUNT_DEFAULT`（开发占位） |
| 股票白名单 | `ALLOWED_STOCKS`（空 = 不限制） | 策略代码内配置（生产建议配齐） |
| 试运行 | `DRY_RUN`（默认 False，真实下单） | 策略代码内配置；True 只打印不发单 |

`IQUANT_BRIDGE_ACCOUNT` 环境变量与 `.bridge_account` 文件均在运行环境设置，**不进 git、
不写死在策略代码**。`.bridge_account` 文件放桥脚本同目录，部署时手动落盘（已被
`.gitignore` 忽略）。

> 鉴权：桥绑 `127.0.0.1` loopback，单用户本机部署，不设 token 鉴权（防御在机器边界：
> 仅本机进程可达 8790 端口）。白名单、单笔限额、频率限制、审计日志仍强制执行。

## 接口

| 端点 | 说明 |
|---|---|
| `GET /ping` | 心跳 + 桥状态 |
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
