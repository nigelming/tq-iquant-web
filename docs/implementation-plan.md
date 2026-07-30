# 实施方案

> 基于 `system-plan-draft.md` 设计文档生成。TDD 驱动，pytest + vitest。
> 每步先写测试，再写实现。

---

## 实现状态（截至 2026-07-30）

> 本节由 Claude Code 在从 opencode 迁移开发后补充，记录**真实完成度**。
> 文末进度表曾由 opencode 标注为全 100%，但实际多数模块为脚手架/桩代码，已据实修正。

**图例**：✅ 已实现　⚠️ 部分实现/有缺陷　❌ 未实现/桩代码

### 总体

opencode 已完成项目脚手架搭建：4 模块结构、14 张 ORM 表、7 个 API 路由注册、NATS 客户端、前端 5 视图、引擎层类骨架。但**核心业务逻辑多为空桩**（回测引擎空循环、实盘引擎全 `pass`、多数 API 返回 `{"code":0,"data":[]}`、iQuant 网关全 mock）。

### P0 配置链修复（2026-07-30 已完成）

原 `main/core/db.py` 硬编码 `sqlite:///./dev.db`、`main/alembic/env.py` 走 `alembic.ini` 硬编码 url，与 `config.yaml` 脱节。已统一为：db.py 与 alembic/env.py 均从 `core.config.load_config()` 读 `database.sqlite_path`；config.yaml 精简为仅 SQLite 配置（删 PG 的 host/port/user/password，未来切 PG 再加回）；config.py 引入 `_deep_merge` 修复嵌套配置默认值丢失。连带清理 `.env.template`、`manage.ps1` 的 `TQ_DB_PASSWORD`。数据库文件现位于 `main/data/dev.db`（`config.yaml` 的 `database.sqlite_path: data/dev.db`，相对 `main/` 解析），已加入 `.gitignore`。详见 `docs/status-audit.md`（如已生成）。

### 分阶段真实状态

| 任务 | 状态 | 说明 |
|---|---|---|
| 1.1 main 环境 | ✅ | FastAPI app + /health |
| 1.2 live 环境 | ✅ | 骨架 + mock 网关 |
| 1.3 前端项目 | ⚠️ | Vite+Vue+路由齐；Pinia 已装未启用，无 stores；无 SSE |
| 1.4 数据库 | ✅ | 现阶段固定 SQLite（P0 修复后配置链通）；docker-compose.yml 保留备用 |
| 1.5 NATS 连通测试 | ❌ | `test_nats_connectivity.py` 不存在 |
| 2.1 shared 包 | ✅ | constants/nats_schemas/stock_utils 齐 |
| 2.2 14 张 ORM 表 | ⚠️ | 14 表全齐；但 `live_session_portfolios` 缺 `circuit_breaker_count/created_at/updated_at` |
| 2.3 Alembic | ⚠️ | init + 迁移在；env.py 已接 config（P0 修复）；**级联删除仅 1 处、索引全缺** |
| 2.4 NATS 客户端 | ⚠️ | main 侧 NatsClient 在；live 侧无独立 nats_client 目录（网关内联） |
| 2.5 测试框架 | ⚠️ | conftest 在但 `dependency_overrides["get_db"]` 用字符串 key 是 bug；测试实际在 `core/tests/` 非计划写的 `tests/` |
| 3.1 TQ 模块 | ⚠️ | 接口骨架齐；`utils.py` 硬编码 `D:\new_tdx64` 路径；未实测连通通达信 |
| 3.2 公式 API+前端 | ❌ | API 仅 list 桩，缺 9 个；无前端公式页 |
| 4.1 事件系统 | ✅ | 5 类 event 齐；EventBus 风控优先已实现 |
| 4.2 数据源+账户+持仓 | ⚠️ | account/position 已实现；**`data_feed.py` 不存在** |
| 4.3 策略运行时+风控 | ⚠️ | risk_manager 已实现；strategy_context.get_signal 返回 `[]`；SignalEngine 框架；ExecutionEngine 部分 + `reduce_by_ratio` 末尾 `return None` bug |
| 4.4 组合策略运行时 | ⚠️ | Portfolio 骨架在；`on_bar` 是 `pass` |
| 4.5 回测引擎 | ❌ | `BacktestEngine.run` 空循环桩，无逐 bar 推进 |
| 5.1 评估模块 | ⚠️ | Evaluator 实算约 12/18 指标；win_rate/profit_factor 等 6 个硬编码 0（入参缺 trades） |
| 5.2 股票池 API+前端 | ⚠️ | API 3/5；前端仅列表展示，无新增/同步操作 |
| 5.3 策略 API+前端 | ❌ | API 1/4（list 桩）；缺 strategies 路由 |
| 5.4 回测 API+前端 | ❌ | API 1/6；**缺 `POST /api/backtest` 启动 + 409 并发冲突** |
| 6.1 实盘引擎 | ❌ | LiveEngine 4 方法全 `pass`；recover 未实现 |
| 6.2 iQuant 网关 | ⚠️ | 骨架在但全 mock；无 trade/nats_client 子目录 |
| 6.3 实盘 API+前端+SSE | ⚠️ | session CRUD 7/11；SSE 仅 ping 心跳；缺 portfolio 级 start/stop、orders、trades |
| 7.1 系统配置 | ✅ | GET/PUT configs + 前端页 |
| 7.2 首页仪表盘 | ⚠️ | `/api/status` 在但 iguant_gateway 固定 false；前端无仪表盘页 |
| 7.3 日志/监控/告警 | ⚠️ | logging_config 在；监控/告警骨架缺 |

