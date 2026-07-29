# 整合多平台的量化回测和交易平台的设计草稿

## 1.项目架构和技术栈
### 1.1项目名称：创懿量化交易平台
### 1.2项目居于windows 10 专业版开发，开发语言为python
### 1.3项目由四个模块组成：核心后端（内含通达信TQ模块）、国信iquant网关、web前端、natsio（仅用于核心后端↔iQuant网关通信）
### 1.4项目建立两个uv环境：
    1、核心后端（含通达信TQ模块）、web前端：使用main环境，python版本为3.13
    2、国信iquant网关：使用live环境，python版本为3.6.8
### 1.5web前端：使用Vue 3 + Vite，开发期 dev server 代理 API，生产期由 FastAPI 直接托管静态文件
### 1.6核心后端：使用fastapi，数据库使用sqlite
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
    │   │   │   ├── auth.py            # 登录认证
    │   │   │   ├── users.py           # 用户管理
    │   │   │   ├── stock_pools.py     # 股票池
    │   │   │   ├── formulas.py        # 公式管理
    │   │   │   ├── strategies.py      # 组合策略+策略
    │   │   │   ├── backtest.py        # 回测
    │   │   │   ├── live.py            # 实盘交易
    │   │   │   └── system.py          # 系统配置
    │   │   ├── models/                # SQLAlchemy ORM 模型（13个）
    │   │   │   ├── __init__.py
    │   │   │   ├── user.py
    │   │   │   ├── session.py
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
    │   │   │   └── live_session.py
    │   │   ├── services/              # 业务逻辑层
    │   │   │   ├── __init__.py
    │   │   │   ├── auth_service.py    # 认证+Session
    │   │   │   ├── user_service.py
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
    │   │   │   ├── data.py              # 历史数据获取、bar 实时订阅
    │   │   │   └── formula.py           # 公式计算、信号输出
    │   │   └── tests/                   # 后端测试（pytest）
    │   │       ├── conftest.py            # fixtures（测试数据库等）
    │   │       ├── unit/                  # 单元测试（engine 各模块）
    │   │       └── integration/           # 集成测试（API 接口）
    ├── live/                          # live uv 环境 (Python 3.6.8)
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
    │   │   ├── api/                   # 后端请求封装
    │   │   └── __tests__/              # 前端测试（vitest）
    │   └── dist/                      # 构建产物，FastAPI 托管

    ├── docs/                          # 文档
    │   └── system-plan-draft.md
    ├── config.yaml                     # 系统配置文件
    └── README.md

## 2.通达信TQ模块
### 2.1 定位：嵌入核心后端的 Python 模块，与 Core 同进程运行，由 Core 直接函数调用（不走 NATS）。负责所有与通达信的数据交互和公式计算
### 2.2 功能：
    1、获取通达信内的自定义股票池的列表，每个股票池中股票清单
    2、获取股票的历史数据（多股票×多周期，返回 polars DataFrame）
    3、通过tq提供的公式计算，获取输出的信号
    4、实盘中订阅股票的1分钟、5分钟 bar的数据
    5、将1分钟、5分钟 数据 按通达信的规范，实盘中追加到通达信1分钟和5分钟的数据上
    6、实盘开启前对通达信1分钟、5分钟、日线进行备份，同时支持恢复
    7、根据请求中携带的 mode（回测/实盘）校验对应的通达信是否已启动，未启动则拒绝操作
### 2.3 业务规则：
    1、统一用带有后缀的股票代码，如000001.SZ 符合通达信的规范
    2、复权方式统一用前复权
    3、统一用支持批量查询，批量公式计算，尽可能避免有循环
    4、Core 直接调用 TQ 模块函数，数据以 polars DataFrame 在进程内传递，不走 NATS
    5、Core 启动时从 config.yaml 读取两个通达信目录路径（回测和实盘），传入 TQ 模块初始化
    6、每次数据请求均携带 mode 字段（backtest/live），TQ 模块根据 mode 选择对应的通达信目录，校验通达信进程是否已启动，未启动则拒绝操作并返回错误
