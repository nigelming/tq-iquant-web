# 整合多平台的量化回测和交易平台的设计草稿

> ⚠️ **架构已变更（2026-08，见 [docs/plans/0009-iquant-http-bridge.md](plans/0009-iquant-http-bridge.md)）**：本文档为早期设计草稿，其中 **NATS 通信拓扑（natsio / 5 个 subject / `live/iguant_gateway/` NATS 网关）已全部废弃**。Core↔iQuant 现改为 **iQuant 客户端内 HTTP 桥**（`127.0.0.1:8790`，`HttpBridgeDispatcher` + `BarPoller`）。下文凡涉及 NATS/natsio/网关 subject 的内容仅作历史记录，不再实施。iQuant 网关也由独立进程改为客户端内策略。

## 1.项目架构和技术栈
### 1.1项目名称：创懿量化交易平台
### 1.2项目居于windows 10 专业版开发，开发语言为python
### 1.3项目由四个模块组成：核心后端（内含通达信TQ模块）、国信iquant网关、web前端、natsio（仅用于核心后端↔iQuant网关通信）
### 1.4项目建立两个uv环境：
    1、核心后端（含通达信TQ模块）、web前端：使用main环境，python版本为3.13
    2、国信iquant网关：使用live环境，python版本为3.7

> **Python 版本说明**：国信 iQuant 自带的 Python 环境为 3.6.8，但其 xquant（xtquant）库也提供 Python 3.7 版本支持。live 环境使用 Python 3.7 而非 3.6.8，是因为 NATS 客户端库（nats-py）最低要求 Python 3.7。Python 3.6 已于 2021 年 EOL，不再获得安全更新和主流包支持。

> **shared 包兼容性约束**：`shared/` 包被 main（Python 3.13）和 live（Python 3.7）共同引用，代码必须兼容 Python 3.7。约束：不得使用 walrus 运算符 `:=`（3.8+）、match 语句（3.10+）、`typing.Self`（3.11+）等高版本语法；数据结构定义使用 `dataclasses` 或 `pydantic v1`（pydantic v2 最低要求 Python 3.8，不兼容 live 环境）；类型注解使用 `typing` 模块（如 `List`、`Dict`），不使用 3.9+ 的内置泛型语法 `list[str]`。

> **时区处理**：系统所有时间均按 Asia/Shanghai（UTC+8）处理。FastAPI 启动时设置 `TZ=Asia/Shanghai`，数据库时间戳统一存储 UTC 并在应用层转换为上海时间。bar 时间判断（如 10:30 触发 60m）均基于上海时间。

> **数据库迁移**：使用 Alembic 管理 SQLAlchemy ORM 的 schema 变更。每次表结构修改需生成 migration 脚本，通过 `alembic upgrade head` 应用。禁止手动修改数据库表结构。
### 1.5web前端：使用Vue 3 + Vite + Pinia（状态管理），开发期 dev server 代理 API，生产期由 FastAPI 直接托管静态文件
### 1.6核心后端：使用fastapi，开发期数据库使用SQLite（`main/data/dev.db`），生产期切换到PostgreSQL
### 1.7 开发模式：TDD（测试驱动开发），先写测试，再写实现。pytest（后端）+ vitest（前端）
### 1.8 项目目录结构：
    tq-iquant-web/
    ├── main/                          # main uv 环境 (Python 3.13)
    │   ├── pyproject.toml
    │   ├── core/                      # 核心后端 + 通达信 TQ 模块（同进程）
    │   │   ├── __init__.py
    │   │   ├── main.py                # FastAPI 入口
    │   │   ├── api/                   # REST 路由
    │   │   │   ├── __init__.py
│   │   │   ├── stock_pools.py     # 股票池
    │   │   │   ├── formulas.py        # 公式管理
    │   │   │   ├── strategies.py      # 组合策略+策略
    │   │   │   ├── backtest.py        # 回测
    │   │   │   ├── live.py            # 实盘交易
    │   │   │   └── system.py          # 系统配置
    │   │   ├── models/                # SQLAlchemy ORM 模型（14个）
    │   │   │   ├── __init__.py
│   │   │   ├── stock_pool.py
    │   │   │   ├── stock_pool_stock.py
    │   │   │   ├── formula.py
    │   │   │   ├── formula_signal.py
    │   │   │   ├── portfolio_strategy.py
    │   │   │   ├── strategy.py
    │   │   │   ├── backtest_record.py
    │   │   │   ├── backtest_trade.py
    │   │   │   ├── backtest_daily_snapshot.py
│   │   │   ├── backtest_evaluation.py
│   │   │   ├── live_session.py
│   │   │   ├── live_session_portfolio.py
│   │   │   ├── live_order.py
│   │   │   └── live_trade.py
    │   │   ├── services/              # 业务逻辑层
    │   │   │   ├── __init__.py
│   │   │   ├── stock_pool_service.py
    │   │   │   ├── formula_service.py
    │   │   │   ├── strategy_service.py
    │   │   │   ├── backtest_service.py
    │   │   │   ├── live_service.py
    │   │   │   └── system_service.py  # 系统配置（config.yaml 读写）
    │   │   ├── engine/                # 自研回测/交易框架（polars）
    │   │   │   ├── __init__.py
    │   │   │   ├── event.py           # BarEvent/SignalEvent/RiskEvent/OrderEvent/TradeEvent
    │   │   │   ├── event_bus.py       # EventBus 事件分发
    │   │   │   ├── data_feed.py       # DataFeed 数据源
    │   │   │   ├── portfolio.py       # Portfolio 组合运行时
    │   │   │   ├── account.py         # Account 资金管理
    │   │   │   ├── strategy_context.py # StrategyContext 策略运行时
    │   │   │   ├── position.py        # Position 持仓
    │   │   │   ├── risk_manager.py    # RiskManager 风控（组合+策略）
    │   │   │   ├── signal_engine.py   # SignalEngine 信号处理
    │   │   │   ├── execution_engine.py # ExecutionEngine 执行
    │   │   │   ├── evaluator.py       # Evaluator 回测评估
    │   │   │   ├── backtest_engine.py # BacktestEngine 回测引擎
    │   │   │   └── live_engine.py     # LiveEngine 实盘引擎
    │   │   ├── nats_client/           # natsio 客户端（仅连接 iQuant）
    │   │       ├── __init__.py
    │   │       └── client.py
    │   │   ├── tq/                      # 通达信 TQ 模块（同进程，直接调用）
│   │   │   ├── __init__.py
│   │   │   ├── utils.py              # tqcenter 连接管理（initialize/close/全局锁）
│   │   │   ├── data.py               # 数据获取（get_market_data/股票池/板块）
│   │   │   └── formula.py            # 公式计算（formula_process_mul_zb/xg）
    │   │   └── tests/                   # 后端测试（pytest）
    │   │       ├── conftest.py            # fixtures（测试数据库等）
    │   │       ├── unit/                  # 单元测试（engine 各模块）
    │   │       └── integration/           # 集成测试（API 接口）
    ├── live/                          # live uv 环境 (Python 3.7)
    │   ├── pyproject.toml
    │   └── iguant_gateway/            # 国信 iQuant 网关
    │       ├── __init__.py
    │       ├── main.py                # 网关入口
    │       ├── trade/                 # 下单、持仓查询
    │       └── nats_client/           # natsio 客户端
    ├── web/                           # Vue 前端
    │   ├── package.json
    │   ├── vite.config.ts
    │   ├── index.html
    │   ├── src/
    │   │   ├── App.vue
    │   │   ├── main.ts
    │   │   ├── router/
│   │   ├── views/                 # 页面（首页、股票池、策略、回测等）
│   │   ├── components/            # 公共组件（功能树等）
│   │   ├── stores/                # Pinia 状态管理（用户、实盘等）
│   │   ├── api/                   # 后端请求封装
    │   │   └── __tests__/              # 前端测试（vitest）
    │   └── dist/                      # 构建产物，FastAPI 托管

    ├── shared/                          # 共享包（main + live 共同引用）
    │   ├── pyproject.toml
    │   └── tq_iquant_shared/
    │       ├── __init__.py
    │       ├── nats_schemas.py           # NATS 消息数据结构
    │       ├── stock_utils.py            # 股票代码校验等
    │       └── constants.py              # 枚举、常量
    ├── docs/                          # 文档
    │   └── system-plan-draft.md
    ├── config.yaml                     # 系统配置文件
    └── README.md

## 2.通达信TQ模块
### 2.1 定位：嵌入核心后端的 Python 模块，与 Core 同进程运行，由 Core 直接函数调用（不走 NATS）。通过通达信 tqcenter SDK（`PYPlugins/sys/tqcenter.py`）与运行中的通达信进程通信，获取数据、计算公式、订阅实时行情。
### 2.2 技术实现：
    1、tqcenter 连接：`sys.path` 注入 `PYPlugins/sys` + `PYPlugins/user`，导入 `tqcenter.tq`，调用 `tq.initialize(__file__)` 连接到通达信进程
    2、数据获取（`tq.get_market_data`）：支持多股票×多周期历史K线（1m/5m/1d/1w等），返回 DataFrame，自动前复权
    3、股票池/板块（`tq.get_sector_list` / `tq.get_stock_list_in_sector`）：读取通达信自定义板块
    4、公式计算（`tq.formula_process_mul_zb` / `tq.formula_process_mul_xg`）：批量指标计算和条件选股
    5、交易接口（`tq.stock_account` / `tq.order_stock` / `tq.query_stock_positions`）：实盘下单、持仓查询
    6、实时行情订阅（`tq.subscribe_hq`）：注册回调函数，股票有更新时自动推送
### 2.3 业务规则：
    1、统一用带有后缀的股票代码，如000001.SZ 符合通达信的规范
    2、复权方式统一用前复权
    3、统一用支持批量查询，批量公式计算，尽可能避免有循环
    4、Core 直接调用 TQ 模块函数，数据以 polars DataFrame 在进程内传递，不走 NATS
    5、Core 启动时从 config.yaml 读取两个通达信目录路径（回测和实盘），TQ 模块内部按 mode 切换使用的路径
    6、每次数据请求均携带 mode 字段（backtest/live），TQ 模块根据 mode 选择对应的通达信目录，校验通达信进程是否已启动，未启动则拒绝操作并返回错误
    7、TQ 模块内部仅同时维护一个通达信连接（当前 mode 对应的目录），不支持同时连接两个通达信进程。同一时刻禁止同时打开两个通达信客户端，以免 T0002 文件冲突
    8、回测与实盘的 TDX 连接冲突处理：
        a）回测预加载数据阶段（主进程）需要使用 backtest 模式 TDX 连接，此时若实盘 session 正在运行（需要 live 模式 TDX 连接），两者冲突
        b）解决方案：TQ 模块内部维护一把 mode 级互斥锁。回测预加载时获取 backtest mode 锁，预加载完成后释放。实盘订阅时获取 live mode 锁。两者不可同时持有
        c）实盘 session 启动时若 backtest 预加载正在进行，则等待释放后再订阅；回测预加载时若有实盘订阅正在使用 live TDX，则等待当前 bar 处理完成后切换
        d）预加载阶段通常较短（批量读取历史数据），对实盘影响可控
    9、多实盘 session 共享 TDX 订阅：多个 live session 同时运行时，TQ 模块对所有 session + 所有组合策略涉及的股票取并集订阅，去重后统一向通达信订阅 1m/5m bar。收到 bar 后按 session → 按组合策略分发信号