### 待办优先级（下一步）

1. **P0**：补 `live_session_portfolios` 缺字段 + 新迁移（熔断恢复数据基础）
2. **P0**：补级联删除 + 设计要求的 9 组索引
3. **P1**：实现 `BacktestEngine.run` 真实逐 bar 逻辑 + `POST /api/backtest`
4. **P1**：补 `data_feed.py`、修 `execution_engine.reduce_by_ratio` bug、补 `StrategyContext.get_signal`
5. **P2**：Evaluator 缺失 6 指标、`LiveEngine.recover`、services 层、前端写操作 + SSE 双端
6. **P3**：修 TQ utils 硬编码路径、conftest override key bug、文档表数量/进度表

---

## 第一阶段：基础设施（无依赖，可并行）

### 1.1 main 环境

```
tq-iquant-web/main/
├── pyproject.toml
├── core/
│   ├── __init__.py
│   └── main.py              # FastAPI app 骨架（GET /health → {"ok": true}）
│   └── tests/
│       ├── __init__.py
│       └── test_main.py     # 测试 health endpoint
```

**步骤**：
1. `uv init main` — 初始化 Python 3.13 项目
2. `uv add fastapi uvicorn sqlalchemy asyncpg alembic pydantic nats-py pyyaml` — 安装依赖
3. `core/main.py` — FastAPI app + `/health` endpoint
4. 测试：GET /health → 200
5. 验证：`uv run uvicorn core.main:app --reload` 启动

**验收标准**：`/health` 返回 200 且启动无报错。

### 1.2 live 环境

```
tq-iquant-web/live/
├── pyproject.toml
└── iguant_gateway/
    ├── __init__.py
    └── main.py              # 网关骨架（等待 NATS 连接）
```

**步骤**：
1. `uv init live` — 初始化 Python 3.7 项目
2. `uv add nats-py pyyaml` — 安装依赖（Python 3.7 兼容版）
3. `iguant_gateway/main.py` — 骨架入口
4. 验证：`uv run python -c "import nats; print('ok')"` → ok

**验收标准**：项目结构就绪，无依赖错误。

### 1.3 前端项目

```
tq-iquant-web/web/
├── package.json
├── vite.config.ts
├── tsconfig.json
├── index.html
├── src/
│   ├── main.ts
│   ├── App.vue
│   ├── router/index.ts
│   ├── stores/    # Pinia
│   ├── views/
│   ├── components/
│   ├── api/
│   └── styles/
└── __tests__/
```

**步骤**：
1. `npm create vite@latest web -- --template vue-ts`
2. `npm install vue-router pinia axios`
3. `npm install -D vitest @vue/test-utils` — 测试框架
4. 配置 `vite.config.ts` — proxy `/api` → FastAPI
5. 配置 `vitest.config.ts`

**验收标准**：`npm run dev` 启动，`npx vitest run` 通过。

### 1.4 PostgreSQL

> **现状（2026-07-30）**：现阶段**固定使用 SQLite**，不启 PostgreSQL。`docker-compose.yml` 保留备用（未来切 PG 时现成配置），但开发期不依赖。`TQ_DB_PASSWORD` 环境变量已从 `.env.template`/`manage.ps1` 移除。

```yaml
# docker-compose.yml（项目根目录）
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: tq_iquant
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: ${TQ_DB_PASSWORD}
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data

  nats:
    image: nats:latest
    ports:
      - "4222:4222"
```