### 2.4 实盘信号流程【已确认】
    1、TQ 模块通过通达信订阅 1m 和 5m bar，注册回调函数
    2、触发规则：
        a）收到 1m bar → 追加到 1m 数据文件 → 触发 1m 周期公式
        b）收到 5m bar → 追加到 5m 数据文件 → 触发 5m 周期公式
        c）收到 5m bar 时检查时间：若为 30 分钟整除点（如 10:00、10:30、11:00、11:30、13:30、14:00、14:30、15:00）→ 合成 30m bar → 触发 30m 周期公式
        d）收到 5m bar 时检查时间：若为 60 分钟结束点（10:30、11:30、14:00、15:00）→ 合成 60m bar → 触发 60m 周期公式
        e）同一个 5m bar 可能同时触发多个周期（如 10:30 同时触发 5m+30m+60m），合并后通过回调函数一次传递给 Core
    3、Core 通过回调接收信号 → SignalEngine → RiskManager → ExecutionEngine

## 3.国信iquant网关
### 3.1 定位：独立运行的一个模块（live 环境，Python 3.6.8），用xquant库与iquant交互，通过natsio与核心后端通信
### 3.2 交易模式：
    1、模拟：国信 iQuant 开设模拟账户，与当天实时行情同步，可进行仿真交易，无需真实资金
    2、实盘：需在国信开立真实账户并注入资金，进行实际交易
### 3.3 功能
    1、交易的下单执行
    2、持仓的获取
    3、支持运行状态
### 3.4 业务规则
    1、统一用带有后缀的股票代码，如0000001.SZ
    2、所有交换的数据均通过natsio 交换
### 3.5 待讨论问题
    1、模拟和实盘是否都支持市价下单→需查阅国信 iQuant 文档确认

## 4.web的前端
### 4.1 定位：与核心后端组成前后端分离的系统
### 4.2 风格：简洁的浅色风格
### 4.3 web页面的基本结构：分为左右结构，左边为两层的功能树，右边为展示
    1、首页：展示核心后端、国信iquant网关 运行状态
    2、数据管理：包括股票池和公式管理
        股票池：从通达信同步股票池，获取股票池列表及股票清单，可持久化到sqlite
        公式管理：新增、删除、编辑公式。公式运行后输出多个信号值（数量不定），每个信号可映射到四种操作类型（OPEN/ADD/REDUCE/CLOSE）之一，并定义触发值为 1 或 -1
    3、组合策略：定义组合策略实例，组合策略实例添加对应策略实例，组合策略实例包括：组合策略的风控、初始资金、对应股票池、交易成本等，策略实例包括：策略的风控、策略使用的公式
    4、回测管理：确定回测开始结束时间、组合策略回测、回测记录查看、回测结果指标展示
    5、实盘交易：新建实盘、包括模拟和实盘两种模式
    6、系统管理：包括用户管理和系统配置
        用户管理：添加、删除、编辑用户
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
                a）max_drawdown 触发：次日自动恢复交易
                b）daily_loss_limit 触发：当日剩余时间停止交易，次日自动恢复
                c）熔断期间不清仓，仅暂停新开仓
            3）交易成本：最低佣金（5元），买入佣金率（0.025%）、卖出佣金率（0.025%）印花税率（0.05%，卖出单向），滑点值（0%），括号中为默认值
            4）执行设置：交易时段（全天/仅上午/仅下午）、执行时机（开盘价/收盘价）
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
                b）回测按最小周期（1m）推进时间点，每个时间点检查各周期 bar 是否结束
                c）每个周期的信号判断以该周期 bar 的结束时间为准（如 60m bar 在 10:30 结束时触发判断）
                d）1d/1w 周期处理：
   - 回测：日线 bar 结束时（当天收盘）触发信号判断，以下一个 bar 的 open 执行；周线同理，bar 结束时触发，下周第一个 bar 的 open 执行
   - 实盘：日线 15:00 收盘时触发信号，周线周五 15:00 触发，此时已收盘无法当日下单，信号延至下一交易日开盘执行（开盘价）
            3）执行时机：
                a）回测：开盘价→下一个bar的open，收盘价→当前bar的close
                b）实盘：信号触发后，以下一个tick或下一个bar的open执行
            4）回测全部按T+1的模式执行，实盘交易前查可用股票数控制
            5）策略角色：对立是指策略在组合中与其他策略无关，主从关系是从策略必须在主策略持有股票的情况下才能买，同时从策略必须选择对应的主策略（在配置时）
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
            9）回测评估基准：
                a）策略评估：以策略占比×组合初始资金为基准（如A策略用30万做分母），保守评估，不过度高估抢不到资金的策略
                b）组合评估：以组合初始资金为基准（如50万）
        5、回测评估
            1）每个策略要单独评估，组合策略也要评估，评估的初始资金，组合为组合的初始资金，策略的为策略的占比*组合初始资金，策略的初始资金和可以大于组合的
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