### 2.4 实盘信号流程【已确认】
    1、TQ 模块通过通达信订阅 1m 和 5m bar，注册回调函数
    2、触发规则：
        a）收到 1m bar → 追加到 1m 数据文件 → 触发 1m 周期公式
        b）收到 5m bar → 追加到 5m 数据文件 → 触发 5m 周期公式
        c）收到 5m bar 时检查**结束时间**（bar 时间戳 + 5 分钟）：若为 30 分钟整除点（如 10:00、10:30、11:00、11:30、13:30、14:00、14:30、15:00）→ 合成 30m bar → 触发 30m 周期公式
        d）收到 5m bar 时检查**结束时间**：若为 60 分钟结束点（10:30、11:30、14:00、15:00）→ 合成 60m bar → 触发 60m 周期公式
        e）同一个 5m bar 可能同时触发多个周期（如 10:30 同时触发 5m+30m+60m），合并后通过回调函数一次传递给 Core
        f）bar 时间戳说明：通达信 bar 时间戳为 bar 开始时间（如 9:55-10:00 的 5m bar 时间戳为 9:55），触发判断时需用开始时间 + 周期长度得到结束时间后再做整除判断
    3、回调线程仅将信号投递到主事件循环，公式计算在独立线程池中执行，不阻塞回调线程
    4、Core 主事件循环接收信号 → SignalEngine → RiskManager → ExecutionEngine

## 3.国信iquant网关
### 3.1 定位：独立运行的一个模块（live 环境，Python 3.7），用xquant库与iquant交互，通过natsio与核心后端通信

> Python 版本说明：国信 iQuant 自带的 Python 环境为 3.6.8，但 xquant（xtquant）库也提供 Python 3.7 版本。live 环境使用 Python 3.7 是因为 NATS 客户端库最低要求 Python 3.7，且 Python 3.6 已 EOL。
### 3.2 交易模式：
    1、模拟：国信 iQuant 开设模拟账户，与当天实时行情同步，可进行仿真交易，无需真实资金
    2、实盘：需在国信开立真实账户并注入资金，进行实际交易
### 3.3 功能
    1、交易的下单执行（支持市价单和限价单）
    2、订单状态查询（pending/filled/partial/rejected/cancelled）
    3、持仓的获取
    4、撤单
    5、支持运行状态
### 3.4 业务规则
    1、统一用带有后缀的股票代码，如000001.SZ
    2、所有交换的数据均通过natsio 交换
    3、订单超时处理：下单后 30 秒内未收到成交回报，标记为 timeout 并查询 iQuant 订单状态
    4、部分成交处理：iQuant 返回部分成交时，Core 更新 live_orders.filled_quantity，已成交部分记入 live_trades
    5、拒绝处理：iQuant 返回拒绝时，记录 error_message 到 live_orders，不影响其他策略运行
### 3.5 待讨论问题
    1、模拟和实盘是否都支持市价下单→需查阅国信 iQuant 文档确认

### 3.6 网关生命周期
iQuant 网关进程独立于 Core 运行，不随 Core 重启而退出：

- **启动**：通过单独命令或系统服务管理，启动后连接 iQuant 并监听 NATS
- **运行**：持续维持 iQuant 连接，处理来自 Core 的 NATS 请求（下单/查订单/撤单/查持仓）
- **Core 重启**：网关不受影响，继续运行。Core 恢复后通过 NATS 重新连接网关，查询当前状态
- **Core 宕机期间**：网关持续监控 iQuant 连接，但无新交易指令。订单成交状态暂存，Core 恢复后查询拉取
- **关闭**：通过单独命令关闭网关，关闭前需确保所有实盘 session 已停止
- **健康检查**：Core 定期（每 30 秒）通过 NATS 向网关发送 ping，连续 3 次无响应则标记网关离线，暂停交易

## 4.web的前端
### 4.1 定位：与核心后端组成前后端分离的系统
### 4.2 风格：简洁的浅色风格
### 4.3 web页面的基本结构：分为左右结构，左边为两层的功能树，右边为展示
    1、首页：展示核心后端、国信iquant网关 运行状态
    2、数据管理：包括股票池和公式管理
         股票池：从通达信同步股票池，获取股票池列表及股票清单，可持久化到数据库
        公式管理：新增、删除、编辑公式。公式运行后输出多个信号值（数量不定），每个信号可映射到四种操作类型（OPEN/ADD/REDUCE/CLOSE）之一，并定义触发值为 1 或 -1
    3、组合策略：定义组合策略实例，组合策略实例添加对应策略实例，组合策略实例包括：组合策略的风控、初始资金、对应股票池、交易成本等，策略实例包括：策略的风控、策略使用的公式
    4、回测管理：确定回测开始结束时间、组合策略回测、回测记录查看、回测结果指标展示
    5、实盘交易：新建实盘、包括模拟和实盘两种模式
    6、系统管理：系统配置
        系统配置：回测通达信目录设置、实盘通达信目录设置、iquant目录设置（存储在 config.yaml，非数据库）

## 5.核心后端
### 5.1 定位：系统的总调度平台，内含通达信TQ模块（同进程，直接调用），同时基于polars开发一个回测实盘共用的自研框架，对数据库进行操作
### 5.2 调度平台：将前端请求处理后，本地调用TQ模块或通过natsio分发到国信iquant网关，并对它们的处理结果进行处理
### 5.3 自研框架：以polars核心 事件触发的回测交易系统
#### 5.3.1 交易的框架为：组合策略--策略，组合策略和策略都对应的风控参数
#### 5.3.2 业务规则：
        1、一个组合策略对应一个股票池
        2、组合策略参数：
            1）初始资金：默认值50万
            2）组合策略的风控参数：max_drawdown（最大回撤触发熔断，默认20%），daily_loss_limit（日亏损触发熔断，默认5%）、max_holdings（最大同时持仓数，默认10）
            熔断规则：
                a）max_drawdown 触发：次日自动恢复交易（恢复后若再次触发则再次熔断，次日恢复，如此循环）
                b）daily_loss_limit 触发：当日剩余时间停止交易，次日自动恢复
                c）熔断期间不清仓，仅暂停新开仓
                d）连续触发限制：max_drawdown 在同一回测/实盘中累计触发 3 次后，转为**手动恢复**模式（需用户在前端点击"恢复交易"按钮），不再自动恢复。此规则防止在持续大回撤中反复熔断又恢复
            3）交易成本：最低佣金（5元），买入佣金率（0.025%）、卖出佣金率（0.025%）印花税率（0.05%，卖出单向），滑点值（0%），括号中为默认值
             4）执行设置：交易时段（全天/仅上午/仅下午）
             5）参考的指数：策略的对比基准，用于计算评估指标中的 benchmark_return。由用户选择（如沪深300、中证500等），可自行配置。基准指数的历史数据通过 TQ 模块直接获取
        3、策略的参数
            1）策略的公式
            2）策略的周期（1m/5m/30m/60m/1d/1w），通达信接口自动从 1m/5m/日线合成其他周期数据，直接调用即可
            3）策略角色（对立、主策略、从策略）
            4）资金占比：策略在组合中的可用资金占比
            5）策略分控参数
                参数	        范围	    默认值	    说明
                最大持仓数      1 ~ 20       5	    该策略最大同时持仓数量
                单只开仓        1% ~ 50%	10%	    单只股票占用开仓资金上限
                止损比例        1% ~ 20%	5%	    触发止损的亏损幅度
                止盈比例        5% ~ 50%	15%	    触发止盈的盈利幅度
                移动止损        0% ~ 20%	3%	    从最高点回撤比例（0=关闭）
                加仓阈值        1% ~ 20%	5%	    现价较成本下跌超此比例触发加仓
                最大加仓次数     0 ~ 10	     2	     最多允许加仓几次（0=禁加仓）
                每次加仓比例	1% ~ 30%    10%	     每次加仓占策略资金比例
                每次减仓比例	5% ~ 100%   30%	     每次减仓占当前持仓比例
        4、业务边界：
            1）组合策略要支持多周期多标的回测和交易
            2）多周期并行推进规则：
                a）回测和实盘均支持多个周期同时执行
                 b）回测按组合中所有策略的最小周期推进时间点（如全部为 5m+ 则按 5m 推进，有 1m 策略则按 1m 推进），每个时间点检查各周期 bar 是否结束
                c）每个周期的信号判断以该周期 bar 的结束时间为准（如 60m bar 在 10:30 结束时触发判断）
                d）1d/1w 周期处理：
   - 回测：日线 bar 结束时（当天收盘）触发信号判断，以下一个 bar 的 open 执行；周线同理，bar 结束时触发，下周第一个 bar 的 open 执行
   - 实盘：日线 15:00 收盘时触发信号，周线周五 15:00 触发，此时已收盘无法当日下单，信号延至下一交易日开盘执行（开盘价）
             3）执行时机：统一以下一个 bar 的 open 执行
                 - 回测：信号在当前 bar 结束时触发 → 下一个 bar 的 open 成交
                 - 实盘：信号触发后 → 下一个 bar 的 open 成交