**步骤**：
1. 写 `docker-compose.yml`
2. 创建 `.env` 文件（`TQ_DB_PASSWORD=...`，不提交 git）
3. 验证：`docker compose up -d` → 数据库 + NATS ready

**验收标准**：`psql -h localhost -U postgres -d tq_iquant -c "SELECT 1"` 通过。

### 1.5 NATS 连通验证

```
main/tests/integration/test_nats_connectivity.py
```

**步骤**：
1. 集成测试：Core（main env）→ NATS → 连通
2. 验证发布/订阅/请求-响应链路

**验收标准**：NATS 连接测试通过。

---

## 第二阶段：数据层（依赖第一阶段）

### 2.1 shared 包

```
tq-iquant-web/shared/
├── pyproject.toml
└── tq_iquant_shared/
    ├── __init__.py
    ├── nats_schemas.py       # NATS 消息数据结构（dataclasses）
    ├── stock_utils.py        # validate_stock_code() 等
    └── constants.py          # 枚举常量（OrderStatus, SignalType 等）
```

**关键约束**：必须兼容 Python 3.7 — 不使用 walrus/match/pydantic v2/内置泛型

```python
# constants.py 示例
from enum import Enum
from typing import List, Dict, Optional

class SignalType(str, Enum):
    OPEN = "OPEN"
    ADD = "ADD"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
```

**测试**：
```python
def test_validate_stock_code():
    assert validate_stock_code("000001.SZ") == True
    assert validate_stock_code("000001") == False
```

**步骤**：
1. `pyproject.toml` — 定义共享包
2. `constants.py` — 所有枚举
3. `nats_schemas.py` — NATS 通信数据结构
4. `stock_utils.py` — 工具函数
5. 测试：`main/` 和 `live/` 都能 `import tq_iquant_shared`

### 2.2 数据库模型（14 张 ORM 表）

```
main/core/models/
├── __init__.py               # 导入所有模型
├── base.py                   # DeclarativeBase
├── stock_pool.py             # StockPool
├── stock_pool_stock.py       # StockPoolStock
├── formula.py                # Formula
├── formula_signal.py         # FormulaSignal
├── portfolio_strategy.py     # PortfolioStrategy
├── strategy.py               # Strategy
├── backtest_record.py        # BacktestRecord
├── backtest_trade.py         # BacktestTrade
├── backtest_daily_snapshot.py# BacktestDailySnapshot
├── backtest_evaluation.py    # BacktestEvaluation
├── live_session.py           # LiveSession
├── live_session_portfolio.py # LiveSessionPortfolio  ← 新增多组合策略关联表
├── live_order.py             # LiveOrder
└── live_trade.py             # LiveTrade
```

**TDD 顺序**：每张表先写 ORM 定义，然后写 schema 验证测试。

**步骤**：
1. 写 `base.py` — SQLAlchemy DeclarativeBase
2. 按依赖顺序写 model：StockPool → StockPoolStock → Formula → FormulaSignal → PortfolioStrategy → Strategy → BacktestRecord → BacktestTrade → BacktestDailySnapshot → BacktestEvaluation → LiveSession → LiveSessionPortfolio → LiveOrder → LiveTrade
3. 写每个 model 的字段定义和关系
4. 验证：`uv run python -c "from core.models import *"` 正常

### 2.3 Alembic 初始化

> **现状（2026-07-30）**：init 迁移已生成（`97323b81fdcc_init_14_tables.py`，14 张表）。`alembic/env.py` 已接入 `core.config.load_config()` 读 `database.sqlite_path`，`alembic.ini` 的 `sqlalchemy.url` 已置空（P0 配置链修复）。
> **遗留**：设计要求的级联删除规则仅落地 1 处（live_session_portfolios→live_sessions CASCADE），索引设计（9 组）完全缺失，待补。

```bash
cd main
uv run alembic init alembic
# 修改 alembic/env.py → 指向 core.models.Base.metadata
uv run alembic revision --autogenerate -m "init 14 tables"
uv run alembic upgrade head
```

**步骤**：
1. `alembic init alembic`
2. 配置 `alembic/env.py` — 连接字符串从 `config.yaml` + 环境变量读取
3. `alembic revision --autogenerate`
4. `alembic upgrade head`
5. 验证：`psql` 登录查看表结构

### 2.4 NATS 客户端封装

```
main/core/nats_client/
├── __init__.py
└── client.py                 # NatsClient — request/reply 封装

live/iguant_gateway/nats_client/
├── __init__.py
└── client.py                 # NatsClient — 同上，Python 3.7 兼容
```