#### 5.3.3 运行时并发模型【已确认】
回测是 CPU 密集计算，实盘有通达信 bar 回调，需避免阻塞 FastAPI 主事件循环：

**回测：独立子进程**
```
用户发起回测 → FastAPI 创建 backtest_record（status=running）
             → ProcessPoolExecutor 提交 BacktestEngine.run()
             → 立即返回 { "id": 1, "status": "running" }
子进程：独立 Python 进程，持有 polars，逐 bar 迭代计算
完成：写 SQLite（trades + daily_snapshots + evaluations），更新 status=completed
失败：更新 status=failed，记录错误信息
前端：轮询 GET /api/backtest/records/{id} 获取实时状态
```

约束：
- 同一时刻最多允许 1 个回测子进程（可通过 config.yaml 配置 max_concurrent_backtest）
- 新请求时若已有回测运行中，返回 `{ "code": 1, "message": "已有回测正在运行，请等待完成" }`

**实盘：回调线程 + 主线程协程**
```
TQ bar 回调（通达信驱动线程）
  → 追加数据 + 公式计算（在回调线程中）
  → asyncio.run_coroutine_threadsafe(signal_event, main_loop)
主线程 asyncio：SignalEngine → RiskManager → ExecutionEngine
  → NATS 下单（async await，不阻塞）
```

约束：
- 信号处理（风控+执行）在主事件循环，确保线程安全
- NATS 下单为异步 I/O，不阻塞主线程

### 5.4 数据库表设计（使用 SQLAlchemy ORM，13 张表）

#### 一、系统
##### 1. users
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| username | VARCHAR(50) UNIQUE | |
| password_hash | VARCHAR(255) | |
| role | VARCHAR(20) | admin / researcher / trader |
| created_at | DATETIME | |
| updated_at | DATETIME | |

##### 2. sessions
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| user_id | INTEGER FK → users | |
| session_token | VARCHAR(255) UNIQUE | |
| expires_at | DATETIME | |
| created_at | DATETIME | |

#### 二、通达信
##### 3. stock_pools
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| name | VARCHAR(100) | 通达信股票池名称 |
| synced_at | DATETIME | 最近同步时间 |
| created_at | DATETIME | |

##### 4. stock_pool_stocks
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| pool_id | INTEGER FK → stock_pools | |
| stock_code | VARCHAR(20) | 如 000001.SZ |
| stock_name | VARCHAR(50) | |

#### 三、公式
##### 5. formulas
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| name | VARCHAR(100) | |
| content | TEXT | 通达信公式文本 |
| created_at | DATETIME | |
| updated_at | DATETIME | |