> 1d/1w 周期：bar 结束时（15:00）已收盘，无法当日成交，延至下一交易日或下一周首个交易日的 open 执行。
            4）回测全部按T+1的模式执行，实盘交易前查可用股票数控制
            5）策略角色：对立（independent）是指策略在组合中与其他策略无关，独立运行；主从关系（master/slave）：从策略（slave）必须在主策略（master）持有股票的情况下才能买入。规则细则：
                a）从策略买入时，主策略必须持有**与从策略买入同一只**股票（从策略只能买主策略当前持有的股票）
                b）主策略全部清仓后，从策略**不可新开仓**，但已持有的存量仓位可按自身信号自行卖出（不强制平仓）
                c）一个主策略可对应多个从策略，无数量限制
                d）从策略在配置时必须选择对应的主策略（master_strategy_id 非空），主策略和对立策略的 master_strategy_id 为 NULL
            6）信号优先级：风控信号（止损、止盈、移动止损）优先级高于公式信号（OPEN/ADD/REDUCE/CLOSE）。同一 bar 内两者同时触发时，先执行风控，风控执行后若已清仓则公式信号不再执行
            7）公式信号与风控的卖出机制：
                a）CLOSE：一次性全部平仓，不受「每次减仓比例」限制
                b）REDUCE：与风控减仓共用「每次减仓比例」参数（默认 30%），按当前持仓比例减仓
            8）资金模型规则：
                a）策略资金占比：仅作为该策略的持仓上限（如A策略60%→上限30万），非预分资金，多策略上限之和可超过100%
                b）下单审批链路：策略风控（检查是否超策略持仓上限）→ 组合风控（检查组合是否还有现金）→ 有钱则批准，不够则按剩余金额等比例缩减买入股数；缩减后不足1手则放弃该笔购买
                c）资金不足：放弃购买，同时记录"因资金不足放弃"的次数
                d）卖出资金回收：T+0，当天卖出释放的资金当天即可复用
                 e）主从策略资金竞争：先到先得，不设优先级。主策略因资金不足买不进时，从策略即便有信号也自然无意义
                 f）部分批准：按批准金额等比例缩减买入股数
                  g）同一 bar 内多信号资金竞争顺序（确保回测结果确定）：
                     - 风控信号（止损/止盈/移动止损）优先于公式信号
                     - 公式信号按策略 ID 升序处理
                     - 同策略内按信号类型：CLOSE > REDUCE > ADD > OPEN
               9）多组合策略虚拟持仓规则（仅实盘适用）：
                  a）实盘 session 可包含多个组合策略（portfolio_strategy），共享同一个 iQuant 账户下单
                  b）同一只股票可能被多个组合策略同时持有，实际 broker 账户合并为一笔持仓，系统内按组合策略拆分记账
                  c）虚拟持仓 Per Portfolio：每个组合策略独立维护 `(stock_code, quantity, avg_cost)`，仅存在于 LiveEngine 运行时内存中
                  d）卖出约束：组合策略只能卖出自己虚拟持仓范围内的数量。若组合策略 A 虚拟持仓 100 股 000001.SZ，系统阻止其卖出超过 100 股，即使实际账户有更多
                  e）成本计价：每个组合策略独立计算 avg_cost（仅统计自己买入的份额），互不干扰；卖出 P&L 基于各自的 avg_cost 计算
                f）虚拟现金：组合策略可用资金 = `initial_capital - Σ(买入金额+佣金) + Σ(卖出金额-佣金-印花税)`（基于成本，非市值），各组合策略的现金互不共享。卖出释放的资金 T+0 可复用
                g）实际账户现金：所有组合策略共享同一个 iQuant 账户。实际账户可用现金通过 NATS 向 iQuant 网关实时查询（`iquant.iguant.position.query`），不本地跟踪。下单前先检查组合级别虚拟现金，再检查账户级别实际现金
                h）恢复重算：Core 重启时，虚拟持仓和虚拟现金不从 DB 直接读取，而是从 `live_trades` 按 `portfolio_strategy_id` 聚合重算
                i）跨组合策略信号执行顺序（同一 bar 内多组合策略同时产生信号时，确保结果确定）：
                   - 先处理所有组合策略的风控信号（止损/止盈/移动止损），再处理公式信号
                   - 组合策略间按 `live_session_portfolios.id` 升序处理
                   - 同一组合策略内部沿用第 8g 条规则（策略 ID 升序，CLOSE > REDUCE > ADD > OPEN）
                   - 实际账户现金不足时，按上述顺序先到先得
              10）回测评估基准：
                 a）组合评估：使用时间加权收益率（TWR），以组合初始资金为起点，逐日复利合成。公式：
                    ```
                    R_daily = (当日总价值 - 前一日总价值) / 前一日总价值
                    R_cumulative = ∏(1 + R_daily) - 1
                    ```
                 b）策略评估：使用固定分母法，分母 = 策略资金占比 × 组合初始资金（如 A 策略 60% → 30 万）。固定分母不随资金实际占用变动，确保各期间收益率可比。资金竞争能力本身也是策略评价的一部分。
         5、回测评估
             1）组合和策略分别评估，口径不同：
                 - 组合：时间加权收益率（TWR），以组合初始资金为起点
                 - 策略：固定分母法，分母 = 策略资金占比 × 组合初始资金
             2）18个评估指标
            #	指标	英文 key	单位	评价方向
            1	总收益率	total_return	%	越高越好
            2	年化收益率	annual_return	%	越高越好
            3	最大回撤	max_drawdown	%	越低越好
            4	年化波动率	volatility	%	越低越好
            5	夏普比率	sharpe_ratio	—	越高越好
            6	索提诺比率	sortino_ratio	—	越高越好
            7	卡玛比率	calmar_ratio	—	越高越好
            8	胜率	win_rate	%	越高越好
            9	盈亏比	profit_factor	—	越高越好
            10	总交易次数	total_trades	次	越低越好
            11	基准收益率	benchmark_return	%	—
            12	平均持仓天数	avg_holding_days	天	越低越好
            13	VaR 95%	var_95	%	越低越好
            14	CVaR 95%	cvar_95	%	越低越好
            15	平均回撤恢复天数	avg_recovery_days	天	越低越好
            16	最大回撤恢复天数	max_recovery_days	天	越低越好
            17	Ulcer指数	ulcer_index	—	越低越好
            18	收益稳定性	return_stability	%	越高越好

> **指标计算方法说明**：
> - **VaR 95%**（历史模拟法）：取日收益率序列的 5% 分位数，即 `percentile(daily_returns, 5)`。样本数不足 20 个交易日时返回 NULL（统计意义不足）。
> - **CVaR 95%**：所有低于 VaR 95% 的日收益率的算术平均值，即 `mean(daily_returns[daily_returns <= var_95])`。
> - **收益稳定性 return_stability**：对日收益率序列做线性回归（R² 拟合度），`R² = 1 - SS_res/SS_tot`，其中 SS_res 为残差平方和，SS_tot 为总平方和。R² 越接近 1 表示收益越稳定。百分比格式存储。
> - **平均回撤恢复天数 avg_recovery_days**：从进入回撤（峰值开始下降）到恢复至前峰值所需交易日的平均值。未恢复的回撤不计入统计。
> - **最大回撤恢复天数 max_recovery_days**：所有回撤中恢复时间最长的天数。未恢复的回撤不计入统计。
> - **Ulcer 指数 ulcer_index**：`sqrt(mean(drawdown_pct²))`，其中 drawdown_pct 为每个交易日相对于历史最高净值的回撤百分比。

#### 5.3.3 运行时并发模型【已确认】
回测是 CPU 密集计算，实盘有通达信 bar 回调，需避免阻塞 FastAPI 主事件循环：

**回测：预加载数据 → 独立子进程计算**
```
用户发起回测 → FastAPI 创建 backtest_record（status=running），冻结当前组合策略+所有策略参数到 params_snapshot（JSON）
             → 主进程调用 TQ 模块批量获取：
                 - 历史 K 线（所有股票 × 所有周期）
                 - 公式信号预计算（所有策略公式）
                 - 基准指数历史数据（沪深300等，日线即可，用于计算 benchmark_return）
             → 打包为 polars DataFrame + 信号缓存字典
             → ProcessPoolExecutor 提交 BacktestEngine.run(
                 klines=预加载的K线,
                 signal_cache=预计算的信号缓存,
                 benchmark_data=基准指数历史数据
               )
             → 立即返回 { "id": 1, "status": "running" }
子进程：纯内存计算（polars 逐 bar 迭代），不连接 TQ 模块
         Evaluator 直接从 benchmark_data 计算 benchmark_return，不需要 TQ
         每完成一定比例，更新 backtest_records.progress（0~100）
完成：写 PostgreSQL（trades + daily_snapshots + evaluations，通过独立数据库连接），更新 status=completed, progress=100
失败：更新 status=failed，记录错误信息
前端：轮询 GET /api/backtest/records/{id} 获取实时状态（含 progress 字段）
```

约束：
- 同一时刻最多允许 1 个回测子进程（可通过 config.yaml 配置 max_concurrent_backtest）
- 新请求时若已有回测运行中，返回 `{ "code": 409, "message": "已有回测正在运行，请等待完成" }`
- **回测仅支持单组合策略**：每次回测针对一个 `portfolio_strategy_id`，不支持多组合策略联合回测。多组合策略的虚拟持仓隔离、实际账户现金竞争等交互行为仅在实盘中体现，无法通过回测验证

**实盘：回调投递 + 主线程协程调度**
```
TQ bar 回调（通达信驱动线程）
  → 追加数据到本地文件
  → asyncio.run_coroutine_threadsafe(handle_bar(), main_loop)
  → 立即返回，不阻塞

主线程 asyncio handle_bar()：
  → loop.run_in_executor(None, compute_formulas, bar)   ← 线程池计算
  → 等待公式结果 → SignalEngine → RiskManager → ExecutionEngine
  → NATS 下单（async/await，不阻塞）
```

```
                   回调线程                         主线程 asyncio               线程池
        ┌─────────────────────┐    run_coro_     ┌────────────────────┐   run_in_   ┌──────────────┐
        │ 收到 bar            │──threadsafe────→  │ handle_bar()       │──executor──→│ compute_     │
        │ 追加数据文件         │                   │  调线程池计算       │←───────────│ formulas()   │
        │ 投递协程             │                   │  收到信号结果       │  返回结果   │              │
        │ ← 立即返回           │                   │  SignalEngine      │            │              │
        └─────────────────────┘                   │  RiskManager       │            └──────────────┘
                                                   │  ExecutionEngine   │
                                                   │  NATS 下单         │
                                                   └────────────────────┘
```

约束：
- 回调线程只做：追加数据 + 投递协程。**不做任何公式计算或业务逻辑**，确保微秒级返回
- 公式计算通过 `loop.run_in_executor` 提交到独立线程池，不阻塞事件循环
- TDX C 扩展（tq 模块）不是线程安全的，所有 TDX 调用必须持有同一把全局锁。公式计算放到线程池的价值在于不阻塞回调线程和事件循环，而非并行加速
- 信号处理（风控+执行）在主事件循环，确保线程安全
- NATS 下单为异步 I/O，不阻塞主线程
- 风控参数和持仓数据应在引擎启动时加载到内存，风控检查不得包含数据库 I/O

#### 实盘恢复机制（多组合策略 × 多天运行）

实盘 session 可能运行数天甚至数周，Core 重启/崩溃后需自动恢复运行状态。

**恢复流程**：