```python
# client.py 接口
class NatsClient:
    async def connect(self, url: str) -> None
    async def close(self) -> None
    async def request(self, subject: str, data: dict, timeout: float = 5.0) -> Optional[dict]
    async def subscribe(self, subject: str, handler: Callable) -> None
```

**测试**：
```python
# tests/unit/test_nats_client.py（Mock NATS 连接）
def test_request_timeout():
    client = NatsClient()
    # 模拟超时，验证返回 None
```

### 2.5 测试框架

```
main/tests/
├── conftest.py               # 全局 fixtures
├── unit/                     # 单元测试（mock DB）
│   ├── conftest.py           # 内存 SQLite fixtures
│   ├── models/               # ORM 模型测试
│   ├── engine/               # 引擎模块测试
│   └── services/             # 业务层测试
└── integration/              # 集成测试（真实 PostgreSQL）
    ├── conftest.py           # 测试数据库 fixtures
    └── api/                  # API 接口测试
```

```python
# conftest.py 关键 fixture
@pytest.fixture
def db_session():
    """内存 SQLite，每次测试独立事务"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    yield session
    session.close()

@pytest.fixture
def test_client(db_session):
    """FastAPI TestClient with DB override"""
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)
```

**步骤**：
1. 写 `conftest.py`
2. 写第一个测试：`tests/unit/test_first.py` — assert True
3. 验证：`uv run pytest tests/` 通过


## 第三阶段：TQ 数据模块

### 3.1 TQ 模块初始化

通过通达信 tqcenter SDK（`PYPlugins/sys/tqcenter.py`）与运行中的通达信进程通信，无需 mock，无需 pytdx。

```
main/core/tq/
├── __init__.py
├── utils.py                  # tqcenter 连接管理 + 全局锁
├── data.py                   # 股票池、历史数据、实时订阅
└── formula.py                # 公式计算（zb/xg）
```

```python
# utils.py — 核心接口
def init_tq(tdx_path: str) -> None:
    """sys.path 注入 PYPlugins/sys + PYPlugins/user → import tqcenter.tq
       → tq.initialize(__file__) 连接到通达信进程"""

def close_tq() -> None:
    """tq.close() 断开连接"""

def get_tdx_lock() -> threading.Lock:
    """全局锁，TDX C 扩展非线程安全"""

def get_tq() -> module:
    """获取 tqcenter.tq 模块实例"""

# data.py — 数据获取
class TQData:
    def get_stock_pools(self) -> List[dict]:       # tq.get_sector_list
    def get_pool_stocks(self, pool_name) -> List[dict]:  # tq.get_stock_list_in_sector
    def get_all_stocks(self, market="5") -> List[str]:   # 5=全A股
    def get_history(self, stocks, periods, start="", end="",
                    dividend_type="front", count=100) -> Dict:  # tq.get_market_data
    def subscribe_bars(self, stocks, periods, callback):        # tq.subscribe_hq

# formula.py — 公式计算
class TQFormula:
    def compute(self, formula_name, formula_arg, stocks,
                period="1d", count=10) -> dict:      # tq.formula_process_mul_zb
    def compute_xg(self, formula_name, formula_arg, stocks,
                   period="1d") -> dict:             # tq.formula_process_mul_xg
```

**验证**：
```python
# 直接连接运行中通达信验证
from core.tq import init_tq, TQData, TQFormula
init_tq(r"D:\new_tdx64")
tqdata = TQData()
stocks = tqdata.get_all_stocks()          # 5543 只 A 股
df = tqdata.get_history(["000001.SZ"], ["1d"], count=5)
```

**验收标准**：通达信开启状态下能获取到真实数据。

### 3.2 公式管理 API + 前端

```
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
```

**TDD 顺序**：
1. 写 formula API 集成测试
2. 实现 formula_service
3. 实现 formula model + formula_signal model
4. 前端公式管理页面
5. 公式测试运行（调用 TQ 模块）

---

## 第四阶段：核心引擎（TDD 驱动，每项先写单元测试）

### 4.1 事件系统

```
main/core/engine/
├── event.py                  # 5 个事件类
└── event_bus.py              # 事件总线 + 优先级排序
```