##### 6. formula_signals
公式运行后输出多个信号，每行定义其中一个信号的映射规则

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| formula_id | INTEGER FK → formulas | |
| signal_name | VARCHAR(50) | 信号名称，与公式输出中的名称对应 |
| signal_type | VARCHAR(10) | 映射到的操作类型：OPEN / ADD / REDUCE / CLOSE |
| trigger_value | INTEGER | 触发值：1 或 -1，公式输出值等于此值时触发 |

#### 四、策略
##### 7. portfolio_strategies
| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| id | INTEGER PK | | |
| name | VARCHAR(100) | | |
| stock_pool_id | INTEGER FK | | 对应股票池 |
| benchmark_index | VARCHAR(20) | 沪深300 | 参考的指数，用于计算 benchmark_return |
| initial_capital | DECIMAL(15,2) | 500000 | |
| max_drawdown | DECIMAL(5,4) | 0.2000 | 20% |
| daily_loss_limit | DECIMAL(5,4) | 0.0500 | 5% |
| max_holdings | INTEGER | 10 | |
| min_commission | DECIMAL(10,2) | 5 | |
| buy_commission_rate | DECIMAL(5,4) | 0.00025 | 万2.5 |
| sell_commission_rate | DECIMAL(5,4) | 0.00025 | 万2.5 |
| stamp_duty_rate | DECIMAL(5,4) | 0.0005 | 万5，卖出单向 |
| slippage | DECIMAL(5,4) | 0 | |
| trading_session | VARCHAR(10) | 全天 | 全天/仅上午/仅下午 |
| execution_timing | VARCHAR(10) | 开盘价 | 开盘价/收盘价 |
| status | VARCHAR(10) | active | active / archived |
| created_at | DATETIME | | |
| updated_at | DATETIME | | |

##### 8. strategies
| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| id | INTEGER PK | | |
| portfolio_id | INTEGER FK | | |
| name | VARCHAR(100) | | |
| formula_id | INTEGER FK | | |
| period | VARCHAR(5) | | 1m/5m/30m/60m/1d/1w |
| role | VARCHAR(10) | | 对立/主策略/从策略 |
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
##### 9. backtest_records
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| portfolio_strategy_id | INTEGER FK | |
| name | VARCHAR(100) | |
| start_date | DATE | |
| end_date | DATE | |
| status | VARCHAR(10) | running / completed / failed |
| error_message | TEXT | NULL，失败时记录错误信息 |
| created_at | DATETIME | |
| completed_at | DATETIME | |

##### 10. backtest_trades
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

##### 11. backtest_daily_snapshots
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

> 每日快照是评估指标的原始数据来源。回测每个交易日结束时生成一条快照，18 个评估指标由 Evaluator 基于快照序列计算得出。

##### 12. backtest_evaluations
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

#### 六、实盘
##### 13. live_sessions
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | |
| name | VARCHAR(100) | |
| portfolio_strategy_id | INTEGER FK | |
| mode | VARCHAR(10) | 模拟 / 实盘 |
| status | VARCHAR(10) | running / stopped / suspended |
| created_at | DATETIME | |
| updated_at | DATETIME | |
| started_at | DATETIME | NULL，最近一次启动时间 |
| stopped_at | DATETIME | NULL，最近一次停止时间 |

#### 表关系
```
users ──< sessions
stock_pools ──< stock_pool_stocks
formulas ──< formula_signals
portfolio_strategies ──< strategies ──< backtest_trades
portfolio_strategies ──< backtest_records ──< backtest_daily_snapshots
portfolio_strategies ──< backtest_records ──< backtest_evaluations
portfolio_strategies ──< backtest_records ──< backtest_trades
portfolio_strategies ──< live_sessions
strategies.master_strategy_id ──> strategies（自引用）
```

### 5.5 自研框架类设计（Engine 运行时）

#### 5.5.1 ORM 与 Engine 的关系
ORM（SQLAlchemy）负责数据库的存取，Engine 负责运行时计算。Engine 从数据库读取配置创建运行时对象，计算结果通过 services 层写回数据库。Engine 本身不持有数据库状态。