```
Core 启动 → 扫描 DB 中 status=running 的 session → 对每个 session：
  1. 查 live_session_portfolios → 得到该 session 的所有组合策略
  2. 查 live_trades 聚合 → 重建各组合策略的虚拟持仓和虚拟现金：
     virtual_cash = initial_capital - Σ(买入金额+费用) + Σ(卖出金额-费用)
     virtual_positions = 按 stock_code 汇总净买入量 + 加权均价
     （直接 SQL 聚合，在 millis 级别完成）
  3. 通过 NATS 连接 iQuant 网关 → 查询实际账户持仓和未完成订单
   4. 虚拟持仓 vs 实际持仓交叉验证：
      - 各组合策略同一股票的虚拟持仓之和应等于实际账户持仓
      - 不一致时记录告警日志，校准策略：差额按各组合策略虚拟持仓量等比例分摊调整；若某股票所有组合策略虚拟持仓均为 0 但实际账户有持仓（Core 宕机期间手动下单），归入第一个组合策略并标记 `unattributed`
  5. 重建 TQ bar 订阅：取所有组合策略股票池并集 → 调用 TQ 模块订阅 1m/5m
  6. 重建 NATS 请求响应监听
  7. 更新 session.status = running → 继续运行
```

**关键约束**：

| 场景 | 处理 |
|------|------|
| Core 宕机期间漏 bar | 通达信无历史实时 bar 查询接口，漏掉的 bar 不做信号计算，从当前最新 bar 继续 |
| iQuant 网关独立运行 | Core 重启不触发网关退出，网关持续维持 iQuant 连接和订单监控 (详见 3.6) |
| Core 宕机期间订单成交 | 恢复后通过 iQuant 查询未完成订单状态，更新 live_orders |
| 虚拟现金重算精度 | 以 live_trades 为唯一数据源，确保恢复结果与宕机前一致 |
| 恢复失败 | 通达信未启动 / iQuant 网关未运行 / NATS 不可达 → session 标记为 stopped，记录错误信息 |

**虚拟持仓恢复 SQL 示例**：
```sql
-- 按 portfolio 聚合买入/卖出，得到虚拟持仓
SELECT
  portfolio_strategy_id,
  stock_code,
  SUM(CASE WHEN trade_type = 'BUY' THEN quantity ELSE -quantity END) as net_quantity,
  SUM(CASE WHEN trade_type = 'BUY' THEN amount ELSE 0 END) / 
    NULLIF(SUM(CASE WHEN trade_type = 'BUY' THEN quantity ELSE 0 END), 0) as avg_cost
FROM live_trades
WHERE live_session_id = ?
GROUP BY portfolio_strategy_id, stock_code
HAVING net_quantity > 0
```

### 5.4 数据库表设计（使用 PostgreSQL + SQLAlchemy ORM，14 张表）

> 开发期使用 SQLite（`main/data/dev.db`，路径由 `config.yaml` 的 `database.sqlite_path` 配置），Alembic 配置已预设。生产期切换到 PostgreSQL，利用 MVCC 解决回测子进程并发写入 + 主进程读取的冲突问题。切换只需修改 `config.yaml` 的 `database.sqlite_path`（或未来切 PG 时的 `alembic.ini`）。

#### 一、通达信
##### 1. stock_pools
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| name | VARCHAR(100) | 通达信股票池名称 |
| synced_at | DATETIME | 最近同步时间 |
| created_at | DATETIME | |

##### 2. stock_pool_stocks
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| pool_id | INTEGER FK → stock_pools | |
| stock_code | VARCHAR(20) | 如 000001.SZ |
| stock_name | VARCHAR(50) | |

> **唯一约束**：`UNIQUE(pool_id, stock_code)`，防止同步时产生重复记录。同步策略为**全量替换**（先删除该池下所有股票，再批量写入）。

#### 三、公式
##### 3. formulas
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| name | VARCHAR(100) | |
| content | TEXT | 通达信公式文本 |
| created_at | DATETIME | |
| updated_at | DATETIME | |

##### 4. formula_signals
公式运行后输出多个信号，每行定义其中一个信号的映射规则

> **触发规则**：公式输出 1 或 -1（纯标记，不表示买卖方向），`trigger_value` 与之匹配则触发。不同公式作者的输出习惯不同（有人用 1=触发，有人用 -1=触发），`trigger_value` 用来兼容各自的习惯。具体操作类型（OPEN/ADD/REDUCE/CLOSE）由 `signal_type` 决定，与公式输出值无关。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| formula_id | INTEGER FK → formulas | |
| signal_name | VARCHAR(50) | 信号名称，与公式输出中的名称对应 |
| signal_type | VARCHAR(10) | 映射到的操作类型：OPEN / ADD / REDUCE / CLOSE |
| trigger_value | INTEGER | 触发值：1 或 -1，公式输出值等于此值时触发 |

#### 四、策略
##### 5. portfolio_strategies
| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| id | INTEGER PK | | |
| name | VARCHAR(100) | | |
| stock_pool_id | INTEGER FK | | 对应股票池 |
| benchmark_index | VARCHAR(20) | 000300.SH | 参考的指数代码（如 000300.SH=沪深300、000905.SH=中证500），用于计算 benchmark_return |
| initial_capital | DECIMAL(15,2) | 500000 | |
| max_drawdown | DECIMAL(5,4) | 0.2000 | 20% |
| daily_loss_limit | DECIMAL(5,4) | 0.0500 | 5% |
| max_holdings | INTEGER | 10 | |
| min_commission | DECIMAL(10,2) | 5 | |
| buy_commission_rate | DECIMAL(8,6) | 0.000250 | 万2.5 |
| sell_commission_rate | DECIMAL(8,6) | 0.000250 | 万2.5 |
| stamp_duty_rate | DECIMAL(8,6) | 0.000500 | 万5，卖出单向 |
| slippage | DECIMAL(8,6) | 0 | |
| trading_session | VARCHAR(10) | full | full=全天 / am=仅上午 / pm=仅下午 |
| status | VARCHAR(10) | active | active / archived |
| created_at | DATETIME | | |
| updated_at | DATETIME | | |

##### 6. strategies
| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| id | INTEGER PK | | |
| portfolio_id | INTEGER FK | | |
| name | VARCHAR(100) | | |
| formula_id | INTEGER FK | | |
| period | VARCHAR(5) | | 1m/5m/30m/60m/1d/1w |
| role | VARCHAR(15) | | independent / master / slave |
| master_strategy_id | INTEGER FK | NULL | 从策略时必填 |
| capital_ratio | DECIMAL(5,4) | 0.6000 | 资金占比上限，持仓上限 = capital_ratio × 组合初始资金 |
| max_positions | INTEGER | 5 | |
| single_open_ratio | DECIMAL(5,4) | 0.1000 | |
| stop_loss_ratio | DECIMAL(5,4) | 0.0500 | |
| take_profit_ratio | DECIMAL(5,4) | 0.1500 | |
| trailing_stop_ratio | DECIMAL(5,4) | 0.0300 | 0=关闭 |
| add_position_threshold | DECIMAL(5,4) | 0.0500 | |
| max_add_count | INTEGER | 2 | 0=禁加仓 |
| add_position_ratio | DECIMAL(5,4) | 0.1000 | |
| reduce_position_ratio | DECIMAL(5,4) | 0.3000 | |
| created_at | DATETIME | | |
| updated_at | DATETIME | | |

#### 五、回测
##### 7. backtest_records
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| portfolio_strategy_id | INTEGER FK | |
| name | VARCHAR(100) | |
| start_date | DATE | |
| end_date | DATE | |
| status | VARCHAR(10) | running / completed / failed |
| progress | INTEGER | 0~100，子进程逐 bar 更新，前端轮询时展示进度条 |
| error_message | TEXT | NULL，失败时记录错误信息 |
| params_snapshot | TEXT | JSON，回测启动时冻结的组合策略+所有策略参数快照，确保回测结果可复现 |
| created_at | DATETIME | |
| completed_at | DATETIME | |

##### 8. backtest_trades
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| backtest_record_id | INTEGER FK | |
| strategy_id | INTEGER FK | |
| formula_signal_id | INTEGER FK | NULL，公式信号触发时关联 formula_signals.id |
| signal_name | VARCHAR(50) | 触发信号名称（公式信号名或风控规则名） |
| signal_type | VARCHAR(15) | OPEN/ADD/REDUCE/CLOSE/STOP_LOSS/TAKE_PROFIT/TRAILING_STOP |
| stock_code | VARCHAR(20) | |
| trade_type | VARCHAR(4) | BUY / SELL |
| price | DECIMAL(10,3) | |
| quantity | INTEGER | 股数 |
| amount | DECIMAL(15,2) | 成交金额 |
| commission | DECIMAL(10,2) | |
| stamp_duty | DECIMAL(10,2) | |
| bar_time | DATETIME | 该 bar 的时间 |
| created_at | DATETIME | |

##### 9. backtest_daily_snapshots
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| backtest_record_id | INTEGER FK | |
| target_type | VARCHAR(10) | portfolio / strategy |
| target_id | INTEGER | 对应 portfolio_strategy.id 或 strategy.id |
| snap_date | DATE | 快照日期（交易日） |
| total_value | DECIMAL(15,2) | 总资产 |
| cash | DECIMAL(15,2) | 现金 |
| market_value | DECIMAL(15,2) | 持仓市值 |
| daily_return | DECIMAL(10,6) | 当日收益率 |
| cumulative_return | DECIMAL(10,6) | 累计收益率 |
| benchmark_value | DECIMAL(15,2) | 基准净值（用于对比） |
| positions_json | TEXT | 当日持仓明细 JSON |
| created_at | DATETIME | |

> 每日快照是评估指标的原始数据来源。回测每个交易日结束时生成一条快照，18 个评估指标由 Evaluator 基于快照序列计算得出。

##### 10. backtest_evaluations
> 以下 18 个指标均由 Evaluator 从 backtest_daily_snapshots 计算得出，每条记录对应一个回测的 portfolio 或单个 strategy 的评估结果。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| backtest_record_id | INTEGER FK | |
| target_type | VARCHAR(10) | portfolio / strategy |
| target_id | INTEGER | 对应 portfolio_strategy.id 或 strategy.id |
| total_return | DECIMAL(10,4) | |
| annual_return | DECIMAL(10,4) | |
| max_drawdown | DECIMAL(10,4) | |
| volatility | DECIMAL(10,4) | |
| sharpe_ratio | DECIMAL(10,4) | |
| sortino_ratio | DECIMAL(10,4) | |
| calmar_ratio | DECIMAL(10,4) | |
| win_rate | DECIMAL(10,4) | |
| profit_factor | DECIMAL(10,4) | |
| total_trades | INTEGER | |
| benchmark_return | DECIMAL(10,4) | |
| avg_holding_days | DECIMAL(10,4) | |
| var_95 | DECIMAL(10,4) | |
| cvar_95 | DECIMAL(10,4) | |
| avg_recovery_days | DECIMAL(10,4) | |
| max_recovery_days | INTEGER | |
| ulcer_index | DECIMAL(10,4) | |
| return_stability | DECIMAL(10,4) | |
| created_at | DATETIME | |