```python
# event.py
@dataclass
class BarEvent:
    stocks: Dict[str, Dict[str, polars.DataFrame]]  # {stock: {period: df}}
    bar_time: datetime

@dataclass
class SignalEvent:
    strategy_id: int
    stock_code: str
    signal_name: str
    signal_type: SignalType
    bar_time: datetime

@dataclass
class RiskEvent:
    strategy_id: Optional[int]
    rule: str                  # stop_loss / take_profit / max_drawdown 等
    stock_code: Optional[str]
    bar_time: datetime

@dataclass
class OrderEvent:
    strategy_id: int
    stock_code: str
    trade_type: str            # BUY / SELL
    signal_type: SignalType
    quantity: int
    price: Optional[float]
    bar_time: datetime

@dataclass
class TradeEvent:
    strategy_id: int
    stock_code: str
    trade_type: str
    price: float
    quantity: int
    amount: float
    commission: float
    stamp_duty: float
    trade_time: datetime
```

```python
# event_bus.py
class EventBus:
    def publish(self, event: Any) -> None        # 发布事件
    def subscribe(self, event_type: type, handler: Callable) -> None
    def process_signals(self, signals: List[SignalEvent]) -> List[OrderEvent]
        """排序规则：风控 > 信号；同策略 CLOSE > REDUCE > ADD > OPEN"""
```

**TDD**：
```python
def test_signal_priority():
    bus = EventBus()
    signals = [
        SignalEvent(..., signal_type="OPEN", ...),
        RiskEvent(..., rule="stop_loss", ...),
    ]
    result = bus.process_signals(signals)
    assert result[0] is RiskEvent  # 风控优先
```

### 4.2 数据源 + 账户 + 持仓

```
main/core/engine/
├── data_feed.py              # DataFeed — 调用 TQData/TQFormula 获取 K 线和公式信号
├── account.py                # Account — 资金管理
└── position.py               # Position — 持仓
```

```python
# account.py
class Account:
    initial_capital: Decimal
    cash: Decimal
    market_value: Decimal
    total_value: Decimal       # = cash + market_value

    def approve_order(self, order: OrderEvent, position_value: Decimal) -> Tuple[bool, int]:
        """检查剩余现金是否足够，返回（是否批准，批准股数）"""

    def deduct_cash(self, amount: Decimal) -> None   # 买入扣现金
    def add_cash(self, amount: Decimal) -> None       # 卖出入现金

# position.py
class Position:
    stock_code: str
    quantity: int
    avg_cost: Decimal
    highest_price: Decimal     # 移动止损用

    def buy(self, quantity: int, price: Decimal) -> None
    def sell(self, quantity: int, price: Decimal) -> Tuple[Decimal, Decimal]
        # 返回 (pnl, sell_amount)
    def update_high(self, price: Decimal) -> None
```

**TDD**：
```python
def test_account_approve_insufficient():
    acc = Account(initial_capital=100000)
    order = OrderEvent(..., trade_type="BUY", quantity=10000, price=15)
    approved, qty = acc.approve_order(order, 0)
    assert approved == True    # 10000*15=150000 > cash 100000 → 缩减
    assert qty < 10000

def test_position_sell():
    pos = Position("000001.SZ")
    pos.buy(1000, Decimal("10"))
    pos.buy(500, Decimal("12"))
    pnl, amount = pos.sell(300, Decimal("14"))
    assert pnl > 0
```

### 4.3 策略运行时 + 风控

```
main/core/engine/
├── strategy_context.py        # StrategyContext
├── risk_manager.py            # PortfolioRiskManager + StrategyRiskManager
├── signal_engine.py           # SignalEngine
├── execution_engine.py        # ExecutionEngine
```