#### 5.5.2 类清单

##### models/（ORM 层，13 个类，与数据库表一一对应）
```
models/
├── user.py                  # User
├── session.py               # Session

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
└── live_session.py          # LiveSession
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
│   └── ExecutionEngine      # 下单、按比例缩减、不足1手放弃、资金不足放弃
│
├── evaluator.py             # 回测评估
│   └── Evaluator            # 读取 daily_snapshots → 计算 18 个指标 → 输出 BacktestEvaluation
│
├── backtest_engine.py       # 回测引擎
│   └── BacktestEngine       # 组装上述组件：初始化→逐bar迭代→事件分发→执行→评估
│
└── live_engine.py           # 实盘引擎
│   └── LiveEngine           # TQ 回调接收 bar→事件分发→执行，NATS 对接 iQuant 网关下单
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
live_sessions         ──创建──→  LiveEngine
```

### 5.6 REST API 设计

#### 5.6.1 通用规范
- 基础路径：`/api`
- 认证：Session，登录后 cookie 自动携带
- 返回格式：
```json
{ "code": 0, "message": "ok", "data": { ... } }
```
code=0 成功，非 0 为错误码

#### 5.6.2 认证
| 方法 | URL | 说明 |
|------|-----|------|
| POST | `/api/auth/login` | 登录 |
| POST | `/api/auth/logout` | 登出 |
| GET | `/api/auth/me` | 获取当前登录用户信息 |

**POST /api/auth/login**
```
请求: { "username": "admin", "password": "admin123" }
返回: { "code": 0, "data": { "id": 1, "username": "admin", "role": "admin" } }
```

#### 5.6.3 首页 - 运行状态
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

#### 5.6.4 股票池
| 方法 | URL | 说明 |
|------|-----|------|
| GET | `/api/stock-pools` | 获取股票池列表 |
| POST | `/api/stock-pools/{id}/sync` | 从通达信同步股票池 |
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

#### 5.6.5 公式管理
| 方法 | URL | 说明 |
|------|-----|------|
| GET | `/api/formulas` | 公式列表 |
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

#### 5.6.6 组合策略
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

#### 5.6.7 策略
| 方法 | URL | 说明 |
|------|-----|------|
| GET | `/api/portfolios/{pid}/strategies` | 策略列表 |
| POST | `/api/portfolios/{pid}/strategies` | 新增策略 |
| PUT | `/api/portfolios/{pid}/strategies/{id}` | 编辑策略 |
| DELETE | `/api/portfolios/{pid}/strategies/{id}` | 删除策略 |

**POST /api/portfolios/{pid}/strategies**
```
请求: { "name": "均线策略A", "formula_id": 1, "period": "1d", "role": "对立", "capital_ratio": 0.6, ... }
返回: { "code": 0, "data": { "id": 1 } }
```

#### 5.6.8 回测管理
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
返回（成功，子进程已启动）: { "code": 0, "data": { "id": 1, "status": "running" } }
返回（已有回测运行中）: { "code": 1, "message": "已有回测正在运行，请等待完成" }
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

#### 5.6.9 实盘交易
| 方法 | URL | 说明 |
|------|-----|------|
| GET | `/api/live/sessions` | 实盘列表 |
| POST | `/api/live/sessions` | 新建实盘 |
| POST | `/api/live/sessions/{id}/start` | 启动实盘 |
| POST | `/api/live/sessions/{id}/stop` | 停止实盘 |
| PUT | `/api/live/sessions/{id}` | 编辑实盘 |
| DELETE | `/api/live/sessions/{id}` | 删除实盘 |

**POST /api/live/sessions**
```
请求: { "name": "实盘测试", "portfolio_strategy_id": 1, "mode": "模拟" }
返回: { "code": 0, "data": { "id": 1, "status": "stopped" } }
```