#### 六、实盘
##### 11. live_sessions
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| name | VARCHAR(100) | |
| mode | VARCHAR(10) | simulation / live |
| status | VARCHAR(10) | running / stopped |
| created_at | DATETIME | |
| updated_at | DATETIME | |
| started_at | DATETIME | NULL，最近一次启动时间 |
| stopped_at | DATETIME | NULL，最近一次停止时间 |

一个 session 对应一个 iQuant 账户，可包含多个组合策略同时运行。

> **Core 重启恢复**：Core 启动时扫描所有 `status=running` 的 session，自动重建 TQ 订阅（1m/5m bar）和 NATS 连接。恢复失败（如通达信未启动）的 session 自动标记为 `stopped`。多天运行的 session 恢复细节详见「实盘恢复机制」（5.3.3 之后）。

##### 12. live_session_portfolios

一个 session 与多个 portfolio_strategy 的关联表，每行记录一个组合策略在 session 中的状态。虚拟现金不持久化存储，Core 恢复时从 `live_trades` 重算。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| session_id | INTEGER FK → live_sessions | ON DELETE CASCADE |
| portfolio_strategy_id | INTEGER FK → portfolio_strategies | |
| status | VARCHAR(15) | active / inactive / circuit_broken（熔断手动恢复等待中） |
| circuit_breaker_count | INTEGER | 0，max_drawdown 累计触发次数，达到 3 后 status 转 circuit_broken |
| created_at | DATETIME | |
| updated_at | DATETIME | |

> **唯一约束**：`UNIQUE(session_id, portfolio_strategy_id)`，防止同一 session 重复添加同一组合策略。

##### 13. live_orders
> 实盘订单跟踪表。记录每次下单的状态流转，支持部分成交、拒绝、超时等场景。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| live_session_id | INTEGER FK → live_sessions | |
| portfolio_strategy_id | INTEGER FK → portfolio_strategies | 所属组合策略 |
| strategy_id | INTEGER FK → strategies | |
| stock_code | VARCHAR(20) | |
| trade_type | VARCHAR(4) | BUY / SELL |
| order_type | VARCHAR(10) | market / limit |
| price | DECIMAL(10,3) | 委托价格（市价单为 NULL） |
| quantity | INTEGER | 委托股数 |
| filled_quantity | INTEGER | 已成交股数，默认 0 |
| filled_price | DECIMAL(10,3) | 成交均价，默认 NULL |
| status | VARCHAR(15) | pending / filled / partial / rejected / cancelled / timeout |
| error_message | VARCHAR(500) | NULL，被拒绝或超时时记录原因 |
| nats_request_id | VARCHAR(64) | NATS 请求 ID，用于关联请求-响应 |
| signal_name | VARCHAR(50) | 触发信号名称 |
| signal_type | VARCHAR(15) | OPEN/ADD/REDUCE/CLOSE/STOP_LOSS/TAKE_PROFIT/TRAILING_STOP |
| bar_time | DATETIME | 触发信号的 bar 时间 |
| created_at | DATETIME | |
| updated_at | DATETIME | |

##### 14. live_trades
> 实盘成交记录表。每笔实际成交（含部分成交）生成一条记录，用于实盘交易审计和盈亏分析。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| live_session_id | INTEGER FK → live_sessions | |
| live_order_id | INTEGER FK → live_orders | NULL，关联的订单 |
| portfolio_strategy_id | INTEGER FK → portfolio_strategies | 所属组合策略 |
| strategy_id | INTEGER FK → strategies | |
| stock_code | VARCHAR(20) | |
| trade_type | VARCHAR(4) | BUY / SELL |
| price | DECIMAL(10,3) | 成交价格 |
| quantity | INTEGER | 成交股数 |
| amount | DECIMAL(15,2) | 成交金额 |
| commission | DECIMAL(10,2) | 佣金 |
| stamp_duty | DECIMAL(10,2) | 印花税 |
| trade_time | DATETIME | 成交时间 |
| created_at | DATETIME | |

#### 表关系
```
stock_pools ──< stock_pool_stocks
formulas ──< formula_signals
portfolio_strategies ──< strategies ──< backtest_trades
portfolio_strategies ──< backtest_records ──< backtest_daily_snapshots
portfolio_strategies ──< backtest_records ──< backtest_evaluations
portfolio_strategies ──< backtest_records ──< backtest_trades
live_sessions ──< live_session_portfolios >── portfolio_strategies
live_sessions ──< live_orders ──< live_trades
live_sessions ──< live_trades
strategies.master_strategy_id ──> strategies（自引用）
```

#### 索引设计
```
-- 高频查询索引
stock_pool_stocks (pool_id, stock_code)       -- 按池查股票，唯一约束防重复
backtest_trades (backtest_record_id, bar_time) -- 按回测查交易明细
backtest_daily_snapshots (backtest_record_id, target_type, target_id, snap_date) -- 按策略查快照
backtest_evaluations (backtest_record_id, target_type, target_id) -- 查评估结果
live_session_portfolios (session_id, portfolio_strategy_id) -- 唯一约束
live_orders (live_session_id, status)         -- 查活跃订单
live_orders (portfolio_strategy_id)           -- 按组合策略查订单
live_trades (live_session_id, trade_time)     -- 按时间查成交
live_trades (live_order_id)                   -- 按订单查成交
live_trades (portfolio_strategy_id, trade_time) -- 按组合策略查成交（恢复重算）
```

#### 级联删除规则
```
-- 删除组合策略时：
--   strategies → CASCADE（自动删除关联策略）
--   backtest_records → RESTRICT（需先删除回测记录）
--   live_session_portfolios → RESTRICT（需先从所有实盘 session 中移除）
-- 删除回测记录时：
--   backtest_trades / backtest_daily_snapshots / backtest_evaluations → CASCADE
-- 删除实盘 session 时：
--   live_session_portfolios → CASCADE
--   live_orders / live_trades → CASCADE
-- 删除公式时：
--   formula_signals → CASCADE
--   引用该公式的 strategies → RESTRICT（需先解除引用）
-- 删除股票池时：
--   stock_pool_stocks → CASCADE
--   引用该池的 portfolio_strategies → RESTRICT（需先解除引用）
```

### 5.5 自研框架类设计（Engine 运行时）

#### 5.5.1 ORM 与 Engine 的关系
ORM（SQLAlchemy）负责数据库的存取，Engine 负责运行时计算。Engine 从数据库读取配置创建运行时对象，计算结果通过 services 层写回数据库。Engine 本身不持有数据库状态。

#### 5.5.2 类清单

##### models/（ORM 层，14 个类，与数据库表一一对应）
```
models/
├── stock_pool.py            # StockPool
├── stock_pool_stock.py      # StockPoolStock
├── formula.py               # Formula
├── formula_signal.py        # FormulaSignal
├── portfolio_strategy.py    # PortfolioStrategy
├── strategy.py              # Strategy
├── backtest_record.py       # BacktestRecord
├── backtest_trade.py        # BacktestTrade
├── backtest_daily_snapshot.py # BacktestDailySnapshot
├── backtest_evaluation.py   # BacktestEvaluation
├── live_session.py          # LiveSession
├── live_session_portfolio.py # LiveSessionPortfolio
├── live_order.py            # LiveOrder
└── live_trade.py            # LiveTrade
```

##### engine/（运行时类）
```
engine/
├── event.py                 # 事件定义
│   ├── BarEvent             # bar 数据到达
│   ├── SignalEvent          # 公式信号（OPEN/ADD/REDUCE/CLOSE）
│   ├── RiskEvent            # 风控触发（止损/止盈/熔断）
│   ├── OrderEvent           # 下单指令
│   └── TradeEvent           # 成交回报
│
├── event_bus.py             # 事件总线
│   └── EventBus             # 事件分发、优先级排序（风控优先于信号）
│
├── data_feed.py             # 数据源
│   └── DataFeed             # 直接调用 TQ 模块获取 bar 数据
│
├── portfolio.py             # 组合策略运行时
│   └── Portfolio            # 从 PortfolioStrategy 创建，持有 Account + 多个 StrategyContext
│
├── account.py               # 账户资金管理
│   └── Account              # 现金、冻结、总市值、资金审批（策略上限+组合现金双层卡控）
│
├── strategy_context.py      # 策略运行时
│   └── StrategyContext      # 从 Strategy 创建，持有信号列表 + 持仓列表
│
├── position.py              # 持仓
│   └── Position             # stock_code, quantity, avg_cost, highest_price
│
├── risk_manager.py          # 风控
│   ├── PortfolioRiskManager # max_drawdown, daily_loss_limit, max_holdings, 熔断（次日恢复）
│   └── StrategyRiskManager  # 止损/止盈/移动止损/加仓/减仓
│
├── signal_engine.py         # 信号处理
│   └── SignalEngine         # 信号优先级（风控>公式）、CLOSE 全清、REDUCE 走减仓比例
│
├── execution_engine.py      # 执行引擎
│   └── ExecutionEngine      # 共用：按比例缩减、不足1手放弃、资金不足放弃
│       ├── OrderDispatcher  # 接口：下单
│       ├── SimulatedDispatcher  # 回测：模拟成交，按 next_bar.open 填价
│       └── NatsDispatcher   # 实盘：通过 NATS 发往 iQuant 网关
│   └── T1Checker            # 接口：T+1 检查
│       ├── SimulatedT1Checker   # 回测：内部模拟检查
│       └── LiveT1Checker    # 实盘：查 iQuant 实际可用股数
│
├── evaluator.py             # 回测评估
│   └── Evaluator            # 读取 daily_snapshots → 计算 18 个指标 → 输出 BacktestEvaluation
│
├── backtest_engine.py       # 回测引擎
│   └── BacktestEngine       # 组装上述组件：初始化→逐bar迭代→事件分发→执行→评估
│
└── live_engine.py           # 实盘引擎
    └── LiveEngine           # 管理多个 Portfolio 实例；TQ 回调接收 bar → 按 portfolio 分发信号 → 事件分发 → 执行
                              # NATS 对接 iQuant 网关下单；订单状态跟踪（live_orders）；成交记录（live_trades）；SSE 推送
```