```python
# strategy_context.py
class StrategyContext:
    strategy_id: int
    formula: Formula
    period: str
    capital_ratio: Decimal
    positions: Dict[str, Position]      # 当前持仓
    open_count: int                     # 当前持仓数

    def get_signal(self, bar: BarEvent) -> List[SignalEvent]

# risk_manager.py
class StrategyRiskManager:
    def check_stop_loss(self, position: Position, current_price: Decimal) -> bool
    def check_take_profit(self, position: Position, current_price: Decimal) -> bool
    def check_trailing_stop(self, position: Position, current_price: Decimal) -> bool

class PortfolioRiskManager:
    max_drawdown: Decimal
    daily_loss_limit: Decimal
    consecutive_drawdown_triggers: int  # 累计触发次数，>=3 转手动恢复
    circuit_breaker_active: bool        # 熔断中

    def check_max_drawdown(self, current_value: Decimal, peak_value: Decimal) -> bool
    def check_daily_loss(self, daily_pnl: Decimal, initial_value: Decimal) -> bool
    def daily_reset(self) -> None       # 每日重置熔断状态

# signal_engine.py
class SignalEngine:
    def process(self, formula_signals: List[SignalEvent],
                risk_events: List[RiskEvent]) -> List[OrderEvent]

# execution_engine.py — 回测/实盘共用，差异通过策略模式隔离
class OrderDispatcher(ABC):
    """下单接口：回测模拟成交，实盘通过 NATS"""
    @abstractmethod
    def place_order(self, order: OrderEvent, portfolio_id: int) -> TradeEvent

class SimulatedDispatcher(OrderDispatcher):
    """回测：按 next_bar.open 模拟成交"""

class NatsDispatcher(OrderDispatcher):
    """实盘：通过 NATS 发往 iQuant 网关"""

class T1Checker(ABC):
    """T+1 接口：回测直接返回持仓，实盘查 iQuant"""
    @abstractmethod
    def get_available_shares(self, stock_code: str, portfolio_id: int) -> int

class SimulatedT1Checker(T1Checker):
    """回测：持仓量即可卖出"""

class LiveT1Checker(T1Checker):
    """实盘：查 iQuant 实际可用股数"""

class ExecutionEngine:
    def __init__(self, dispatcher: OrderDispatcher, t1_checker: T1Checker)
    def execute(self, order: OrderEvent, account: Account,
                position: Optional[Position], portfolio_id: int) -> Optional[TradeEvent]
    def reduce_by_ratio(self, position: Position, ratio: Decimal) -> OrderEvent
```

**TDD**：
```python
def test_stop_loss_trigger():
    rm = StrategyRiskManager(stop_loss_ratio=Decimal("0.05"))
    pos = Position("000001.SZ")
    pos.buy(1000, Decimal("10"))
    assert rm.check_stop_loss(pos, Decimal("9.4")) == True   # 跌 6% > 5%
    assert rm.check_stop_loss(pos, Decimal("9.6")) == False  # 跌 4% < 5%

def test_execution_insufficient_hand():
    ee = ExecutionEngine()
    order = OrderEvent(..., quantity=100)  # 1手
    # 批准 50 股 → 不足 1 手 → 放弃
    trade = ee.execute(order, account, None)
    assert trade is None
```

### 4.4 组合策略运行时

```
main/core/engine/
└── portfolio.py               # Portfolio
```

```python
# portfolio.py
class Portfolio:
    portfolio_id: int
    account: Account
    strategies: List[StrategyContext]
    strategy_risk_managers: Dict[int, StrategyRiskManager]
    portfolio_risk_manager: PortfolioRiskManager
    benchmark_data: polars.DataFrame        # 基准指数日线

    def on_bar(self, bar: BarEvent) -> None
        """逐 bar 回调：策略信号 → 风控 → 执行"""
    def snapshot(self) -> BacktestDailySnapshot
        """每日快照"""
    def check_circuit_breaker(self) -> bool
        """检查组合级熔断"""
```

### 4.5 回测引擎

```
main/core/engine/
└── backtest_engine.py         # BacktestEngine
```

```python
# backtest_engine.py
class BacktestEngine:
    def run(self, portfolio: Portfolio, klines: Dict,
            signal_cache: Dict, benchmark_data: polars.DataFrame,
            progress_callback: Callable) -> BacktestResult:
        """逐 bar 推进 → 事件分发 → 快照 → 评估"""
```

**回测流程**：
```
1. 确定最小周期（所有策略的 period 最小值）
2. 按最小周期逐时间点推进
3. 每个时间点检查各周期 bar 是否结束
4. 已结束 → 触发对应周期的公式信号
5. 风控检查 → 排序 → 执行 → 更新持仓/资金
6. 交易日结束时生成 daily_snapshot
7. 更新 progress（0~100）
8. 回测结束 → 写入 trades/snapshots/evaluations
```

**接受标准**：小数据集端到端回测通过（验证多周期推进、交易记录、快照完整）。

---

## 第五阶段：回测

### 5.1 评估模块

```
main/core/engine/
└── evaluator.py               # Evaluator
```

```python
# evaluator.py
class Evaluator:
    def evaluate(self, snapshots: List[BacktestDailySnapshot],
                 benchmark_data: polars.DataFrame) -> BacktestEvaluation:
        """18 个指标计算"""
```

**18 个指标计算公式**（已确认，详见设计文档 5.3.2.5）：
- 基准收益率：从 benchmark_data 取对应日期区间
- 回撤相关：max_drawdown / avg_recovery_days / max_recovery_days / ulcer_index
- 风险指标：VaR 95% / CVaR 95%
- 收益稳定性：R² 线性回归拟合度