#### 5.6.10 用户管理（管理员）
| 方法 | URL | 说明 |
|------|-----|------|
| GET | `/api/users` | 用户列表 |
| POST | `/api/users` | 新增用户 |
| PUT | `/api/users/{id}` | 编辑用户 |
| DELETE | `/api/users/{id}` | 删除用户 |

**POST /api/users**
```
请求: { "username": "researcher1", "password": "123456", "role": "researcher" }
返回: { "code": 0, "data": { "id": 2 } }
```

#### 5.6.11 系统配置（管理员，文件存储）
系统配置存储在项目根目录 `config.yaml`，不存数据库。

| 方法 | URL | 说明 |
|------|-----|------|
| GET | `/api/system/configs` | 读取配置文件全部内容 |
| PUT | `/api/system/configs` | 全量更新配置文件 |

**config.yaml 结构**
```yaml
tdx_backtest_path: D:\tdx\data
tdx_live_path: D:\tdx\data2
iquant_path: D:\iquant
max_concurrent_backtest: 1
```

**PUT /api/system/configs**
```
请求: { "tdx_backtest_path": "D:\\tdx\\data", "tdx_live_path": "D:\\tdx\\data2", "iquant_path": "D:\\iquant", "max_concurrent_backtest": 1 }
返回: { "code": 0 }
```

#### 5.6.12 接口汇总

```
POST   /api/auth/login
POST   /api/auth/logout
GET    /api/auth/me
GET    /api/status
GET    /api/stock-pools
POST   /api/stock-pools/{id}/sync
GET    /api/stock-pools/{id}/stocks
GET    /api/formulas
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
POST   /api/live/sessions
POST   /api/live/sessions/{id}/start
POST   /api/live/sessions/{id}/stop
PUT    /api/live/sessions/{id}
DELETE /api/live/sessions/{id}
GET    /api/users
POST   /api/users
PUT    /api/users/{id}
DELETE /api/users/{id}
GET    /api/system/configs
PUT    /api/system/configs
```

## 6.natsio
### 6.1 定位：连接核心后端（main 环境，Python 3.13）与国信 iQuant 网关（live 环境，Python 3.6.8）的唯一通信桥梁。Core 与 TQ 模块同进程直接调用，不走 NATS

### 6.2 通信模式
| 模式 | 场景 |
|------|------|
| 请求-响应（Request-Reply） | 所有操作：下单、查持仓、查状态 |

### 6.3 Subject 清单（共 3 个）

| 方向 | Subject | 模式 | 说明 |
|------|---------|------|------|
| Core→iQuant | `iquant.iguant.order.place` | 请求-响应 | 下单 |
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

## 7.用户管理
### 7.1 系统有三个权限：管理员、研究员、交易员
### 7.2 管理员：权限是最高权限（全部菜单可用），研究员：实盘交易、系统管理不可用，交易员：系统管理不可用
### 7.3 系统初始化管理为admin，密码 admin123
### 7.4 前端认证：使用 Session 方式，由 FastAPI 管理，SQLite 存储 session 数据

## 8.系统统一规则
### 8.1 系统中的股票代码统一都用带后缀的 如000001.SZ
### 8.2 系统中的数据统一用通达信的数据

## 9.须明确的事项
### 9.1 【已明确】策略资金模型规则→详见 5.3.2 业务边界第 8-9 条
### 9.2 【已明确】数据库表设计（13 张表）→详见 5.4
### 9.3 【已明确】自研框架类设计→详见 5.5
### 9.4 【已明确】natsio 消息格式与 Subject 设计→详见第 6 章
### 9.5 【已明确】实盘执行时机→下一个 tick 或下一个 bar 的 open，详见 5.3.2 业务边界第 3 条
### 9.6 【已明确】策略周期数据来源→通达信接口自动合成，详见 5.3.2 策略参数第 2 条
### 9.7 【已明确】REST API 协议设计→详见 5.6（11 组，43 个接口）
### 9.8 待明确：日志、监控、告警机制设计

## 10.开发顺序