#### 5.5.3 数据库表与运行时对象对应关系
```
数据库表 (ORM)                     运行时对象 (Engine)
─────────────────────────────────────────────────────────
portfolio_strategies  ──创建──→  Portfolio
                                   ├── Account
strategies            ──创建──→  StrategyContext
                                   ├── Position[] (0..n)
                                   ├── FormulaSignal[]（引用）
formulas + formula_signals ──引用──→  SignalEngine
(策略风控参数)         ──创建──→  StrategyRiskManager
(组合风控参数)         ──创建──→  PortfolioRiskManager
                                   ├── EventBus
                                   ├── DataFeed
                                   ├── ExecutionEngine
backtest_records      ──输出──→  BacktestEngine.run()
backtest_trades       ──输出──→  ExecutionEngine → TradeEvent
backtest_daily_snapshots ──输出──→ BacktestEngine（每交易日结束时生成快照）
backtest_evaluations  ──输出──→  Evaluator.evaluate(snapshots)
live_sessions + live_session_portfolios ──创建──→  LiveEngine（含多个 Portfolio）
live_orders           ──输出──→  LiveEngine（订单状态跟踪）
live_trades           ──输出──→  LiveEngine（成交记录写入，恢复时重算虚拟持仓）
```

#### 5.5.4 回测/实盘执行适配层

ExecutionEngine 是回测和实盘共用的核心执行逻辑（按比例缩减、资金审批、1 手检查），差异部分通过策略模式抽取为接口：

```
  BarEvent → SignalEngine → RiskManager → ExecutionEngine
                                                │
                                    ┌───────────┴───────────┐
                                    │                       │
                            OrderDispatcher            T1Checker
                                    │                       │
                    ┌───────────────┼───┐           ┌───────┴───────┐
                    │               │   │           │               │
            Simulated         Nats     (预留其他)   Simulated    LiveT1Checker
            Dispatcher     Dispatcher              T1Checker      (查 iQuant)
            (回测模拟成交)  (实盘NATS下单)

```

##### OrderDispatcher 接口

```python
# execution_engine.py
class OrderDispatcher(ABC):
    @abstractmethod
    def place_order(self, order: OrderEvent, portfolio_id: int) -> TradeEvent:
        """下单，返回成交结果"""

class SimulatedDispatcher(OrderDispatcher):
    """回测使用：按 next_bar.open 模拟成交，不涉及外部系统"""
    def place_order(self, order, portfolio_id) -> TradeEvent:
        price = self._get_open_price(order.stock_code, order.bar_time)
        return TradeEvent(
            price=price,
            quantity=order.quantity,
            amount=price * order.quantity,
            commission=calc_commission(order, price),
            stamp_duty=calc_stamp_duty(order, price),
        )

class NatsDispatcher(OrderDispatcher):
    """实盘使用：通过 NATS 发往 iQuant 网关"""
    def place_order(self, order, portfolio_id) -> TradeEvent:
        # 异步下单，等待成交回报
        nats_client.request("iquant.iguant.order.place", {
            "stock_code": order.stock_code,
            "trade_type": order.trade_type,
            "quantity": order.quantity,
            "order_type": "market",
            "portfolio_id": portfolio_id,  # 标记所属组合策略
        })
        # 成交回报由网关异步推送，更新 live_orders/live_trades
```

##### T1Checker 接口

```python
class T1Checker(ABC):
    @abstractmethod
    def get_available_shares(self, stock_code: str, portfolio_id: int) -> int:
        """获取当日可卖出股数（实盘查 iQuant，回测直接返回持仓量）"""

class SimulatedT1Checker(T1Checker):
    def get_available_shares(self, stock_code, portfolio_id) -> int:
        # 昨天及之前买入的即可卖出，无真实 T+1 限制之外的其他约束
        return position.quantity  # 持仓量即可卖出

class LiveT1Checker(T1Checker):
    def get_available_shares(self, stock_code, portfolio_id) -> int:
        # 通过 NATS 查 iQuant 实际可用股数
        resp = nats_client.request("iquant.iguant.position.query", ...)
        return resp.get("available_shares", 0)
```

##### 共有逻辑（ExecutionEngine 主体）

```python
class ExecutionEngine:
    def __init__(self, dispatcher: OrderDispatcher, t1_checker: T1Checker):
        self._dispatcher = dispatcher
        self._t1_checker = t1_checker

    def execute(self, order: OrderEvent, account: Account,
                position: Position, portfolio_id: int) -> Optional[TradeEvent]:
        """共用：审批 → 缩减 → 1手检查 → 委托"""
        # 1. 资金审批（策略上限 + 组合现金）
        approved, qty = account.approve_order(order, position.market_value)
        if not approved or qty < 100:  # 不足 1 手
            account.record_insufficient_funds()  # 记录资金不足次数
            return None
        # 2. T+1 检查
        available = self._t1_checker.get_available_shares(order.stock_code, portfolio_id)
        if order.trade_type == "SELL":
            qty = min(qty, available)
            if qty < 100: return None
        # 3. 下单
        order.quantity = qty
        trade = self._dispatcher.place_order(order, portfolio_id)
        # 4. 更新账户和持仓
        account.apply_trade(trade)
        if position: position.apply_trade(trade)
        return trade
```

##### 创建方式

```python
# BacktestEngine 创建时
executor = ExecutionEngine(
    dispatcher=SimulatedDispatcher(),
    t1_checker=SimulatedT1Checker(),
)

# LiveEngine 创建时
executor = ExecutionEngine(
    dispatcher=NatsDispatcher(nats_client),
    t1_checker=LiveT1Checker(nats_client),
)
```

### 5.6 REST API 设计

#### 5.6.1 通用规范
- 基础路径：`/api`
- 无鉴权，所有接口直接可访问（单用户系统）
- 分页：所有列表接口支持 `page`（页码，从 1 开始）和 `page_size`（每页条数，默认 20，最大 100）查询参数。返回格式：
```json
{ "code": 0, "data": { "items": [...], "total": 150, "page": 1, "page_size": 20 } }
```
未传分页参数时返回全量数据（仅限数据量小的接口：股票池列表、组合策略列表、公式列表等）。交易明细、快照等大数据量接口必须传分页参数。
- 返回格式：
```json
{ "code": 0, "message": "ok", "data": { ... } }
```
code=0 成功，非 0 为错误码。错误码与 HTTP 状态码对齐：

| code | HTTP | message | 说明 |
|------|------|---------|------|
| 0 | 200 | ok | 成功 |
| 404 | 404 | 资源不存在 | 请求的资源未找到 |
| 409 | 409 | 已有回测正在运行，请等待完成 | 回测并发冲突 |
| 422 | 422 | 请求参数校验失败 | 参数格式或值不合法 |
| 500 | 500 | 服务器内部错误 | 未预期的异常 |

#### 5.6.2 首页 - 运行状态
| 方法 | URL | 说明 |
|------|-----|------|
| GET | `/api/status` | 获取系统运行状态 |

**GET /api/status**
```
返回: { "code": 0, "data": {
  "core":           { "online": true, "version": "1.0", "uptime": "2h30m", "tdx_backtest_running": true, "tdx_live_running": false },
  "iguant_gateway": { "online": true, "version": "1.0", "uptime": "2h30m" }
}}
```

#### 5.6.3 股票池

> **实现状态（2026-08-11 复核，审计 #16）**：下表端点与 `main/core/api/stock_pools.py` 对照——
> - `GET /api/stock-pools/{id}`（详情）**未实现**；列表 `_serialize_pool` 已含 id/code/name/synced_at/stock_count，详情字段无增量，按需可复用列表项。
> - **路径偏差**：`POST /{id}/sync` → 实现为 `POST /api/stock-pools/sync`（body `{code}`，按板块 code 而非池 id 同步）；`GET /{id}/stocks` → 实现为 `GET /api/stock-pools/tdx/{code}/stocks`（按通达信板块 code 取实时成分股）。
> - **额外端点**（设计无但已实现）：`DELETE /api/stock-pools/{id}`（删本地池，CASCADE 删成分股）。

| 方法 | URL | 说明 |
|------|-----|------|
| GET | `/api/stock-pools` | 获取股票池列表 |
| GET | `/api/stock-pools/tdx` | 获取通达信中可用的股票池列表（未同步的） |
| POST | `/api/stock-pools/{id}/sync` | 从通达信同步股票池（全量替换） |
| GET | `/api/stock-pools/{id}` | 获取股票池详情 |
| GET | `/api/stock-pools/{id}/stocks` | 获取股票池中的股票清单 |

**GET /api/stock-pools**
```
返回: { "code": 0, "data": [
  { "id": 1, "name": "涨停池", "stock_count": 50, "synced_at": "2026-07-28T09:00:00" }
]}
```

**GET /api/stock-pools/{id}/stocks**
```
返回: { "code": 0, "data": [
  { "id": 1, "stock_code": "000001.SZ", "stock_name": "平安银行" }
]}
```

#### 5.6.4 公式管理

> **实现状态（2026-08-11 复核，审计 #16）**：下表端点与 `main/core/api/formulas.py` 对照——
> - 信号映射 CRUD（`GET/POST/PUT/DELETE /api/formulas/{id}/signals[/{signal_id}]`）**未单独实现**；已并入公式 CRUD 全量保存——`_serialize_formula` 内嵌 signals 子列表，`POST/PUT /api/formulas` 请求体含 `signals: list[SignalItem]`，后端全量替换（删旧建新），等价覆盖增删改。
> - `POST /api/formulas/{id}/test-run`（公式试运行）**未实现**（涉及 TQ 调用，按需单独立项）。

| 方法 | URL | 说明 |
|------|-----|------|
| GET | `/api/formulas` | 公式列表 |
| GET | `/api/formulas/{id}` | 获取公式详情 |
| POST | `/api/formulas` | 新增公式 |
| PUT | `/api/formulas/{id}` | 编辑公式 |
| DELETE | `/api/formulas/{id}` | 删除公式 |
| GET | `/api/formulas/{id}/signals` | 获取公式的信号映射列表 |
| POST | `/api/formulas/{id}/signals` | 新增信号映射 |
| PUT | `/api/formulas/{id}/signals/{signal_id}` | 编辑信号映射 |
| DELETE | `/api/formulas/{id}/signals/{signal_id}` | 删除信号映射 |
| POST | `/api/formulas/{id}/test-run` | 测试运行公式，返回输出的信号名称和值 |

**POST /api/formulas**
```
请求: { "name": "均线策略", "content": "MA5:=MA(C,5);..." }
返回: { "code": 0, "data": { "id": 1 } }
```

**POST /api/formulas/{id}/signals**
```
请求: { "signal_name": "买入", "signal_type": "OPEN", "trigger_value": 1 }
返回: { "code": 0, "data": { "id": 1 } }
```