**TDD**：
```python
def test_evaluator_win_rate():
    snapshots = [mock_snapshots]  # 固定快照序列
    ev = Evaluator.evaluate(snapshots, benchmark_data)
    assert ev.win_rate == expected

def test_sharpe_ratio():
    ev = Evaluator.evaluate(mock_snapshots, benchmark_data)
    assert abs(ev.sharpe_ratio - expected) < 0.001
```

### 5.2 股票池 API + 前端

```
GET    /api/stock-pools
GET    /api/stock-pools/tdx         # 通达信未同步的股票池
POST   /api/stock-pools/{id}/sync   # 全量替换
GET    /api/stock-pools/{id}
GET    /api/stock-pools/{id}/stocks
```

**TDD 顺序**：
1. API 集成测试
2. StockPoolService
3. StockPool + StockPoolStock models
4. TQ pool 调用
5. 前端页面

### 5.3 策略 API + 前端

```
GET    /api/portfolios
POST   /api/portfolios
GET    /api/portfolios/{id}
PUT    /api/portfolios/{id}
DELETE /api/portfolios/{id}
GET    /api/portfolios/{pid}/strategies
POST   /api/portfolios/{pid}/strategies
PUT    /api/portfolios/{pid}/strategies/{id}
DELETE /api/portfolios/{pid}/strategies/{id}
```

**TDD 顺序**：
1. API 集成测试
2. StrategyService
3. PortfolioStrategy + Strategy models
4. 前端组合策略管理页面

### 5.4 回测 API + 前端

```
POST   /api/backtest               # 启动回测
GET    /api/backtest/records       # 回测列表
GET    /api/backtest/records/{id}  # 详情 + 评估
GET    /api/backtest/records/{id}/trades
GET    /api/backtest/records/{id}/snapshots
GET    /api/backtest/records/{id}/results
```

**TDD 顺序**：
1. API 集成测试（含并发冲突 409）
2. BacktestService（ProcessPoolExecutor 提交）
3. 前端回测页面

**特殊测试**：
```python
def test_concurrent_backtest_rejected(test_client):
    # 启动回测
    resp1 = test_client.post("/api/backtest", json=...)
    assert resp1.status_code == 200
    # 再次启动 → 409
    resp2 = test_client.post("/api/backtest", json=...)
    assert resp2.status_code == 409
```

---

## 第六阶段：实盘

### 6.1 实盘引擎

```
main/core/engine/
└── live_engine.py               # LiveEngine
```

```python
# live_engine.py
class LiveEngine:
    session_id: int
    portfolios: List[Portfolio]          # 多个组合策略
    tq_data: TQData                       # TDX 数据（通过 tqcenter）
    nats_client: NatsClient              # iQuant 通信
    sse_manager: SSEManager               # SSE 实时推送

    async def start(self) -> None
        """加载配置 → 重建虚拟持仓 → 恢复未完成订单 → 订阅 bar"""
    async def stop(self) -> None
        """取消订阅 → 关闭 NATS → 更新 DB"""

    async def on_bar(self, bar: BarEvent) -> None
        """bar 回调 → 按 portfolio 分发 → 执行"""
    async def on_order_update(self, nats_msg: dict) -> None
        """订单状态更新 → live_orders → 虚拟持仓调整"""
```

**恢复逻辑**：
```python
async def recover(self) -> None:
    """Core 重启恢复"""
    # 1. 查 live_session_portfolios → 加载配置
    # 2. 聚合 live_trades → 重算虚拟持仓 + 虚拟现金
    # 3. 通过 NATS 查 iQuant 实际持仓，交叉验证
    # 4. 重建 TQ 订阅（所有 portfolio 股票并集）
    # 5. 查询未完成订单状态
```

**TDD**：
```python
def test_virtual_position_recovery():
    engine = LiveEngine(session_id=1)
    engine.recover()
    assert engine.portfolios[0].account.cash == expected_cash
    assert engine.portfolios[0].positions["000001.SZ"].quantity == expected_qty
```

### 6.2 iQuant 网关

```
live/iguant_gateway/
├── main.py                     # 入口
├── trade/
│   ├── __init__.py
│   ├── order.py                # 下单、查订单、撤单
│   └── position.py             # 持仓查询
└── nats_client/
    ├── __init__.py
    └── client.py               # NATS 客户端（Python 3.7 兼容）
```

```python
# 网关 NATS handler
# iquant.iguant.order.place
async def handle_place_order(msg):
    data = json.loads(msg.data)
    order_id = xtquant.order_place(
        stock_code=data["stock_code"],
        price=data.get("price"),
        quantity=data["quantity"],
        order_type=data.get("order_type", "market"),
    )
    # 回复请求
    await msg.respond(json.dumps({"success": True, "data": {"order_id": order_id}}))
```