### 第一阶段：基础设施（无依赖，可并行）
| # | 任务 | 内容 |
|---|------|------|
| 1 | main 环境 | 搭建 uv 环境（Python 3.13），初始化 FastAPI 项目骨架 |
| 2 | live 环境 | 搭建 uv 环境（Python 3.6.8），初始化 iQuant 网关骨架 |
| 3 | 前端项目 | Vite + Vue 初始化，安装依赖 |
| 4 | natsio | 安装并配置 natsio server，验证连通 |

### 第二阶段：数据层（依赖第一阶段）
| # | 任务 | 内容 |
|---|------|------|
| 5 | 数据库 | 13 张表 SQLAlchemy 建表、迁移脚本 |
| 6 | nats_client | Core 侧 natsio 客户端封装 |
| 7 | 前端框架 | 路由、功能树框架、页面占位 |
| 8 | 测试框架 | 配置 pytest + vitest，编写 conftest.py（测试数据库 fixtures） |

### 第三阶段：TQ 数据模块（依赖第二阶段，与 Core 同进程）
| # | 任务 | 内容 |
|---|------|------|
| 9 | TQ 数据模块 | 股票池获取、历史数据、bar 实时订阅、公式计算（进程内直接调用，无 NATS） |
| 10 | 公式管理 | 先写 API 集成测试 → formulas API + 前端页面 |

### 第四阶段：核心引擎（依赖第三阶段，每项 TDD：先写单元测试再写实现）
| # | 任务 | 内容 |
|---|------|------|
| 11 | 事件系统 | event.py + event_bus.py + 单元测试 |
| 12 | 数据+账户 | data_feed.py（调用 TQ 模块） + account.py + position.py + 单元测试 |
| 13 | 信号+风控 | signal_engine.py + risk_manager.py + 单元测试 |
| 14 | 执行引擎 | execution_engine.py + 单元测试 |
| 15 | 组合+策略 | portfolio.py + strategy_context.py + 单元测试 |
| 16 | 回测引擎 | backtest_engine.py（逐bar迭代→每交易日结束生成 daily_snapshot）+ 集成测试（小数据集端到端验证） |

### 第五阶段：回测（依赖第四阶段）
| # | 任务 | 内容 |
|---|------|------|
| 17 | 评估模块 | evaluator.py（读取 daily_snapshots → 计算 18 个指标）+ 单元测试（用固定快照序列验证指标计算） |
| 18 | 股票池管理 | 先写测试 → 股票池 API + 前端页面 |
| 19 | 策略管理 | 先写测试 → 组合策略+策略 API + 前端页面 |
| 20 | 回测管理 | 先写测试 → 启动/查询/交易明细/每日快照/评估结果 API + 前端页面 |

### 第六阶段：实盘（依赖第五阶段）
| # | 任务 | 内容 |
|---|------|------|
| 21 | 实盘引擎 | live_engine.py（TQ 回调接收 bar）+ 单元测试 |
| 22 | iQuant 网关 | 下单、持仓查询、状态（NATS 通信）+ Mock 测试 |
| 23 | 实盘管理 | 先写测试 → 创建/启动/停止 API + 前端页面 |

### 第七阶段：系统收尾
| # | 任务 | 内容 |
|---|------|------|
| 24 | 用户管理 | 先写测试 → 用户增删改查 + 登录认证 API + 页面 |
| 25 | 系统配置 | 先写测试 → 配置文件读写 API（config.yaml）+ 页面 |
| 26 | 首页仪表盘 | 运行状态展示 + 组件测试 |
| 27 | 日志监控 | 日志、监控、告警机制 |

### 依赖关系图
```
一: [1][2][3][4]
      │
二: [5][6][7]→[8]
      │
三: [9]→[10]
      │
四: [11]→[12]→[13]→[14]→[15]→[16]
      │
五: [17]→[18][19]→[20]
      │
六: [21]→[22]→[23]
      │
七: [24][25][26][27]
```