**GET /api/formulas/{id}/signals**
```
返回: { "code": 0, "data": [
  { "id": 1, "signal_name": "买入", "signal_type": "OPEN", "trigger_value": 1 },
  { "id": 2, "signal_name": "卖出", "signal_type": "CLOSE", "trigger_value": 1 }
]}
```

**POST /api/formulas/{id}/test-run**
```
请求: { "stock_code": "000001.SZ", "period": "1d", "start_date": "2024-01-01", "end_date": "2024-06-30" }
返回: { "code": 0, "data": {
  "signals": [
    { "name": "买入", "values": [0, 0, 1, 0, ...] },
    { "name": "卖出", "values": [0, 0, 0, -1, ...] }
  ]
}}
```

#### 5.6.5 组合策略
| 方法 | URL | 说明 |
|------|-----|------|
| GET | `/api/portfolios` | 组合策略列表 |
| POST | `/api/portfolios` | 新增组合策略 |
| PUT | `/api/portfolios/{id}` | 编辑组合策略 |
| DELETE | `/api/portfolios/{id}` | 删除组合策略 |
| GET | `/api/portfolios/{id}` | 获取组合策略详情（含策略列表） |

**POST /api/portfolios**
```
请求: { "name": "测试组合", "stock_pool_id": 1, "initial_capital": 500000, ... }
返回: { "code": 0, "data": { "id": 1 } }
```

#### 5.6.6 策略
| 方法 | URL | 说明 |
|------|-----|------|
| GET | `/api/portfolios/{pid}/strategies` | 策略列表 |
| POST | `/api/portfolios/{pid}/strategies` | 新增策略 |
| PUT | `/api/portfolios/{pid}/strategies/{id}` | 编辑策略 |
| DELETE | `/api/portfolios/{pid}/strategies/{id}` | 删除策略 |

**POST /api/portfolios/{pid}/strategies**
```
请求: { "name": "均线策略A", "formula_id": 1, "period": "1d", "role": "independent", "capital_ratio": 0.6, ... }
返回: { "code": 0, "data": { "id": 1 } }
```

#### 5.6.7 回测管理

> **实现状态（2026-08-11 复核，审计 #16）**：下表端点与 `main/core/api/backtest.py` 对照——
> - `GET /api/backtest/records/{id}/trades`、`GET /records/{id}/snapshots`、`GET /records/{id}/results`（#7/#8/#9）**未单独实现**；已并入 `GET /api/backtest/records/{id}` 内嵌详情——一次返回 record + snapshots + trades + evaluations + strategy_*，前端无需再发 3 个子请求。
> - **额外端点**（设计无但已实现）：`DELETE /api/backtest/records/{id}`（删回测记录 + CASCADE 删子表）。

| 方法 | URL | 说明 |
|------|-----|------|
| POST | `/api/backtest` | 启动回测（非阻塞，子进程执行） |
| GET | `/api/backtest/records` | 回测记录列表 |
| GET | `/api/backtest/records/{id}` | 回测记录详情（含评估结果） |
| GET | `/api/backtest/records/{id}/trades` | 回测交易明细 |
| GET | `/api/backtest/records/{id}/snapshots` | 每日快照（权益曲线） |
| GET | `/api/backtest/records/{id}/results` | 回测评估结果 |

**POST /api/backtest**
```
请求: { "portfolio_strategy_id": 1, "name": "2024年回测", "start_date": "2024-01-01", "end_date": "2024-12-31" }
返回（成功，子进程已启动）: { "code": 0, "data": { "id": 1, "status": "running", "progress": 0 } }
返回（已有回测运行中）: { "code": 409, "message": "已有回测正在运行，请等待完成" }
```

**GET /api/backtest/records/{id}/trades**
```
返回: { "code": 0, "data": [
  { "id": 1, "strategy_id": 1, "stock_code": "000001.SZ", "signal_name": "买入", "signal_type": "OPEN", "trade_type": "BUY", "price": 10.50, "quantity": 1000, "amount": 10500, "bar_time": "2024-01-02T09:31:00" }
]}
```

**GET /api/backtest/records/{id}/snapshots**
```
查询参数: ?target_type=portfolio&target_id=1（可选，不传返回 portfolio 级别）
返回: { "code": 0, "data": [
  { "snap_date": "2024-01-02", "total_value": 100500.00, "cash": 89500.00, "market_value": 11000.00, "daily_return": 0.0050, "cumulative_return": 0.0050, "benchmark_value": 100200.00 },
  { "snap_date": "2024-01-03", "total_value": 101200.00, "cash": 89500.00, "market_value": 11700.00, "daily_return": 0.0070, "cumulative_return": 0.0120, "benchmark_value": 100800.00 }
]}
```

**GET /api/backtest/records/{id}/results**
```
返回: { "code": 0, "data": {
  "portfolio": { "total_return": 0.15, "annual_return": 0.12, "max_drawdown": -0.08, ... },
  "strategies": [
    { "strategy_id": 1, "strategy_name": "均线A", "total_return": 0.10, ... },
    { "strategy_id": 2, "strategy_name": "均线B", "total_return": 0.20, ... }
  ]
}}
```

#### 5.6.8 实盘交易

> **实现状态（2026-08-11 复核，审计 #16）**：下表端点与 `main/core/api/live.py` 对照——
> - 单组合启停 `POST /api/live/sessions/{id}/portfolios/{pid}/start|stop`（#10/#11）**未实现**；现为整 session 启停（`POST /sessions/{id}/start`、`POST /sessions/{id}/stop`），按需单独立项（涉及 LiveEngine 多组合调度改造）。
> - `PUT /api/live/sessions/{id}`（#12 编辑会话）**未实现**；现为创建后不可编辑（仅可删后重建）。
> - **额外端点**（设计无但已实现）：`GET /api/live/sessions/{id}/positions`（实时持仓快照）、`GET /api/live/sessions/{id}/bridge-status`（iQuant 桥连接/心跳状态）。

| 方法 | URL | 说明 |
|------|-----|------|
| GET | `/api/live/sessions` | 实盘列表 |
| GET | `/api/live/sessions/{id}` | 获取实盘详情（含组合策略列表、持仓、盈亏） |
| POST | `/api/live/sessions` | 新建实盘（选择多个组合策略） |
| POST | `/api/live/sessions/{id}/start` | 启动实盘（启动全部组合策略） |
| POST | `/api/live/sessions/{id}/stop` | 停止实盘（停止全部组合策略） |
| POST | `/api/live/sessions/{id}/portfolios/{pid}/start` | 启动/恢复单个组合策略（含熔断手动恢复） |
| POST | `/api/live/sessions/{id}/portfolios/{pid}/stop` | 停止单个组合策略 |
| PUT | `/api/live/sessions/{id}` | 编辑实盘（名称、组合策略列表） |
| DELETE | `/api/live/sessions/{id}` | 删除实盘 |
| GET | `/api/live/sessions/{id}/orders` | 查询订单列表（支持按 portfolio_id 筛选） |
| GET | `/api/live/sessions/{id}/trades` | 查询成交记录（支持按 portfolio_id 筛选） |

**POST /api/live/sessions**
```
请求: { "name": "实盘测试", "mode": "simulation", "portfolio_ids": [1, 2] }
返回: { "code": 0, "data": { "id": 1, "status": "stopped", "portfolios": [{"portfolio_id": 1, "name": "趋势跟踪", "status": "inactive"}, {"portfolio_id": 2, "name": "网格策略", "status": "inactive"}] } }
```

**GET /api/live/sessions/{id}**
```
返回: { "code": 0, "data": {
  "id": 1, "name": "实盘测试", "mode": "simulation", "status": "running",
  "portfolios": [
    { "portfolio_id": 1, "name": "趋势跟踪", "virtual_cash": 450000, "virtual_positions": [{"stock_code": "000001.SZ", "quantity": 1000, "avg_cost": 10.50, "market_value": 10500, "pnl": 200}] },
    { "portfolio_id": 2, "name": "网格策略", "virtual_cash": 280000, "virtual_positions": [...] }
  ],
  "actual_account": { "total_asset": 780000, "cash": 730000, "market_value": 50000 },
  "started_at": "2026-07-28T09:30:00", "stopped_at": null
}}
```

**GET /api/live/sessions/{id}/orders**
```
查询参数: ?portfolio_id=1&status=pending（均可选）
返回: { "code": 0, "data": [
  { "id": 1, "portfolio_strategy_id": 1, "strategy_id": 1, "stock_code": "000001.SZ", "trade_type": "BUY", "order_type": "market", "quantity": 1000, "filled_quantity": 1000, "filled_price": 10.50, "status": "filled", "signal_name": "买入", "signal_type": "OPEN", "bar_time": "2024-01-02T10:30:00" }
]}
```

**GET /api/live/sessions/{id}/trades**
```
查询参数: ?portfolio_id=1（可选）
返回: { "code": 0, "data": [
  { "id": 1, "portfolio_strategy_id": 1, "live_order_id": 1, "strategy_id": 1, "stock_code": "000001.SZ", "trade_type": "BUY", "price": 10.50, "quantity": 1000, "amount": 10500, "commission": 5.00, "stamp_duty": 0, "trade_time": "2024-01-02T10:30:05" }
]}
```

#### 5.6.9 系统配置（文件存储）
系统配置存储在项目根目录 `config.yaml`，不存数据库。

| 方法 | URL | 说明 |
|------|-----|------|
| GET | `/api/system/configs` | 读取配置文件全部内容 |
| PUT | `/api/system/configs` | 全量更新配置文件 |

**config.yaml 结构**
> 数据库密码等敏感信息不存储在 config.yaml 中，通过环境变量 `TQ_DB_PASSWORD` 读取。config.yaml 仅存储非敏感配置。

```yaml
tdx_backtest_path: D:\tdx\data
tdx_live_path: D:\tdx\data2
iquant_path: D:\iquant
max_concurrent_backtest: 1

database:
  host: localhost
  port: 5432
  database: tq_iquant
  user: postgres
  # password 从环境变量 TQ_DB_PASSWORD 读取，不写入配置文件

nats:
  url: nats://localhost:4222
```

**PUT /api/system/configs**
```
请求: { "tdx_backtest_path": "D:\\tdx\\data", "tdx_live_path": "D:\\tdx\\data2", "iquant_path": "D:\\iquant", "max_concurrent_backtest": 1, "database": { "host": "localhost", "port": 5432, "database": "tq_iquant", "user": "postgres" }, "nats": { "url": "nats://localhost:4222" } }
返回: { "code": 0 }
```
#### 5.6.10 实盘实时推送（SSE）

> 实盘交易需要实时推送持仓变化、成交回报、信号触发等事件，HTTP 轮询无法满足实时性要求。使用 Server-Sent Events（SSE）实现服务端到前端的单向推送。