**测试**：Mock xtquant 库，验证 NATS 请求-响应链路。

**关键实现点**：
- Python 3.7 兼容的 async/await（asyncio 原生）
- xtquant API 封装（下单/撤单/查询）
- 网关无状态，所有订单状态由 Core 管理

### 6.3 实盘管理 API + 前端 + SSE

```
POST   /api/live/sessions                # { "name": "...", "mode": "simulation", "portfolio_ids": [1,2] }
GET    /api/live/sessions
GET    /api/live/sessions/{id}           # 含组合策略列表 + 虚拟持仓
POST   /api/live/sessions/{id}/start     # 启动全部
POST   /api/live/sessions/{id}/stop      # 停止全部
PUT    /api/live/sessions/{id}
DELETE /api/live/sessions/{id}
GET    /api/live/sessions/{id}/orders?portfolio_id=1
GET    /api/live/sessions/{id}/trades?portfolio_id=1
WS     /api/live/sessions/{id}/stream
```

**SSE 前端**：
```typescript
// web/src/stores/live.ts
const sse = new EventSource(`/api/live/sessions/${id}/stream`)
sse.addEventListener('signal',   (e) => { /* 信号触发 */ })
sse.addEventListener('order',    (e) => { /* 订单状态 */ })
sse.addEventListener('trade',    (e) => { /* 成交 */ })
sse.addEventListener('position', (e) => { /* 持仓变化 */ })
sse.addEventListener('risk',     (e) => { /* 风控触发 */ })
sse.addEventListener('ping',     (e) => { /* 心跳，无需处理 */ })
```

**TDD 顺序**：
1. live session API 集成测试（含多组合策略场景）
2. LiveService（Engine 生命周期管理 + NAS 下单）
3. SSE 推送
4. 前端实盘管理页面

---

## 第七阶段：系统收尾

### 7.1 系统配置

```
GET    /api/system/configs
PUT    /api/system/configs
```

**步骤**：
1. config.yaml 读写封装（pyyaml）
2. API 实现（密码字段过滤/回写空）
3. 前端系统配置页面

### 7.2 首页仪表盘

```
GET    /api/status
```

**步骤**：
1. 状态聚合（Core 运行状态 + TQ 状态 + iQuant 网关状态 + NATS 状态）
2. 前端仪表盘页面

### 7.3 日志/监控/告警（骨架）

```python
# main/core/logging_config.py
LOGGING_CONFIG = {
    "level": "INFO",
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
        "file": {"class": "logging.handlers.RotatingFileHandler",
                 "filename": "logs/core.log", "maxBytes": 10*1024*1024, "backupCount": 5},
    },
}
```

**步骤**：
1. 配置 logging（文件 + 控制台）
2. 关键路径加结构化日志（operation_id 关联请求链路）
3. 简单告警通知（日志级别触发 → 暂为文件告警，后续可扩展）

---

## 开发进展跟踪

> **注意（2026-07-30）**：以下为**真实完成度**，已替换 opencode 此前标注的全 100% 进度。各阶段内部任务完成度差异较大，详见上方"实现状态"节。

```
第一阶段：基础设施   ███████████░░░░░░░░░  ~55%  [#1-#5]  环境齐，DB 固定 SQLite，NATS 连通测试缺
第二阶段：数据层     ██████████████░░░░░░  ~70%  [#6-#9]  14 表+shared 在，级联/索引缺
第三阶段：TQ 模块    ███████░░░░░░░░░░░░░  ~35%  [#10-#11] 接口骨架在，未实测，公式 API/前端缺
第四阶段：核心引擎   ████████░░░░░░░░░░░░  ~40%  [#12-#17] 事件/风控/账户在，回测引擎+数据源缺
第五阶段：回测       ████░░░░░░░░░░░░░░░░  ~20%  [#18-#21] Evaluator 部分，回测 API/引擎缺
第六阶段：实盘       ████░░░░░░░░░░░░░░░░  ~20%  [#22-#24] session CRUD 在，引擎/recover/网关缺
第七阶段：收尾       ██████████░░░░░░░░░░  ~50%  [#25-#27] 配置/日志在，仪表盘/告警缺
```

每条任务完成后标记进度。回测端到端链路打通后（#17）进入可验证状态。**当前尚未到达可验证状态**——`BacktestEngine.run` 仍为空桩。