| URL | 说明 |
|-----|------|
| `GET /api/live/sessions/{id}/stream` | 实盘 session 实时事件流 |

**连接认证**：无鉴权，直接连接。

**事件类型**（所有事件均带 `portfolio_id` 字段）：
```json
// 信号触发
event: signal
data: { "portfolio_id": 1, "strategy_id": 1, "stock_code": "000001.SZ", "signal_name": "买入", "signal_type": "OPEN", "bar_time": "2024-01-02T10:30:00" }

// 订单状态
event: order
data: { "portfolio_id": 1, "order_id": 1, "status": "filled", "filled_quantity": 1000, "filled_price": 10.50 }

// 成交回报
event: trade
data: { "portfolio_id": 1, "trade_id": 1, "stock_code": "000001.SZ", "trade_type": "BUY", "price": 10.50, "quantity": 1000, "amount": 10500 }

// 持仓变化
event: position
data: { "portfolio_id": 1, "stock_code": "000001.SZ", "quantity": 1000, "avg_cost": 10.50, "market_value": 10500, "pnl": 0 }

// 风控触发
event: risk
data: { "portfolio_id": 1, "rule": "max_drawdown", "triggered": true, "message": "最大回撤熔断触发" }
```

**约束**：
- 每个 SSE 连接绑定一个 live session
- 服务端每 30 秒发送 `event: ping` 心跳，浏览器自动保持连接
- 连接断开后浏览器 `EventSource` 自动重连（原生行为，无需重连逻辑）

#### 5.6.11 接口汇总

```
GET    /api/status
GET    /api/stock-pools
GET    /api/stock-pools/tdx
POST   /api/stock-pools/{id}/sync
GET    /api/stock-pools/{id}
GET    /api/stock-pools/{id}/stocks
GET    /api/formulas
GET    /api/formulas/{id}
POST   /api/formulas
PUT    /api/formulas/{id}
DELETE /api/formulas/{id}
GET    /api/formulas/{id}/signals
POST   /api/formulas/{id}/signals
PUT    /api/formulas/{id}/signals/{sid}
DELETE /api/formulas/{id}/signals/{sid}
POST   /api/formulas/{id}/test-run
GET    /api/portfolios
POST   /api/portfolios
PUT    /api/portfolios/{id}
DELETE /api/portfolios/{id}
GET    /api/portfolios/{id}
GET    /api/portfolios/{pid}/strategies
POST   /api/portfolios/{pid}/strategies
PUT    /api/portfolios/{pid}/strategies/{id}
DELETE /api/portfolios/{pid}/strategies/{id}
POST   /api/backtest
GET    /api/backtest/records
GET    /api/backtest/records/{id}
GET    /api/backtest/records/{id}/trades
GET    /api/backtest/records/{id}/snapshots
GET    /api/backtest/records/{id}/results
GET    /api/live/sessions
GET    /api/live/sessions/{id}
POST   /api/live/sessions
POST   /api/live/sessions/{id}/start
POST   /api/live/sessions/{id}/stop
POST   /api/live/sessions/{id}/portfolios/{pid}/start
POST   /api/live/sessions/{id}/portfolios/{pid}/stop
PUT    /api/live/sessions/{id}
DELETE /api/live/sessions/{id}
GET    /api/live/sessions/{id}/orders?portfolio_id=1
GET    /api/live/sessions/{id}/trades?portfolio_id=1
GET    /api/live/sessions/{id}/stream     # SSE
GET    /api/system/configs
PUT    /api/system/configs
```

## 6.natsio
### 6.1 定位：连接核心后端（main 环境，Python 3.13）与国信 iQuant 网关（live 环境，Python 3.7）的唯一通信桥梁。Core 与 TQ 模块同进程直接调用，不走 NATS

### 6.2 通信模式
| 模式 | 场景 |
|------|------|
| 请求-响应（Request-Reply） | 所有操作：下单、查订单、撤单、查持仓、查状态 |

### 6.3 Subject 清单（共 5 个）

| 方向 | Subject | 模式 | 说明 |
|------|---------|------|------|
| Core→iQuant | `iquant.iguant.order.place` | 请求-响应 | 下单 |
| Core→iQuant | `iquant.iguant.order.query` | 请求-响应 | 查询订单状态 |
| Core→iQuant | `iquant.iguant.order.cancel` | 请求-响应 | 撤单 |
| Core→iQuant | `iquant.iguant.position.query` | 请求-响应 | 查询持仓 |
| Core→iQuant | `iquant.iguant.status` | 请求-响应 | 查询运行状态 |

### 6.4 消息格式
统一使用 JSON。

请求-响应：
```json
// 请求
{ "request_id": "uuid", "data": { ... } }
// 响应
{ "request_id": "uuid", "success": true, "data": { ... }, "error": null }
```

### 6.5 完整数据交互流程

#### 回测
```
Core 直接调用 TQ 模块获取多股票 × 多周期 bar + 信号（进程内，无 NATS）
Core 按最小周期逐时间点迭代 → SignalEngine → RiskManager → ExecutionEngine
```

#### 实盘（模拟/实盘）
```
1. Core 直接调用 TQ 模块订阅 bar，注册回调（进程内，无 NATS）
2. TQ 实时接收 bar → 追加数据文件 → 触发对应周期公式（同 bar 可能同时触发 5m+30m+60m）→ 合并信号 → 回调传递给 Core
3. Core 接收信号 → SignalEngine → RiskManager → ExecutionEngine
4. Core → iQuant（NATS）: iquant.iguant.order.place（下单）
5. iQuant → Core（NATS）: 返回成交结果
```

## 7.认证

无用户认证。系统为单机单用户设计，无登录页，无 Session，无角色权限。前端直接展示功能界面，后端所有接口无需鉴权。

## 8.系统统一规则
### 8.1 系统中的股票代码统一都用带后缀的 如000001.SZ
### 8.2 系统中的数据统一用通达信的数据

## 9.须明确的事项
### 9.1 【已明确】策略资金模型规则→详见 5.3.2 业务边界第 8-9 条
### 9.2 【已明确】数据库表设计（14 张表）→详见 5.4
### 9.3 【已明确】自研框架类设计→详见 5.5
### 9.4 【已明确】natsio 消息格式与 Subject 设计→详见第 6 章
### 9.5 【已明确】实盘执行时机→下一个 bar 的 open，详见 5.3.2 业务边界第 3 条
### 9.6 【已明确】策略周期数据来源→通达信接口自动合成，详见 5.3.2 策略参数第 2 条
### 9.7 【已明确】REST API 协议设计→详见 5.6（9 组，44 个 HTTP 接口 + 1 个 SSE）
### 9.8 待明确：日志、监控、告警机制设计

## 10.开发顺序

### 第一阶段：基础设施（无依赖，可并行）
| # | 任务 | 内容 |
|---|------|------|
| 1 | main 环境 | 搭建 uv 环境（Python 3.13），初始化 FastAPI 项目骨架 |
| 2 | live 环境 | 搭建 uv 环境（Python 3.7），初始化 iQuant 网关骨架 |
| 3 | 前端项目 | Vite + Vue + Pinia 初始化，安装依赖 |
| 4 | PostgreSQL | 安装配置 PostgreSQL |
| 5 | natsio | 安装并配置 natsio server，验证连通 |

### 第二阶段：数据层（依赖第一阶段）
| # | 任务 | 内容 |
|---|------|------|
| 6 | 数据库 | 14 张表 SQLAlchemy ORM 定义 + Alembic 迁移初始化 |
| 7 | nats_client | Core 侧 natsio 客户端封装 |
| 8 | 前端框架 | 路由、功能树框架、页面占位 |
| 9 | 测试框架 | 配置 pytest + vitest，编写 conftest.py（测试数据库 fixtures，用独立 PostgreSQL 测试库或内存 SQLite） |

### 第三阶段：TQ 数据模块（依赖第二阶段，与 Core 同进程）
| # | 任务 | 内容 |
|---|------|------|
| 10 | TQ 数据模块 | 股票池获取、历史数据、bar 实时订阅、公式计算（进程内直接调用，无 NATS） |
| 11 | 公式管理 | 先写 API 集成测试 → formulas API + 前端页面 |

### 第四阶段：核心引擎（依赖第三阶段，每项 TDD：先写单元测试再写实现）
| # | 任务 | 内容 |
|---|------|------|
| 12 | 事件系统 | event.py + event_bus.py + 单元测试 |
| 13 | 数据+账户 | data_feed.py（调用 TQ 模块） + account.py + position.py + 单元测试 |
| 14 | 信号+风控 | signal_engine.py + risk_manager.py + 单元测试 |
| 15 | 执行引擎 | execution_engine.py + 单元测试 |
| 16 | 组合+策略 | portfolio.py + strategy_context.py + 单元测试 |
| 17 | 回测引擎 | backtest_engine.py（逐bar迭代→每交易日结束生成 daily_snapshot）+ 集成测试（小数据集端到端验证） |

### 第五阶段：回测（依赖第四阶段）
| # | 任务 | 内容 |
|---|------|------|
| 18 | 评估模块 | evaluator.py（读取 daily_snapshots → 计算 18 个指标）+ 单元测试（用固定快照序列验证指标计算） |
| 19 | 股票池管理 | 先写测试 → 股票池 API + 前端页面 |
| 20 | 策略管理 | 先写测试 → 组合策略+策略 API + 前端页面 |
| 21 | 回测管理 | 先写测试 → 启动/查询/交易明细/每日快照/评估结果 API + 前端页面 |

### 第六阶段：实盘（依赖第五阶段）
| # | 任务 | 内容 |
|---|------|------|
| 22 | 实盘引擎 | live_engine.py（TQ 回调接收 bar）+ 单元测试 |
| 23 | iQuant 网关 | 下单、订单查询、撤单、持仓查询、状态（NATS 通信）+ Mock 测试 |
| 24 | 实盘管理 | 先写测试 → 创建/启动/停止/订单/成交查询 API + SSE 推送 + 前端页面 |

### 第七阶段：系统收尾
| # | 任务 | 内容 |
|---|------|------|
| 25 | 系统配置 | 先写测试 → 配置文件读写 API（config.yaml）+ 页面 |
| 26 | 首页仪表盘 | 运行状态展示 + 组件测试 |
| 27 | 日志监控 | 日志、监控、告警机制 |

### 依赖关系图
```
一: [1][2][3][4]→[5]
      │
二: [6][7][8][9]
      │
三: [10]→[11]
      │
四: [12]→[13]→[14]→[15]→[16]→[17]
      │
五: [18]→[19][20]→[21]
      │
六: [22]→[23]→[24]
      │
七: [25][26][27]
```
