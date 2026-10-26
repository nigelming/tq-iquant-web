# 实盘信号预警（alert / send_warn）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让实盘策略在 `alert`（半自动）模式下按可配置频率计算 TQ 导入公式、落 `live_signals`、通过通达信 `send_warn` 推送预警并发 SSE，且不触发任何下单；`auto` 保留现有自动交易链路。

**Architecture:** 新增 `SignalScheduler` 负责频率窗口，`SignalMonitor` 负责命中/边沿去重/落库/SSE，`TQAlert` 负责 `send_warn`，`AlertDataDispatcher` 提供无交易账号的 TQ 行情。`LiveEngine` 的 alert 分支只拉行情、算公式、推送；auto 分支在 `Portfolio.on_bar` 前统一由 `SignalMonitor` 记录公式信号。

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy 2 / SQLite / Alembic / pytest；通达信 TQ 同进程接口；Vue 前端 P1 不在本计划。

## Global Constraints

- 实盘信号只接受通达信导入公式：`Formula.engine == "tq"`；`builtin` 仅回测/研究，alert 启动必须 503。
- `alert` 模式绝不调用 `Portfolio.on_bar`、`ExecutionEngine` 或任何下单 dispatcher；`AlertDataDispatcher.place_order()` 必须抛错。
- `alert` 模式行情固定走同进程 TQ；不使用 iQuant 桥。
- 新会话默认 `execution_mode=alert`；迁移前已有会话写 `auto`，保持升级前行为。
- `Strategy.signal_frequency` 不得细于 `Strategy.period`；`auto` 下 P0 必须 `signal_frequency == period`（NULL 视为 period），否则启动 503。
- `1w/1mon` 在 P0 只允许 `signal_data_end=completed`；不得用未完成周/月 bar。
- 唯一键：`(live_session_id, portfolio_strategy_id, strategy_id, stock_code, signal_name, bar_time)`。
- `bar_time` 是数据窗口时点：日内=bar 时间；`1d`=当日 `signal_at`；`1w`=上周最后交易日 15:00；`1mon`=上月最后交易日 15:00。
- 所有时间按 Asia/Shanghai；周一休市顺延到同一周下一个交易日同一时刻。
- 启动补算窗口 `catch_up_minutes=30`，只补当前数据窗口；超窗跳过并 `send_message` 汇总，不补历史。
- 迁移用 Alembic，禁止手改表；当前 head 为 `a9b8c7d6e5f4`。
- 不新增第三方依赖；不改 `live/` 桥（Python 3.6）。
- 后端测试从仓库根运行：`uv run --project main pytest main/core/tests/...`。
- 本计划只做 P0 后端 + API/SSE；Web 信号面板、重发、失败退避属 P1，不在本计划。
- 为兼容现有直接构造 `LiveEngine` 的测试：`LiveEngine(signal_monitor=None)` 时保留旧 `signal` SSE 行为；生产 `build_engine` 必须注入 `SignalMonitor`。

---

### Task 1: 数据模型与 Alembic 迁移

**Files:**
- Create: `main/core/models/live_signal.py`
- Create: `main/alembic/versions/f0a1b2c3d4e5_add_live_signal_alert.py`
- Modify: `main/core/models/live_session.py`, `main/core/models/strategy.py`, `main/core/models/__init__.py`
- Test: `main/core/tests/unit/test_live_signal_model.py`

**Interfaces:**
- Consumes: `Base`, existing `LiveSession`/`Strategy` models.
- Produces: `LiveSignal` with fields `live_session_id, portfolio_strategy_id, strategy_id, formula_id, stock_code, period, bar_time, formula_name, signal_name, signal_type, trigger_value, price, execution_mode, status, alert_status, alert_error, tdx_run_id, alerted_at, created_at`; `LiveSession.execution_mode`; `Strategy.signal_frequency/signal_at/signal_weekday/signal_data_end`.

- [ ] **Step 1: Write the failing model test**

```python
# main/core/tests/unit/test_live_signal_model.py
from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.models import (
    Base, Formula, LiveSession, LiveSignal, PortfolioStrategy, Strategy, StockPool,
)


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_live_signal_defaults_and_strategy_schedule_fields():
    db = _db()
    session = LiveSession(name="s", mode="simulation", status="stopped",
                          engine="tdx", execution_mode="alert")
    pool = StockPool(code="P", name="p")
    db.add_all([session, pool])
    db.flush()
    formula = Formula(name="momentum", content="C")
    db.add(formula)
    db.flush()
    port = PortfolioStrategy(name="p", stock_pool_id=pool.id,
                             initial_capital=Decimal("100000"))
    db.add(port)
    db.flush()
    strat = Strategy(portfolio_id=port.id, name="s1", formula_id=formula.id,
                     period="1d", role="master", signal_frequency="1w",
                     signal_at="09:00", signal_weekday=1,
                     signal_data_end="completed")
    db.add(strat)
    db.flush()
    row = LiveSignal(
        live_session_id=session.id, portfolio_strategy_id=port.id,
        strategy_id=strat.id, stock_code="000001.SZ", period="1d",
        bar_time=datetime(2026, 10, 23, 15, 0),
        formula_name="momentum", signal_name="buy",
        signal_type="OPEN", trigger_value=1, price=Decimal("12.34"),
        execution_mode="alert",
    )
    db.add(row)
    db.commit()
    assert row.status == "new"
    assert row.alert_status is None
    assert strat.signal_frequency == "1w"
    assert strat.signal_weekday == 1
    assert session.execution_mode == "alert"


def test_live_signal_unique_key_rejects_duplicate():
    import pytest
    from sqlalchemy.exc import IntegrityError

    db = _db()
    session = LiveSession(name="s", mode="simulation", status="stopped",
                          engine="tdx", execution_mode="alert")
    pool = StockPool(code="P", name="p")
    db.add_all([session, pool])
    db.flush()
    formula = Formula(name="momentum", content="C")
    db.add(formula)
    db.flush()
    port = PortfolioStrategy(name="p", stock_pool_id=pool.id,
                             initial_capital=Decimal("100000"))
    db.add(port)
    db.flush()
    strat = Strategy(portfolio_id=port.id, name="s1", formula_id=formula.id,
                     period="1d", role="master")
    db.add(strat)
    db.flush()
    kwargs = dict(
        live_session_id=session.id, portfolio_strategy_id=port.id,
        strategy_id=strat.id, stock_code="000001.SZ", period="1d",
        bar_time=datetime(2026, 10, 23, 15, 0), formula_name="f",
        signal_name="buy", signal_type="OPEN", trigger_value=1,
        price=Decimal("12.34"), execution_mode="alert",
    )
    db.add(LiveSignal(**kwargs))
    db.commit()
    db.add(LiveSignal(**kwargs))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project main pytest main/core/tests/unit/test_live_signal_model.py -q`
Expected: FAIL with `ImportError: cannot import name 'LiveSignal'` or missing `execution_mode`.

- [ ] **Step 3: Add models**

`main/core/models/live_session.py` add after `engine`:

```python
    # 执行模式：alert（半自动，只预警不下单）/ auto（现有自动交易）。
    # 迁移前历史会话写 auto；新建会话由 live_service 显式写 alert。
    execution_mode = Column(String(10), nullable=False, server_default="auto")
```

`main/core/models/strategy.py` add after `period`:

```python
    # 信号推送频率（可等于或粗于 period）；NULL = 使用 period。
    signal_frequency = Column(String(10), nullable=True)
    # HH:MM；NULL = 按频率默认（1d=14:30，1w/1mon=09:00）。
    signal_at = Column(String(5), nullable=True)
    # 1-5，仅 1w 使用；NULL = 周一。
    signal_weekday = Column(Integer, nullable=True)
    # completed / current；NULL = 1w/1mon 用 completed，其余 current。
    signal_data_end = Column(String(10), nullable=True)
```

`main/core/models/live_signal.py`:

```python
from sqlalchemy import (
    Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from .base import Base


class LiveSignal(Base):
    __tablename__ = "live_signals"

    id = Column(Integer, primary_key=True)
    live_session_id = Column(
        Integer, ForeignKey("live_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    portfolio_strategy_id = Column(
        Integer, ForeignKey("portfolio_strategies.id"), nullable=False, index=True,
    )
    strategy_id = Column(
        Integer, ForeignKey("strategies.id"), nullable=False, index=True,
    )
    formula_id = Column(Integer, ForeignKey("formulas.id"), nullable=True)
    stock_code = Column(String(20), nullable=False)
    period = Column(String(5), nullable=False)
    bar_time = Column(DateTime, nullable=False)
    formula_name = Column(String(100), nullable=False)
    signal_name = Column(String(50), nullable=False)
    signal_type = Column(String(10), nullable=False)
    trigger_value = Column(Integer, nullable=False)
    price = Column(Numeric(10, 3), nullable=True)
    execution_mode = Column(String(10), nullable=False)
    status = Column(String(15), nullable=False, server_default="new", default="new")
    alert_status = Column(String(15), nullable=True)
    alert_error = Column(String(500), nullable=True)
    tdx_run_id = Column(String(30), nullable=True)
    alerted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "live_session_id", "portfolio_strategy_id", "strategy_id",
            "stock_code", "signal_name", "bar_time",
            name="uq_live_signal_dedup",
        ),
        Index("ix_live_signals_session_bar", "live_session_id", "bar_time"),
        Index("ix_live_signals_strategy_stock", "strategy_id", "stock_code"),
    )
```

Add `LiveSignal` to `main/core/models/__init__.py` import and `__all__` following the existing pattern. Remove the unused `Date` import if linter flags it.

- [ ] **Step 4: Create migration**

`main/alembic/versions/f0a1b2c3d4e5_add_live_signal_alert.py`:

```python
"""add live signal alert tables and columns

Revision ID: f0a1b2c3d4e5
Revises: a9b8c7d6e5f4
"""
from alembic import op
import sqlalchemy as sa

revision = "f0a1b2c3d4e5"
down_revision = "a9b8c7d6e5f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "live_sessions",
        sa.Column("execution_mode", sa.String(10), nullable=False,
                  server_default="auto"),
    )
    for name, typ in (
        ("signal_frequency", sa.String(10)),
        ("signal_at", sa.String(5)),
        ("signal_weekday", sa.Integer()),
        ("signal_data_end", sa.String(10)),
    ):
        op.add_column("strategies", sa.Column(name, typ, nullable=True))
    op.create_table(
        "live_signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("live_session_id", sa.Integer(),
                  sa.ForeignKey("live_sessions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("portfolio_strategy_id", sa.Integer(),
                  sa.ForeignKey("portfolio_strategies.id"), nullable=False),
        sa.Column("strategy_id", sa.Integer(),
                  sa.ForeignKey("strategies.id"), nullable=False),
        sa.Column("formula_id", sa.Integer(),
                  sa.ForeignKey("formulas.id"), nullable=True),
        sa.Column("stock_code", sa.String(20), nullable=False),
        sa.Column("period", sa.String(5), nullable=False),
        sa.Column("bar_time", sa.DateTime(), nullable=False),
        sa.Column("formula_name", sa.String(100), nullable=False),
        sa.Column("signal_name", sa.String(50), nullable=False),
        sa.Column("signal_type", sa.String(10), nullable=False),
        sa.Column("trigger_value", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(10, 3), nullable=True),
        sa.Column("execution_mode", sa.String(10), nullable=False),
        sa.Column("status", sa.String(15), nullable=False,
                  server_default="new"),
        sa.Column("alert_status", sa.String(15), nullable=True),
        sa.Column("alert_error", sa.String(500), nullable=True),
        sa.Column("tdx_run_id", sa.String(30), nullable=True),
        sa.Column("alerted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "live_session_id", "portfolio_strategy_id", "strategy_id",
            "stock_code", "signal_name", "bar_time",
            name="uq_live_signal_dedup",
        ),
    )
    op.create_index("ix_live_signals_live_session_id", "live_signals",
                    ["live_session_id"])
    op.create_index("ix_live_signals_portfolio_strategy_id", "live_signals",
                    ["portfolio_strategy_id"])
    op.create_index("ix_live_signals_strategy_id", "live_signals",
                    ["strategy_id"])
    op.create_index("ix_live_signals_session_bar", "live_signals",
                    ["live_session_id", "bar_time"])
    op.create_index("ix_live_signals_strategy_stock", "live_signals",
                    ["strategy_id", "stock_code"])


def downgrade() -> None:
    for ix in (
        "ix_live_signals_strategy_stock", "ix_live_signals_session_bar",
        "ix_live_signals_strategy_id", "ix_live_signals_portfolio_strategy_id",
        "ix_live_signals_live_session_id",
    ):
        op.drop_index(ix, table_name="live_signals")
    op.drop_table("live_signals")
    with op.batch_alter_table("strategies") as batch:
        batch.drop_column("signal_data_end")
        batch.drop_column("signal_weekday")
        batch.drop_column("signal_at")
        batch.drop_column("signal_frequency")
    with op.batch_alter_table("live_sessions") as batch:
        batch.drop_column("execution_mode")
```

- [ ] **Step 5: Run migration + tests**

Run:
```powershell
uv run --project main pytest main/core/tests/unit/test_live_signal_model.py -q
uv run --directory main alembic -c alembic.ini upgrade head
uv run --directory main alembic -c alembic.ini heads
```
Expected: tests PASS; `heads` prints `f0a1b2c3d4e5 (head)`.

- [ ] **Step 6: Commit**

```powershell
git add main/core/models/live_signal.py main/core/models/live_session.py main/core/models/strategy.py main/core/models/__init__.py main/alembic/versions/f0a1b2c3d4e5_add_live_signal_alert.py main/core/tests/unit/test_live_signal_model.py
git commit -m "feat: add live signal model and migration"
```

---

### Task 2: 信号频率配置规则

**Files:**
- Create: `main/core/services/signal_schedule.py`
- Test: `main/core/tests/unit/test_signal_schedule.py`

**Interfaces:**
- Produces:
  - `VALID_SIGNAL_FREQUENCIES: tuple[str, ...]`
  - `default_signal_at(frequency: str) -> str`
  - `default_signal_data_end(frequency: str) -> str`
  - `effective_signal_frequency(period: str, frequency: str | None) -> str`
  - `effective_signal_data_end(frequency: str, data_end: str | None) -> str`
  - `parse_signal_at(value: str | None, frequency: str) -> time`
  - `validate_signal_config(period, frequency, signal_at, signal_weekday, data_end) -> str | None`

- [ ] **Step 1: Write failing tests**

```python
# main/core/tests/unit/test_signal_schedule.py
from datetime import time

from core.services.signal_schedule import (
    effective_signal_data_end, effective_signal_frequency, parse_signal_at,
    validate_signal_config,
)


def test_effective_and_defaults():
    assert effective_signal_frequency("1d", None) == "1d"
    assert effective_signal_frequency("1d", "1w") == "1w"
    assert effective_signal_data_end("1w", None) == "completed"
    assert effective_signal_data_end("1d", None) == "current"
    assert parse_signal_at(None, "1w") == time(9, 0)
    assert parse_signal_at(None, "1d") == time(14, 30)


def test_frequency_cannot_be_finer_than_period():
    assert validate_signal_config("1d", "1m", None, None, None)
    assert validate_signal_config("1d", "1w", "09:00", 1, "completed") is None
    assert validate_signal_config("5m", "1m", None, None, None)


def test_weekly_and_monthly_require_completed():
    assert validate_signal_config("1d", "1w", "09:00", 1, "current")
    assert validate_signal_config("1d", "1mon", "09:00", None, None) is None
    assert validate_signal_config("1d", "1w", "9:00", 1, None)
    assert validate_signal_config("1d", "1w", "09:00", 6, None)
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --project main pytest main/core/tests/unit/test_signal_schedule.py -q`
Expected: FAIL `ModuleNotFoundError: core.services.signal_schedule`.

- [ ] **Step 3: Implement**

```python
# main/core/services/signal_schedule.py
"""实盘信号频率/时点规则（API 校验与 SignalScheduler 共用）。"""
from datetime import time
from typing import Optional

VALID_SIGNAL_FREQUENCIES = (
    "1m", "5m", "15m", "30m", "1h", "1d", "1w", "1mon",
)
_RANK = {
    "1m": 0, "5m": 1, "15m": 2, "30m": 3, "1h": 4,
    "1d": 5, "1w": 6, "1mon": 7,
}
_DEFAULT_AT = {"1d": "14:30", "1w": "09:00", "1mon": "09:00"}


def default_signal_at(frequency: str) -> str:
    return _DEFAULT_AT.get(frequency, "14:30")


def default_signal_data_end(frequency: str) -> str:
    return "completed" if frequency in ("1w", "1mon") else "current"


def effective_signal_frequency(period: str, frequency: Optional[str]) -> str:
    return frequency or period


def effective_signal_data_end(frequency: str, data_end: Optional[str]) -> str:
    return data_end or default_signal_data_end(frequency)


def parse_signal_at(value: Optional[str], frequency: str) -> time:
    raw = value or default_signal_at(frequency)
    try:
        hh, mm = raw.split(":")
        return time(int(hh), int(mm))
    except (TypeError, ValueError):
        return time(14, 30)


def validate_signal_config(
    period: str,
    frequency: Optional[str],
    signal_at: Optional[str],
    signal_weekday: Optional[int],
    data_end: Optional[str],
) -> Optional[str]:
    if period not in _RANK:
        return "period 非法：%s" % period
    freq = frequency or period
    if freq not in VALID_SIGNAL_FREQUENCIES:
        return "signal_frequency 必须为 %s，收到 %s" % (
            list(VALID_SIGNAL_FREQUENCIES), freq)
    if _RANK[freq] < _RANK[period]:
        return "signal_frequency 不能比 period 更细（period=%s, frequency=%s）" % (
            period, freq)
    at = signal_at or default_signal_at(freq)
    try:
        hh, mm = at.split(":")
        if len(hh) != 2 or len(mm) != 2:
            raise ValueError
        time(int(hh), int(mm))
    except (TypeError, ValueError):
        return "signal_at 必须为 HH:MM，收到 %s" % at
    if signal_weekday is not None and not (1 <= signal_weekday <= 5):
        return "signal_weekday 必须为 1-5，收到 %s" % signal_weekday
    if data_end is not None and data_end not in ("completed", "current"):
        return "signal_data_end 必须为 completed/current，收到 %s" % data_end
    if freq in ("1w", "1mon") and data_end == "current":
        return "%s 只允许 signal_data_end=completed，禁止未完成周/月 bar" % freq
    return None
```

- [ ] **Step 4: Run tests**

Run: `uv run --project main pytest main/core/tests/unit/test_signal_schedule.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add main/core/services/signal_schedule.py main/core/tests/unit/test_signal_schedule.py
git commit -m "feat: add signal schedule validation rules"
```

---

### Task 3: 策略字段接入 Context / API / Service

**Files:**
- Modify: `main/core/engine/strategy_context.py`, `main/core/engine/portfolio_builder.py`, `main/core/services/strategy_service.py`, `main/core/api/strategies.py`
- Test: `main/core/tests/unit/test_strategy_signal_fields.py`

**Interfaces:**
- Consumes: `effective_signal_frequency`, `validate_signal_config` from Task 2.
- Produces: `StrategyContext.signal_frequency/signal_at/signal_weekday/signal_data_end`; `StrategyCreate`/`StrategyItem` accept the four fields; `serialize_strategy` returns them.

- [ ] **Step 1: Write failing test**

```python
# main/core/tests/unit/test_strategy_signal_fields.py
from decimal import Decimal

from core.api.strategies import StrategyCreate, _validate_strategy
from core.engine.strategy_context import StrategyContext
from core.models import Base, Formula, PortfolioStrategy, Strategy, StockPool
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_context_carries_signal_schedule_fields():
    ctx = StrategyContext(
        strategy_id=1, period="1d", capital_ratio=Decimal("0.5"),
        max_positions=3, signal_frequency="1w", signal_at="09:00",
        signal_weekday=1, signal_data_end="completed",
    )
    assert ctx.signal_frequency == "1w"
    assert ctx.signal_data_end == "completed"


def test_api_rejects_finer_frequency_and_current_week():
    db = _db()
    pool = StockPool(code="P", name="p")
    db.add(pool)
    db.flush()
    port = PortfolioStrategy(name="p", stock_pool_id=pool.id,
                             initial_capital=Decimal("100000"))
    f = Formula(name="f", content="C")
    db.add_all([port, f])
    db.commit()
    base = dict(name="s", formula_id=f.id, role="master",
                capital_ratio=0.5, max_positions=3)
    bad = StrategyCreate(period="1d", signal_frequency="1m", **base)
    assert "不能比 period 更细" in _validate_strategy(bad, db, port.id)
    bad2 = StrategyCreate(period="1d", signal_frequency="1w",
                          signal_at="09:00", signal_weekday=1,
                          signal_data_end="current", **base)
    assert "completed" in _validate_strategy(bad2, db, port.id)
    ok = StrategyCreate(period="1d", signal_frequency="1w",
                        signal_at="09:00", signal_weekday=1,
                        signal_data_end="completed", **base)
    assert _validate_strategy(ok, db, port.id) is None
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --project main pytest main/core/tests/unit/test_strategy_signal_fields.py -q`
Expected: FAIL `TypeError: __init__() got an unexpected keyword argument 'signal_frequency'`.

- [ ] **Step 3: Add context fields**

`main/core/engine/strategy_context.py` signature add after `exposure_cap_enabled`:

```python
        signal_frequency: Optional[str] = None,
        signal_at: Optional[str] = None,
        signal_weekday: Optional[int] = None,
        signal_data_end: Optional[str] = None,
```

Assign near `self.period = period`:

```python
        self.signal_frequency = signal_frequency
        self.signal_at = signal_at
        self.signal_weekday = signal_weekday
        self.signal_data_end = signal_data_end
```

`main/core/engine/portfolio_builder.py` inside `StrategyContext(...)` add:

```python
            signal_frequency=strat.signal_frequency,
            signal_at=strat.signal_at,
            signal_weekday=strat.signal_weekday,
            signal_data_end=strat.signal_data_end,
```

- [ ] **Step 4: Expose in API/service and validate**

`main/core/api/strategies.py` import:

```python
from core.services.signal_schedule import validate_signal_config
```

Add to both `StrategyItem` and `StrategyCreate`:

```python
    signal_frequency: str | None = None
    signal_at: str | None = None
    signal_weekday: int | None = None
    signal_data_end: str | None = None
```

In `_validate_strategy` after period validation add:

```python
    sig_msg = validate_signal_config(
        req.period, req.signal_frequency, req.signal_at,
        req.signal_weekday, req.signal_data_end,
    )
    if sig_msg:
        return sig_msg
```

`main/core/services/strategy_service.py` `serialize_strategy` add:

```python
        "signal_frequency": s.signal_frequency,
        "signal_at": s.signal_at,
        "signal_weekday": s.signal_weekday,
        "signal_data_end": s.signal_data_end,
```

`_apply_strategy_fields` add:

```python
    s.signal_frequency = getattr(req, "signal_frequency", None)
    s.signal_at = getattr(req, "signal_at", None)
    s.signal_weekday = getattr(req, "signal_weekday", None)
    s.signal_data_end = getattr(req, "signal_data_end", None)
```

`_create_strategies_two_step` add the same four fields to the `Strategy(...)` construction:

```python
            signal_frequency=getattr(s, "signal_frequency", None),
            signal_at=getattr(s, "signal_at", None),
            signal_weekday=getattr(s, "signal_weekday", None),
            signal_data_end=getattr(s, "signal_data_end", None),
```

- [ ] **Step 5: Run tests + regression**

Run:
```powershell
uv run --project main pytest main/core/tests/unit/test_strategy_signal_fields.py main/core/tests/unit/test_signal_schedule.py -q
uv run --project main pytest main/core/tests/unit/test_strategy_context.py main/core/tests/unit/test_portfolio.py -q
```
Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add main/core/engine/strategy_context.py main/core/engine/portfolio_builder.py main/core/services/strategy_service.py main/core/api/strategies.py main/core/tests/unit/test_strategy_signal_fields.py
git commit -m "feat: expose strategy signal schedule fields"
```

---

### Task 4: TQAlert（send_warn / send_message / preflight）

**Files:**
- Create: `main/core/tq/alert.py`
- Modify: `main/core/tq/__init__.py`
- Test: `main/core/tests/unit/test_tq_alert.py`

**Interfaces:**
- Produces: `WarnItem`, `TQAlertError`, `TQAlert.send_warn(items) -> dict`, `TQAlert.send_message(msg) -> dict`, `TQAlert.preflight() -> str | None`.
- `send_warn` return shape: `{"ok": bool, "ErrorId": str, "Error": str, "run_id": str, "batches": int}`.

- [ ] **Step 1: Write failing tests**

```python
# main/core/tests/unit/test_tq_alert.py
from contextlib import nullcontext

from core.tq.alert import TQAlert, WarnItem


class FakeTQ:
    def __init__(self, error_id="0", has_warn=True):
        self.calls = []
        self.error_id = error_id
        if has_warn:
            self.send_warn = self._send_warn

    def _send_warn(self, **kwargs):
        self.calls.append(kwargs)
        return {"ErrorId": self.error_id, "Error": "", "run_id": str(len(self.calls))}

    def send_message(self, msg):
        self.calls.append({"message": msg})
        return {"ErrorId": "0"}


def _patch(monkeypatch, fake):
    monkeypatch.setattr("core.tq.alert.get_tq", lambda: fake)
    monkeypatch.setattr("core.tq.alert.get_tdx_lock", lambda: nullcontext())


def test_send_warn_chunks_50_and_truncates_reason(monkeypatch):
    fake = FakeTQ()
    _patch(monkeypatch, fake)
    items = [
        WarnItem(stock_code="000001.SZ", time="20261023090000",
                 price="12.34", close="12.34", bs_flag="0",
                 reason="很长的公式名-很长的信号名-超过二十五个汉字需要截断")
        for _ in range(51)
    ]
    result = TQAlert().send_warn(items)
    assert result["ok"] is True
    assert result["batches"] == 2
    assert fake.calls[0]["count"] == 50
    assert fake.calls[1]["count"] == 1
    assert len(fake.calls[0]["reason_list"][0]) == 25
    assert fake.calls[0]["warn_type_list"][0] == "0"


def test_send_warn_returns_failure_on_errorid(monkeypatch):
    fake = FakeTQ(error_id="7")
    _patch(monkeypatch, fake)
    result = TQAlert().send_warn([
        WarnItem(stock_code="000001.SZ", time="20261023090000",
                 price="1", close="1", bs_flag="1", reason="x"),
    ])
    assert result["ok"] is False
    assert result["ErrorId"] == "7"


def test_preflight_reports_missing_send_warn(monkeypatch):
    fake = FakeTQ(has_warn=False)
    _patch(monkeypatch, fake)
    assert "send_warn" in TQAlert().preflight()


def test_empty_send_is_skipped(monkeypatch):
    fake = FakeTQ()
    _patch(monkeypatch, fake)
    assert TQAlert().send_warn([])["skipped"] is True
    assert fake.calls == []
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --project main pytest main/core/tests/unit/test_tq_alert.py -q`
Expected: FAIL `ModuleNotFoundError: core.tq.alert`.

- [ ] **Step 3: Implement**

```python
# main/core/tq/alert.py
"""通达信 TQ send_warn / send_message 薄封装。"""
from dataclasses import dataclass
from typing import List, Optional

from .utils import get_tdx_lock, get_tq

_BATCH = 50


class TQAlertError(RuntimeError):
    pass


@dataclass
class WarnItem:
    stock_code: str
    time: str
    price: str = "0"
    close: str = "0"
    volume: str = "0"
    bs_flag: str = "2"
    warn_type: str = "0"
    reason: str = ""


def _chunks(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def _num(value, default="0") -> str:
    try:
        float(str(value))
        return str(value)
    except (TypeError, ValueError):
        return default


class TQAlert:
    def preflight(self) -> Optional[str]:
        try:
            tq = get_tq()
        except Exception as exc:  # noqa: BLE001
            return "通达信 TQ 未初始化：%s" % exc
        if not hasattr(tq, "send_warn"):
            return "当前通达信 TQ 不支持 send_warn，请升级客户端/插件"
        try:
            from core.tq.data import TQData
            raw = TQData().get_history_raw(["000001.SH"], ["1d"], count=2)
            close = (raw.get("1d") or {}).get("Close")
            if close is None or getattr(close, "empty", True):
                return "通达信行情不可用：未取到 000001.SH 日线"
        except Exception as exc:  # noqa: BLE001
            return "通达信行情不可用：%s" % exc
        return None

    def send_warn(self, items: List[WarnItem]) -> dict:
        if not items:
            return {"ok": True, "ErrorId": "0", "skipped": True, "batches": 0}
        tq = get_tq()
        if not hasattr(tq, "send_warn"):
            raise TQAlertError("当前通达信 TQ 不支持 send_warn")
        run_id = ""
        for chunk in _chunks(items, _BATCH):
            payload = {
                "stock_list": [i.stock_code for i in chunk],
                "time_list": ["".join(c for c in i.time if c.isdigit())[:14]
                              for i in chunk],
                "price_list": [_num(i.price) for i in chunk],
                "close_list": [_num(i.close) for i in chunk],
                "volum_list": [_num(i.volume) for i in chunk],
                "bs_flag_list": [i.bs_flag if i.bs_flag in ("0", "1", "2")
                                 else "2" for i in chunk],
                "warn_type_list": ["0" for _ in chunk],
                "reason_list": [(i.reason or "")[:25] for i in chunk],
                "count": len(chunk),
            }
            with get_tdx_lock():
                raw = tq.send_warn(**payload)
            raw = raw or {}
            err_id = str(raw.get("ErrorId", "0"))
            if err_id not in ("0", ""):
                return {
                    "ok": False, "ErrorId": err_id,
                    "Error": str(raw.get("Error") or ""),
                    "run_id": str(raw.get("run_id") or ""),
                    "batches": len(_chunks(items, _BATCH)),
                }
            run_id = str(raw.get("run_id") or run_id)
        return {"ok": True, "ErrorId": "0", "Error": "",
                "run_id": run_id, "batches": len(_chunks(items, _BATCH))}

    def send_message(self, msg: str) -> dict:
        try:
            tq = get_tq()
            if not hasattr(tq, "send_message"):
                return {"ok": False, "Error": "send_message unavailable"}
            with get_tdx_lock():
                raw = tq.send_message(msg)
            raw = raw or {}
            return {"ok": str(raw.get("ErrorId", "0")) in ("0", ""),
                    "Error": str(raw.get("Error") or ""), "raw": raw}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "Error": str(exc)}
```

`main/core/tq/__init__.py` add `from .alert import TQAlert, TQAlertError, WarnItem` and `__all__` entries.

- [ ] **Step 4: Run tests**

Run: `uv run --project main pytest main/core/tests/unit/test_tq_alert.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add main/core/tq/alert.py main/core/tq/__init__.py main/core/tests/unit/test_tq_alert.py
git commit -m "feat: add TQ send_warn wrapper"
```

---

### Task 5: AlertDataDispatcher

**Files:**
- Create: `main/core/engine/live/alert_dispatcher.py`
- Test: `main/core/tests/unit/test_alert_dispatcher.py`

**Interfaces:**
- Produces: `AlertDataDispatcher` with `channel="alert"`, `supports_remark=False`, `query_quote`, `query_calendar`, `heartbeat`, `place_order`, `get_positions`, `get_asset`, `query_orders`, `query_deals`, `close`.

- [ ] **Step 1: Write failing test**

```python
# main/core/tests/unit/test_alert_dispatcher.py
import pytest

from core.engine.live.alert_dispatcher import AlertDataDispatcher


class FakeQuote:
    def query_quote(self, code, period="1m", count=10):
        return [{"stime": "20261023150000", "close": 12.3}]

    def query_calendar(self):
        return ["20261023"]


def test_alert_dispatcher_delegates_quote_and_calendar(monkeypatch):
    monkeypatch.setattr(
        "core.engine.live.alert_dispatcher.TdxTqDispatcher",
        lambda **kw: FakeQuote(),
    )
    d = AlertDataDispatcher()
    assert d.channel == "alert"
    assert d.query_quote("000001.SZ", "1d", 5)[0]["close"] == 12.3
    assert d.query_calendar() == ["20261023"]


def test_alert_dispatcher_never_places_order(monkeypatch):
    monkeypatch.setattr(
        "core.engine.live.alert_dispatcher.TdxTqDispatcher",
        lambda **kw: FakeQuote(),
    )
    d = AlertDataDispatcher()
    with pytest.raises(RuntimeError):
        d.place_order(None)
    assert d.get_positions() == []
    assert d.get_asset() == {}
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --project main pytest main/core/tests/unit/test_alert_dispatcher.py -q`
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# main/core/engine/live/alert_dispatcher.py
"""alert 模式专用行情 dispatcher：TQ 行情 + 禁止下单。"""
import logging

from ..tdx_tq_dispatcher import TdxTqDispatcher

logger = logging.getLogger(__name__)


class AlertDataDispatcher:
    channel = "alert"
    supports_remark = False

    def __init__(self, **kwargs):
        self.last_error = ""
        self._quote = TdxTqDispatcher(account="", **kwargs)

    def query_quote(self, code, period="1m", count=10):
        return self._quote.query_quote(code, period=period, count=count)

    def query_calendar(self):
        return self._quote.query_calendar()

    def heartbeat(self) -> bool:
        try:
            from core.tq.data import TQData
            raw = TQData().get_history_raw(["000001.SH"], ["1d"], count=2)
            close = (raw.get("1d") or {}).get("Close")
            if close is None or getattr(close, "empty", True):
                self.last_error = "TQ 行情不可用：未取到 000001.SH 日线"
                return False
            self.last_error = ""
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = "TQ 行情不可用：%s" % exc
            return False

    def place_order(self, *args, **kwargs):
        raise RuntimeError("alert 模式禁止下单")

    def get_positions(self):
        return []

    def get_asset(self):
        return {}

    def query_orders(self):
        return []

    def query_deals(self):
        return []

    def close(self):
        self._quote.close()
```

- [ ] **Step 4: Run tests**

Run: `uv run --project main pytest main/core/tests/unit/test_alert_dispatcher.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add main/core/engine/live/alert_dispatcher.py main/core/tests/unit/test_alert_dispatcher.py
git commit -m "feat: add TQ-only alert data dispatcher"
```

---

### Task 6: SignalScheduler

**Files:**
- Create: `main/core/engine/live/signal_scheduler.py`
- Test: `main/core/tests/unit/test_signal_scheduler.py`

**Interfaces:**
- Produces:
  - `DueBatch(portfolio, strategies, frequency, period, mode, bar_time, data_end_date)`
  - `SignalScheduler(ctx, market_data, calendar_provider, formula_by_strategy, formula_count_by_name, formula_id_by_strategy, monitor, alert, catch_up_minutes=30, clock=now_shanghai)`
  - `SignalScheduler.due(now: datetime) -> list[DueBatch]`
  - `SignalScheduler.execute(batch: DueBatch) -> None`

- [ ] **Step 1: Write failing scheduler tests**

```python
# main/core/tests/unit/test_signal_scheduler.py
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

from core.engine.live.signal_scheduler import DueBatch, SignalScheduler
from core.engine.live.calendar import TradingCalendar


class FakeCalendar:
    def __init__(self, days):
        self.days = set(days)

    def is_trading_day(self, d):
        return d in self.days


class FakeAlert:
    def __init__(self):
        self.messages = []

    def send_message(self, msg):
        self.messages.append(msg)
        return {"ok": True}


class FakeMonitor:
    def __init__(self):
        self.calls = []

    def evaluate(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class FakeMarketData:
    pass


def _ctx(strategy):
    port = SimpleNamespace(portfolio_id=1, strategies=[strategy])
    return SimpleNamespace(portfolios=[port], code_period_count={})


def _strategy(**kw):
    base = dict(strategy_id=1, period="1d", signal_frequency="1w",
                signal_at="09:00", signal_weekday=1,
                signal_data_end="completed")
    base.update(kw)
    return SimpleNamespace(**base)


def _scheduler(now_days, strategy, alert=None, clock=None, catch_up=30):
    cal = FakeCalendar(now_days)
    return SignalScheduler(
        _ctx(strategy), FakeMarketData(), lambda: cal,
        {1: "momentum"}, {"momentum": 200}, {1: 10},
        FakeMonitor(), alert or FakeAlert(), catch_up_minutes=catch_up,
        clock=clock or (lambda: datetime(2026, 10, 26, 9, 0)),
    )


def test_weekly_due_at_monday_0900_uses_last_friday():
    days = [date(2026, 10, 23), date(2026, 10, 26)]
    s = _scheduler(days, _strategy())
    assert s.due(datetime(2026, 10, 26, 8, 59)) == []
    batches = s.due(datetime(2026, 10, 26, 9, 0))
    assert len(batches) == 1
    b = batches[0]
    assert b.mode == "compute"
    assert b.frequency == "1w"
    assert b.data_end_date == date(2026, 10, 23)
    assert b.bar_time == datetime(2026, 10, 23, 15, 0)
    assert s.due(datetime(2026, 10, 26, 9, 1)) == []


def test_weekly_due_once_per_portfolio():
    days = [date(2026, 10, 23), date(2026, 10, 26)]
    st = _strategy()
    ctx = SimpleNamespace(portfolios=[
        SimpleNamespace(portfolio_id=1, strategies=[st]),
        SimpleNamespace(portfolio_id=2, strategies=[st]),
    ], code_period_count={})
    s = SignalScheduler(
        ctx, FakeMarketData(), lambda: FakeCalendar(days),
        {1: "momentum"}, {"momentum": 200}, {1: 10},
        FakeMonitor(), FakeAlert(),
    )
    assert len(s.due(datetime(2026, 10, 26, 9, 0))) == 2


def test_weekly_holiday_shifts_to_next_trading_day():
    days = [date(2026, 10, 23), date(2026, 10, 27)]
    s = _scheduler(days, _strategy())
    assert s.due(datetime(2026, 10, 26, 9, 0)) == []
    batches = s.due(datetime(2026, 10, 27, 9, 0))
    assert batches[0].data_end_date == date(2026, 10, 23)


def test_daily_1430_is_inject():
    days = [date(2026, 10, 26)]
    s = _scheduler(days, _strategy(signal_frequency="1d", signal_at="14:30",
                                  signal_data_end="current"))
    batches = s.due(datetime(2026, 10, 26, 14, 30))
    assert len(batches) == 1
    assert batches[0].mode == "inject"
    assert batches[0].bar_time == datetime(2026, 10, 26, 14, 30)
    assert batches[0].data_end_date == date(2026, 10, 26)


def test_catch_up_window_and_skip_message():
    days = [date(2026, 10, 23), date(2026, 10, 26)]
    alert = FakeAlert()
    s = _scheduler(days, _strategy(), alert=alert)
    assert len(s.due(datetime(2026, 10, 26, 9, 20))) == 1
    alert2 = FakeAlert()
    s2 = _scheduler(days, _strategy(), alert=alert2)
    assert s2.due(datetime(2026, 10, 26, 9, 31)) == []
    assert alert2.messages and "跳过" in alert2.messages[0]


class FakeFormula:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def compute(self, *a, **kw):
        self.calls.append((a, kw))
        return self.raw


class ExecuteMarketData:
    def __init__(self):
        self.signal_cache = {}
        self._period_count = {"1d": 200}
        self._formula_count = 200
        self._formula_count_by_name = {"momentum": 200}
        self._bar_poller = SimpleNamespace(stock_codes=["000001.SZ"])
        self._tq_formula = FakeFormula({
            "ErrorId": "0",
            "000001.SZ": {"buy": [{"Date": "20261023", "Value": 1}]},
        })
        self.fills = []

    def _sort_and_cap(self, bars, count):
        return bars

    def fill_signal_cache(self, portfolio, bar, bars_by_code=None, **kw):
        self.fills.append((bar, bars_by_code))
        self.signal_cache[(1, "000001.SZ", bar.bar_time)] = [
            {"name": "buy", "value": 1},
        ]

    @staticmethod
    def _extract_latest_signal(raw, code):
        return [{"name": "buy", "value": 1}]


def _exec_ctx(strategy, bars):
    disp = SimpleNamespace(
        query_quote=lambda code, period="1m", count=10: list(bars)
    )
    port = SimpleNamespace(portfolio_id=1, strategies=[strategy])
    return SimpleNamespace(portfolios=[port], code_period_count={}, dispatcher=disp)


_BARS = [
    {"stime": "20261023150000", "open": 12, "high": 12.5, "low": 11.9,
     "close": 12.34, "volume": 100},
    {"stime": "20261026143000", "open": 12.5, "high": 13, "low": 12.4,
     "close": 12.9, "volume": 50},
]


def test_execute_inject_completed_caps_bars_and_price():
    strategy = _strategy(signal_frequency="1d", signal_at="14:30",
                         signal_data_end="completed")
    md = ExecuteMarketData()
    mon = FakeMonitor()
    sched = SignalScheduler(
        _exec_ctx(strategy, _BARS), md,
        lambda: FakeCalendar([date(2026, 10, 23), date(2026, 10, 26)]),
        {1: "momentum"}, {"momentum": 200}, {1: 10}, mon, FakeAlert(),
    )
    batch = DueBatch(
        portfolio=_exec_ctx(strategy, _BARS).portfolios[0],
        strategies=[strategy], frequency="1d", period="1d", mode="inject",
        bar_time=datetime(2026, 10, 26, 14, 30),
        data_end_date=date(2026, 10, 23),
    )
    sched.execute(batch)
    bar, bars_by_code = md.fills[0]
    assert len(bars_by_code["000001.SZ"]) == 1
    assert bars_by_code["000001.SZ"][0]["stime"].startswith("20261023")
    assert bar.stocks["000001.SZ"]["close"] == Decimal("12.34")
    assert mon.calls


def test_execute_compute_uses_window_end_price_not_forming_bar():
    strategy = _strategy()
    md = ExecuteMarketData()
    mon = FakeMonitor()
    sched = SignalScheduler(
        _exec_ctx(strategy, _BARS), md,
        lambda: FakeCalendar([date(2026, 10, 23), date(2026, 10, 26)]),
        {1: "momentum"}, {"momentum": 200}, {1: 10}, mon, FakeAlert(),
    )
    batch = DueBatch(
        portfolio=_exec_ctx(strategy, _BARS).portfolios[0],
        strategies=[strategy], frequency="1w", period="1d", mode="compute",
        bar_time=datetime(2026, 10, 23, 15, 0),
        data_end_date=date(2026, 10, 23),
    )
    sched.execute(batch)
    portfolio, bar, _cache = mon.calls[0][0][:3]
    assert bar.stocks["000001.SZ"]["close"] == Decimal("12.34")
    assert md._tq_formula.calls
    assert md._tq_formula.calls[0][1]["end_time"] == "20261023"
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --project main pytest main/core/tests/unit/test_signal_scheduler.py -q`
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Implement scheduler**

```python
# main/core/engine/live/signal_scheduler.py
"""实盘信号频率调度：何时算、数据截至哪天。"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable, Dict, List, Optional

from ..bar_poller import latest_completed_bar, parse_bar_time, to_ohlcv
from ..event import BarEvent
from ..live.timing import now_shanghai
from ...services.signal_schedule import (
    effective_signal_data_end, effective_signal_frequency, parse_signal_at,
)


@dataclass
class DueBatch:
    portfolio: object
    strategies: list
    frequency: str
    period: str
    mode: str
    bar_time: datetime
    data_end_date: date


class SignalScheduler:
    def __init__(
        self, ctx, market_data, calendar_provider: Callable,
        formula_by_strategy: Dict[int, str],
        formula_count_by_name: Dict[str, int],
        formula_id_by_strategy: Dict[int, int],
        monitor, alert, catch_up_minutes: int = 30,
        clock: Callable[[], datetime] = now_shanghai,
    ):
        self.ctx = ctx
        self.market_data = market_data
        self._calendar = calendar_provider
        self._formula_by_strategy = formula_by_strategy
        self._formula_count_by_name = formula_count_by_name
        self._formula_id_by_strategy = formula_id_by_strategy
        self.monitor = monitor
        self.alert = alert
        self.catch_up_minutes = catch_up_minutes
        self.clock = clock
        self._fired = set()

    def due(self, now: datetime) -> List[DueBatch]:
        batches: List[DueBatch] = []
        skipped = 0
        for portfolio in self.ctx.portfolios:
            groups = {}
            for st in portfolio.strategies:
                freq = effective_signal_frequency(st.period, st.signal_frequency)
                if freq not in ("1d", "1w", "1mon"):
                    continue
                # 归一化默认值：NULL 与显式默认值必须归入同一批，避免同窗口双发。
                at = parse_signal_at(st.signal_at, freq).strftime("%H:%M")
                weekday = st.signal_weekday or 1
                data_end = effective_signal_data_end(freq, st.signal_data_end)
                key = (freq, st.period, at, weekday, data_end)
                groups.setdefault(key, []).append(st)
            for (freq, period, at, weekday, data_end), strategies in groups.items():
                due_dt, window_end, bar_time = self._schedule(
                    now, freq, at, weekday, data_end
                )
                if due_dt is None or now < due_dt:
                    continue
                fired_key = (
                    portfolio.portfolio_id, freq, period, window_end,
                    at, weekday, data_end,
                )
                if fired_key in self._fired:
                    continue
                if now > due_dt + timedelta(minutes=self.catch_up_minutes):
                    self._fired.add(fired_key)
                    skipped += len(strategies)
                    continue
                self._fired.add(fired_key)
                batches.append(DueBatch(
                    portfolio=portfolio, strategies=strategies, frequency=freq,
                    period=period,
                    mode="inject" if freq == "1d" else "compute",
                    bar_time=bar_time, data_end_date=window_end,
                ))
        if skipped:
            try:
                self.alert.send_message(
                    "已跳过 %d 个过期信号（超过 %d 分钟补算窗口）"
                    % (skipped, self.catch_up_minutes)
                )
            except Exception:  # noqa: BLE001
                pass
        return batches

    def _schedule(self, now, freq, at, weekday, data_end):
        cal = self._calendar()
        at_time = parse_signal_at(at, freq)
        today = now.date()
        if freq == "1d":
            if not cal.is_trading_day(today):
                return None, None, None
            due_dt = datetime.combine(today, at_time)
            end = today
            if effective_signal_data_end(freq, data_end) == "completed":
                end = self._last_trading_before(today)
            return due_dt, end, due_dt
        if freq == "1w":
            monday = today - timedelta(days=today.weekday())
            due_date = monday + timedelta(days=(weekday or 1) - 1)
            for _ in range(7):
                if cal.is_trading_day(due_date):
                    break
                due_date += timedelta(days=1)
            else:
                return None, None, None
            end = self._last_trading_before(due_date)
            return (
                datetime.combine(due_date, at_time), end,
                datetime.combine(end, time(15, 0)),
            )
        if freq == "1mon":
            first = date(today.year, today.month, 1)
            due_date = first
            for _ in range(31):
                if cal.is_trading_day(due_date):
                    break
                due_date += timedelta(days=1)
            else:
                return None, None, None
            end = self._last_trading_before(due_date)
            return (
                datetime.combine(due_date, at_time), end,
                datetime.combine(end, time(15, 0)),
            )
        return None, None, None

    def _last_trading_before(self, d: date) -> date:
        cal = self._calendar()
        cur = d - timedelta(days=1)
        for _ in range(40):
            if cal.is_trading_day(cur):
                return cur
            cur -= timedelta(days=1)
        return d - timedelta(days=1)

    def execute(self, batch: DueBatch) -> None:
        if batch.mode == "inject":
            self._execute_inject(batch)
        else:
            self._execute_compute(batch)

    def _codes(self):
        return list(self.market_data._bar_poller.stock_codes)

    def _quote(self, code, period, count):
        bars = self.ctx.dispatcher.query_quote(code, period=period, count=count)
        return self.market_data._sort_and_cap(bars or [], count)

    def _execute_inject(self, batch: DueBatch) -> None:
        bars_by_code = {}
        stocks = {}
        current = effective_signal_data_end(
            batch.frequency, batch.strategies[0].signal_data_end
        ) == "current"
        target = datetime.combine(batch.data_end_date, time(15, 0))
        for code in self._codes():
            count = self.ctx.code_period_count.get(
                (code, batch.period),
                self.market_data._period_count.get(
                    batch.period, self.market_data._formula_count),
            )
            bars = self._quote(code, batch.period, count)
            if not bars:
                continue
            if not current:
                capped = []
                for b in bars:
                    bt = parse_bar_time(b)
                    if bt is not None and bt <= target:
                        capped.append(b)
                if capped:
                    bars = capped
                else:
                    continue
            bars_by_code[code] = bars
            # 已按 data_end_date 截断（completed）或保留 forming（current），
            # 排序由 _quote→_sort_and_cap 保证，末根即目标 bar。
            stocks[code] = to_ohlcv(bars[-1])
        if not stocks:
            return
        bar = BarEvent(stocks=stocks, bar_time=batch.bar_time,
                       period=batch.period, bars_by_code=bars_by_code)
        self.market_data.fill_signal_cache(
            batch.portfolio, bar, bars_by_code=bars_by_code
        )
        self.monitor.evaluate(
            batch.portfolio, bar, self.market_data.signal_cache,
            strategies=batch.strategies, frequency=batch.frequency,
        )

    def _execute_compute(self, batch: DueBatch) -> None:
        end_time = batch.data_end_date.strftime("%Y%m%d")
        for st in batch.strategies:
            formula_name = self._formula_by_strategy.get(st.strategy_id)
            if not formula_name:
                continue
            count = self._formula_count_by_name.get(formula_name, 200)
            codes = self._codes()
            try:
                raw = self.market_data._tq_formula.compute(
                    formula_name, "", codes, period=batch.period,
                    count=count, end_time=end_time,
                )
            except Exception as exc:  # noqa: BLE001
                self._report_error(batch, st, formula_name, str(exc))
                continue
            if not isinstance(raw, dict) or str(raw.get("ErrorId", "0")) not in ("0", "19"):
                self._report_error(
                    batch, st, formula_name,
                    str((raw or {}).get("Error") or "ErrorId != 0"),
                )
                continue
            stocks = {}
            target = datetime.combine(batch.data_end_date, time(15, 0))
            for code in codes:
                bars = self._quote(code, batch.period, count)
                chosen = None
                for b in bars:
                    bt = parse_bar_time(b)
                    if bt is not None and bt <= target:
                        chosen = b
                if chosen is None:
                    fallback = latest_completed_bar(bars)
                    fb_time = parse_bar_time(fallback) if fallback else None
                    if fb_time is not None and fb_time <= target:
                        chosen = fallback
                if chosen is not None:
                    stocks[code] = to_ohlcv(chosen)
                outputs = self.market_data._extract_latest_signal(raw, code)
                if outputs:
                    self.market_data.signal_cache[
                        (st.strategy_id, code, batch.bar_time)
                    ] = outputs
            if not stocks:
                continue
            bar = BarEvent(stocks=stocks, bar_time=batch.bar_time,
                           period=batch.period)
            self.monitor.evaluate(
                batch.portfolio, bar, self.market_data.signal_cache,
                strategies=[st], frequency=batch.frequency,
            )

    def _report_error(self, batch, st, formula_name, message):
        text = "公式失败 %s/%s: %s" % (formula_name, st.strategy_id, message)
        try:
            self.alert.send_message(text)
        except Exception:  # noqa: BLE001
            pass
        self.monitor.report_runtime_error(
            batch.portfolio, st, formula_name, message
        )
```

- [ ] **Step 4: Run tests**

Run: `uv run --project main pytest main/core/tests/unit/test_signal_scheduler.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add main/core/engine/live/signal_scheduler.py main/core/tests/unit/test_signal_scheduler.py
git commit -m "feat: add live signal scheduler"
```

---

### Task 7: SignalMonitor

**Files:**
- Create: `main/core/engine/live/signal_monitor.py`
- Test: `main/core/tests/unit/test_signal_monitor.py`

**Interfaces:**
- Consumes: `LiveSignal`, `TQAlert`, `WarnItem`, `SignalType`.
- Produces: `SignalMonitor(session_id, db_session_factory, alert, emit, execution_mode, clock=now_shanghai)`; `evaluate(portfolio, bar, signal_cache, strategies, frequency=None)`; `report_runtime_error(portfolio, strategy, formula_name, message)`.

- [ ] **Step 1: Write failing tests**

```python
# main/core/tests/unit/test_signal_monitor.py
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.engine.event import BarEvent
from core.engine.live.signal_monitor import SignalMonitor
from core.models import (
    Base, Formula, FormulaSignal, LiveSession, LiveSignal, PortfolioStrategy,
    StockPool, StockPoolStock, Strategy,
)
from tq_iquant_shared.constants import SignalType


class FakeAlert:
    def __init__(self, ok=True):
        self.ok = ok
        self.items = []

    def send_warn(self, items):
        self.items.extend(items)
        if self.ok:
            return {"ok": True, "ErrorId": "0", "run_id": "7"}
        return {"ok": False, "ErrorId": "9", "Error": "boom", "run_id": ""}

    def send_message(self, msg):
        return {"ok": True}


def _factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


def _seed(Session):
    db = Session()
    db.add(LiveSession(id=1, name="s", mode="simulation", status="running",
                       engine="tdx", execution_mode="alert"))
    pool = StockPool(code="P", name="p")
    db.add(pool)
    db.flush()
    db.add(StockPoolStock(pool_id=pool.id, stock_code="000001.SZ"))
    formula = Formula(id=10, name="momentum", content="C")
    db.add(formula)
    db.flush()
    db.add(FormulaSignal(formula_id=10, signal_name="buy",
                         signal_type="OPEN", trigger_value=1))
    port = PortfolioStrategy(id=1, name="p", stock_pool_id=pool.id,
                             initial_capital=Decimal("100000"))
    db.add(port)
    db.flush()
    st = Strategy(id=1, portfolio_id=1, name="s1", formula_id=10,
                  period="1d", role="master", signal_frequency="1d")
    db.add(st)
    db.commit()
    db.close()


def _ctx():
    st = SimpleNamespace(
        strategy_id=1, period="1d", signal_frequency="1d",
        formula_name="momentum", formula_id=10,
        formula_signals=[{"signal_name": "buy", "signal_type": SignalType.OPEN,
                          "trigger_value": 1}],
        stock_pool={"000001.SZ"},
    )
    return SimpleNamespace(portfolio_id=1, strategies=[st])


def _bar():
    return BarEvent(
        stocks={"000001.SZ": {"close": Decimal("12.34")}},
        bar_time=datetime(2026, 10, 26, 14, 30), period="1d",
    )


def test_monitor_persists_and_alerts(monkeypatch):
    Session, _ = _factory()
    _seed(Session)
    alert = FakeAlert()
    events = []
    mon = SignalMonitor(
        1, Session, alert, lambda t, p: events.append((t, p)),
        "alert", clock=lambda: datetime(2026, 10, 26, 14, 30),
    )
    cache = {(1, "000001.SZ", datetime(2026, 10, 26, 14, 30)): [
        {"name": "buy", "value": 1},
    ]}
    mon.evaluate(_ctx(), _bar(), cache,
                 strategies=_ctx().strategies, frequency="1d")
    db = Session()
    row = db.query(LiveSignal).one()
    assert row.status == "alerted"
    assert row.alert_status == "sent"
    assert row.tdx_run_id == "7"
    assert alert.items[0].bs_flag == "0"
    assert "momentum-buy" == alert.items[0].reason
    assert events[0][0] == "signal"
    db.close()


def test_monitor_level_edge_and_dedup():
    Session, _ = _factory()
    _seed(Session)
    alert = FakeAlert()
    mon = SignalMonitor(1, Session, alert, lambda t, p: None, "alert")
    cache = {(1, "000001.SZ", datetime(2026, 10, 26, 14, 30)): [
        {"name": "buy", "value": 1},
    ]}
    ctx = _ctx()
    mon.evaluate(ctx, _bar(), cache, strategies=ctx.strategies, frequency="1d")
    mon.evaluate(ctx, _bar(), cache, strategies=ctx.strategies, frequency="1d")
    db = Session()
    assert db.query(LiveSignal).count() == 1
    db.close()


def test_monitor_marks_failed_alert():
    Session, _ = _factory()
    _seed(Session)
    mon = SignalMonitor(1, Session, FakeAlert(ok=False), lambda t, p: None, "alert")
    cache = {(1, "000001.SZ", datetime(2026, 10, 26, 14, 30)): [
        {"name": "buy", "value": 1},
    ]}
    ctx = _ctx()
    mon.evaluate(ctx, _bar(), cache, strategies=ctx.strategies, frequency="1d")
    db = Session()
    row = db.query(LiveSignal).one()
    assert row.status == "failed"
    assert row.alert_status == "failed"
    assert "boom" in row.alert_error
    db.close()
```

Add `StockPoolStock` to the import in the test.

- [ ] **Step 2: Run and verify failure**

Run: `uv run --project main pytest main/core/tests/unit/test_signal_monitor.py -q`
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# main/core/engine/live/signal_monitor.py
"""信号命中、边沿去重、落库、send_warn、SSE。"""
import logging
from datetime import datetime
from typing import Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from ...models import LiveSignal
from ...tq.alert import WarnItem
from ..live.timing import now_shanghai

logger = logging.getLogger(__name__)


class SignalMonitor:
    def __init__(
        self, session_id: int, db_session_factory: Callable[[], Session],
        alert, emit: Callable[[str, dict], None], execution_mode: str,
        clock: Callable[[], datetime] = now_shanghai,
    ):
        self.session_id = session_id
        self._db = db_session_factory
        self._alert = alert
        self._emit = emit
        self.execution_mode = execution_mode
        self.clock = clock
        self._last_value: Dict[tuple, int] = {}
        self._load_last_states()

    def _load_last_states(self) -> None:
        db = self._db()
        try:
            rows = (
                db.query(LiveSignal)
                .filter(LiveSignal.live_session_id == self.session_id)
                .order_by(LiveSignal.id)
                .all()
            )
            for row in rows:
                self._last_value[
                    (row.strategy_id, row.stock_code, row.signal_name)
                ] = row.trigger_value
        finally:
            db.close()

    def evaluate(
        self, portfolio, bar, signal_cache: dict, strategies: list,
        frequency: Optional[str] = None,
    ) -> None:
        effective = frequency or bar.period or "1m"
        candidates = []
        for st in strategies:
            if (st.signal_frequency or st.period) != effective:
                continue
            for code in bar.stocks:
                if st.stock_pool is not None and code not in st.stock_pool:
                    continue
                outputs = signal_cache.get(
                    (st.strategy_id, code, bar.bar_time), []
                )
                values = {o["name"]: o["value"] for o in outputs}
                for cfg in st.formula_signals:
                    name = cfg["signal_name"]
                    current = values.get(name)
                    key = (st.strategy_id, code, name)
                    previous = self._last_value.get(key)
                    if (current is not None
                            and current == cfg["trigger_value"]
                            and previous != current):
                        candidates.append({
                            "portfolio_id": portfolio.portfolio_id,
                            "strategy_id": st.strategy_id,
                            "formula_id": getattr(st, "formula_id", None),
                            "formula_name": getattr(st, "formula_name", "") or "",
                            "stock_code": code,
                            "period": st.period,
                            "bar_time": bar.bar_time,
                            "signal_name": name,
                            "signal_type": cfg["signal_type"].value
                            if hasattr(cfg["signal_type"], "value")
                            else str(cfg["signal_type"]),
                            "trigger_value": cfg["trigger_value"],
                            "price": bar.stocks[code].get("close"),
                        })
                    self._last_value[key] = current
        if candidates:
            self._persist_and_alert(candidates)

    def _persist_and_alert(self, candidates: list) -> None:
        db = self._db()
        rows = []
        try:
            for c in candidates:
                exists = db.query(LiveSignal).filter_by(
                    live_session_id=self.session_id,
                    portfolio_strategy_id=c["portfolio_id"],
                    strategy_id=c["strategy_id"],
                    stock_code=c["stock_code"],
                    signal_name=c["signal_name"],
                    bar_time=c["bar_time"],
                ).first()
                if exists is not None:
                    continue
                row = LiveSignal(
                    live_session_id=self.session_id,
                    portfolio_strategy_id=c["portfolio_id"],
                    strategy_id=c["strategy_id"],
                    formula_id=c.get("formula_id"),
                    stock_code=c["stock_code"],
                    period=c["period"],
                    bar_time=c["bar_time"],
                    formula_name=c.get("formula_name", ""),
                    signal_name=c["signal_name"],
                    signal_type=c["signal_type"],
                    trigger_value=c["trigger_value"],
                    price=c.get("price"),
                    execution_mode=self.execution_mode,
                    status="new",
                )
                db.add(row)
                db.flush()
                rows.append(row)
            db.commit()
            if not rows:
                return
            items = [
                WarnItem(
                    stock_code=r.stock_code,
                    time=r.bar_time.strftime("%Y%m%d%H%M%S"),
                    price=str(r.price if r.price is not None else 0),
                    close=str(r.price if r.price is not None else 0),
                    bs_flag="0" if r.signal_type in ("OPEN", "ADD")
                    else "1" if r.signal_type in ("REDUCE", "CLOSE") else "2",
                    reason=(r.formula_name + "-" + r.signal_name)[:25],
                )
                for r in rows
            ]
            try:
                result = self._alert.send_warn(items)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "ErrorId": "", "Error": str(exc)}
            for r in rows:
                r.alerted_at = self.clock()
                if result.get("ok"):
                    r.status = "alerted"
                    r.alert_status = "sent"
                    r.tdx_run_id = str(result.get("run_id") or "")[:30]
                else:
                    r.status = "failed"
                    r.alert_status = "failed"
                    r.alert_error = str(
                        result.get("Error") or result.get("ErrorId") or "send_warn failed"
                    )[:500]
            db.commit()
            for r in rows:
                self._emit("signal", {
                    "signal_id": r.id,
                    "session_id": self.session_id,
                    "portfolio_id": r.portfolio_strategy_id,
                    "strategy_id": r.strategy_id,
                    "stock_code": r.stock_code,
                    "signal_name": r.signal_name,
                    "signal_type": r.signal_type,
                    "bar_time": r.bar_time.isoformat(),
                    "price": str(r.price) if r.price is not None else None,
                    "execution_mode": r.execution_mode,
                    "alert_status": r.alert_status,
                    "alert_error": r.alert_error,
                    "tdx_run_id": r.tdx_run_id,
                })
        except Exception:
            db.rollback()
            logger.exception("persist live signals failed")
        finally:
            db.close()

    def report_runtime_error(self, portfolio, strategy, formula_name: str,
                             message: str) -> None:
        self._emit("decision", {
            "portfolio_id": portfolio.portfolio_id,
            "strategy_id": strategy.strategy_id,
            "stock_code": None,
            "gate": "signal",
            "layer": "formula",
            "action": "error",
            "param_name": formula_name,
            "param_value": None,
            "actual_value": None,
            "requested_qty": None,
            "final_qty": None,
            "message": message[:200],
            "bar_time": None,
        })
```

**公式名/ID 回填（确定方案）**

`StrategyContext` 的 `formula_name`/`formula_id` 字段在 Task 8 Step 3 加入；本任务的测试用 `SimpleNamespace` 已显式提供这两个属性。`SignalMonitor.evaluate` 构造 candidate 时使用：

```python
"formula_name": getattr(st, "formula_name", "") or "",
"formula_id": getattr(st, "formula_id", None),
```

`_persist_and_alert` 写入 `formula_name=c.get("formula_name", "")` 与 `formula_id=c.get("formula_id")`。

生产链路回填位置固定为 `build_engine`：在 `formula_by_strategy` 与 `strategy_formula` 构建完成后、`return LiveEngine(...)` 之前：

```python
for port in portfolios:
    for ctx in port.strategies:
        ctx.formula_name = formula_by_strategy.get(ctx.strategy_id, "")
        ctx.formula_id = strategy_formula.get(ctx.strategy_id)
```

- [ ] **Step 4: Run tests**

Run: `uv run --project main pytest main/core/tests/unit/test_signal_monitor.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add main/core/engine/live/signal_monitor.py main/core/tests/unit/test_signal_monitor.py
git commit -m "feat: add live signal monitor"
```

---

### Task 8: LiveEngine alert/auto 接入

**Files:**
- Modify: `main/core/engine/live_engine.py`, `main/core/engine/strategy_context.py`, `main/core/services/live_service.py`
- Test: `main/core/tests/unit/test_live_engine_alert.py`

**Interfaces:**
- Consumes: `AlertDataDispatcher`, `SignalScheduler`, `SignalMonitor`.
- Produces: `LiveEngine(execution_mode="auto", signal_monitor=None, signal_scheduler=None, formula_id_by_strategy=None)`; `_tick_alert`; `_handle_alert_bar`; auto `_handle_bar` calls monitor when present.

- [ ] **Step 1: Write failing tests**

```python
# main/core/tests/unit/test_live_engine_alert.py
from datetime import datetime
from types import SimpleNamespace

from core.engine.event import BarEvent
from core.engine.live_engine import LiveEngine


class FakePoller:
    def __init__(self):
        self.calls = 0

    def poll(self):
        self.calls += 1


class FakeDispatcher:
    channel = "alert"

    def __init__(self):
        self.calls = []

    def heartbeat(self):
        self.calls.append("heartbeat")
        return True

    def query_quote(self, *a, **kw):
        self.calls.append("quote")
        return []

    def query_calendar(self):
        return []

    def get_positions(self):
        self.calls.append("positions")
        return []

    def query_orders(self):
        self.calls.append("orders")
        return []

    def query_deals(self):
        self.calls.append("deals")
        return []


class FakeMonitor:
    def __init__(self):
        self.calls = []

    def evaluate(self, *a, **kw):
        self.calls.append((a, kw))


class FakeScheduler:
    def __init__(self):
        self.calls = 0

    def due(self, now):
        self.calls += 1
        return []


def _engine(monitor=None, scheduler=None):
    disp = FakeDispatcher()
    eng = LiveEngine(
        session_id=1, portfolios=[], dispatcher=disp,
        bar_poller=FakePoller(), db_session_factory=lambda: None,
        execution_mode="alert", signal_monitor=monitor,
        signal_scheduler=scheduler or FakeScheduler(),
    )
    return eng, disp


def test_tick_alert_does_not_touch_broker_or_orders():
    mon = FakeMonitor()
    sched = FakeScheduler()
    eng, disp = _engine(mon, sched)
    eng._tick_alert(datetime(2026, 10, 26, 9, 30))
    assert "positions" not in disp.calls
    assert "orders" not in disp.calls
    assert "deals" not in disp.calls
    assert sched.calls == 1


def test_handle_alert_bar_fills_and_notifies_without_orders(monkeypatch):
    mon = FakeMonitor()
    eng, _ = _engine(mon)
    called = {"fill": 0, "on_bar": 0}

    def fake_fill(portfolio, bar, **kw):
        called["fill"] += 1

    def fake_on_bar(*a, **kw):
        called["on_bar"] += 1
        raise AssertionError("alert must not call Portfolio.on_bar")

    monkeypatch.setattr(eng.market_data, "fill_signal_cache", fake_fill)
    port = SimpleNamespace(portfolio_id=1, strategies=[], on_bar=fake_on_bar)
    bar = BarEvent(stocks={"000001.SZ": {"close": 12.3}},
                   bar_time=datetime(2026, 10, 26, 14, 30), period="1d")
    eng._handle_alert_bar(port, bar)
    assert called == {"fill": 1, "on_bar": 0}
    assert mon.calls
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --project main pytest main/core/tests/unit/test_live_engine_alert.py -q`
Expected: FAIL `TypeError: __init__() got an unexpected keyword argument 'execution_mode'`.

- [ ] **Step 3: Add context fields + engine signature**

`StrategyContext.__init__` add `formula_name: str = ""`, `formula_id: Optional[int] = None`; assign `self.formula_name`, `self.formula_id`.

`LiveEngine.__init__` signature add:

```python
        execution_mode: str = "auto",
        signal_monitor=None,
        signal_scheduler=None,
        formula_id_by_strategy: Optional[Dict[int, int]] = None,
        enable_signal_monitor: bool = False,
    ```

Early assignments:

```python
        self.execution_mode = execution_mode
        self._signal_monitor = signal_monitor
        self._signal_scheduler = signal_scheduler
        self._formula_id_by_strategy = formula_id_by_strategy or {}
```

Change `MarketDataService` callback:

```python
            on_bar=(
                self._handle_alert_bar
                if execution_mode == "alert" else self._handle_bar
            ),
```

Change `_on_bar` handler selection:

```python
        handler = (
            self._handle_alert_bar
            if self.execution_mode == "alert" else self._handle_bar
        )
        ...
                handler(
                    portfolio, bar, bars_by_code=bars_by_code,
                    df_cache=df_cache, raw_cache=raw_cache,
                )
```

- [ ] **Step 4: Add alert tick/bar + auto monitor hook**

In `start()` wrap `self._inject_startup_periods(...)`:

```python
        if self.execution_mode != "alert":
            self._inject_startup_periods(
                datetime.combine(now_shanghai().date(), datetime.min.time())
            )
```

Wrap `_deals_task` creation:

```python
        if self.execution_mode != "alert":
            self._deals_task = asyncio.create_task(self._deals_loop())
```

As the first statements **inside** `_tick_main` 的 `try:`（沿用现有异常兜底）：

```python
        try:
            if self.execution_mode == "alert":
                self._tick_alert()
                return
            # ... existing body ...
```

Add before `_tick_main` or after:

```python
    def _tick_alert(self, now: Optional[datetime] = None) -> None:
        """alert 单轮：TQ 心跳 → 拉行情 → 调度信号；不碰委托/成交/持仓。"""
        was_online = self._bridge_online
        if not self._dispatcher.heartbeat():
            self._bridge_online = False
            return
        self._bridge_online = True
        if not was_online:
            # 离线恢复：丢弃离线期间积压的 bar/预热缓存，避免恢复首轮补发陈旧预警。
            if hasattr(self._bar_poller, "reset_baseline"):
                self._bar_poller.reset_baseline()
            self._preheat_cache.clear()
        self._bar_poller.poll()
        now = now or now_shanghai()
        if self._signal_scheduler is not None:
            for batch in self._signal_scheduler.due(now):
                self._signal_scheduler.execute(batch)
```

Add `_handle_alert_bar` near `_handle_bar`:

```python
    def _handle_alert_bar(self, portfolio, bar, bars_by_code=None,
                          df_cache=None, raw_cache=None) -> None:
        """alert 模式：只算公式 + 监控信号，不下单、不跑风控。"""
        self.market_data.fill_signal_cache(
            portfolio, bar, bars_by_code=bars_by_code,
            df_cache=df_cache, raw_cache=raw_cache,
        )
        if self._signal_monitor is not None:
            self._signal_monitor.evaluate(
                portfolio, bar, self.market_data.signal_cache,
                strategies=portfolio.strategies,
                frequency=bar.period or "1m",
            )
```

In `_handle_bar` after `_fill_signal_cache` and before `portfolio.on_bar`, add:

```python
        if self._signal_monitor is not None:
            self._signal_monitor.evaluate(
                portfolio, bar, self.signal_cache,
                strategies=portfolio.strategies,
                frequency=bar.period or "1m",
            )
```

Wrap existing `self._emit("signal", {...})` in the order loop:

```python
                # 1w/1mon 的 signal_cache 由注入路径提供、monitor 不重算，
                # 保留旧成单 SSE 避免前端丢信号；其余周期由 monitor 统一发。
                if self._signal_monitor is None or bar.period in ("1w", "1mon"):
                    self._emit("signal", {... existing payload ...})
```

This keeps direct-constructor tests unchanged; production auto avoids duplicate SSE.

- [ ] **Step 5: Run tests + existing live engine tests**

Run:
```powershell
uv run --project main pytest main/core/tests/unit/test_live_engine_alert.py main/core/tests/unit/test_live_engine.py -q
```
Expected: new tests PASS; existing live engine tests PASS (no monitor by default).

- [ ] **Step 6: Commit**

```powershell
git add main/core/engine/live_engine.py main/core/engine/strategy_context.py main/core/tests/unit/test_live_engine_alert.py
git commit -m "feat: wire alert mode into live engine"
```

---

### Task 9: build_engine / start_session / 预检

**Files:**
- Modify: `main/core/services/live_service.py`, `main/core/api/live.py`
- Test: `main/core/tests/unit/test_live_alert_service.py`

**Interfaces:**
- Consumes: `AlertDataDispatcher`, `TQAlert`, `SignalMonitor`, `SignalScheduler`, `validate_signal_config`.
- Produces: `create_session` default alert; `build_engine(..., execution_mode="auto")`; `preflight_signal_config(db, session_id, execution_mode)`; `start_session` alert heartbeat path.

- [ ] **Step 1: Write failing service tests**

```python
# main/core/tests/unit/test_live_alert_service.py
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.models import (
    Base, Formula, FormulaSignal, LiveSession, LiveSessionPortfolio,
    PortfolioStrategy, StockPool, StockPoolStock, Strategy,
)
from core.services import live_service


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed(db, execution_mode="alert"):
    db.add(LiveSession(id=1, name="s", mode="simulation", status="stopped",
                       engine="tdx", execution_mode=execution_mode))
    pool = StockPool(code="P", name="p")
    db.add(pool)
    db.flush()
    db.add(StockPoolStock(pool_id=pool.id, stock_code="000001.SZ"))
    f = Formula(id=1, name="momentum", content="C")
    db.add(f)
    db.flush()
    db.add(FormulaSignal(formula_id=1, signal_name="buy",
                         signal_type="OPEN", trigger_value=1))
    port = PortfolioStrategy(id=1, name="p", stock_pool_id=pool.id,
                             initial_capital=Decimal("100000"))
    db.add(port)
    db.add(LiveSessionPortfolio(session_id=1, portfolio_strategy_id=1))
    db.add(Strategy(id=1, portfolio_id=1, name="s1", formula_id=1,
                    period="1d", role="master", signal_frequency="1w",
                    signal_at="09:00", signal_weekday=1,
                    signal_data_end="completed"))
    db.commit()


def test_create_session_defaults_to_alert():
    db = _db()
    row = live_service.create_session(db, {"name": "s", "engine": "tdx"}, "simulation")
    assert row["execution_mode"] == "alert"


def test_preflight_rejects_builtin():
    db = _db()
    _seed(db)
    db.query(Formula).filter_by(id=1).update({"engine": "builtin"})
    db.commit()
    status, msg = live_service.preflight_signal_config(db, 1, "alert")
    assert status == 503
    assert "通达信导入公式" in msg


def test_preflight_rejects_auto_custom_frequency():
    db = _db()
    _seed(db)
    status, msg = live_service.preflight_signal_config(db, 1, "auto")
    assert status == 503
    assert "P2" in msg
```

These tests must not call TQ. Therefore `preflight_signal_config` must run the TQ-only checks after the pure config checks and the tests should monkeypatch `TQAlert.preflight`/formula smoke. Add a separate assertion or monkeypatch:

```python
def test_preflight_alert_passes_with_fake_tq(monkeypatch):
    db = _db()
    _seed(db)
    monkeypatch.setattr(live_service, "_preflight_tq_alert", lambda: None)
    monkeypatch.setattr(live_service, "_smoke_formula_outputs",
                        lambda *a, **kw: None)
    assert live_service.preflight_signal_config(db, 1, "alert") is None
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --project main pytest main/core/tests/unit/test_live_alert_service.py -q`
Expected: FAIL missing `execution_mode` key / `preflight_signal_config`.

- [ ] **Step 3: Implement service changes**

`main/core/services/live_service.py` imports add:

```python
from core.engine.live.alert_dispatcher import AlertDataDispatcher
from core.engine.live.signal_monitor import SignalMonitor
from core.engine.live.signal_scheduler import SignalScheduler
from core.services.signal_schedule import (
    effective_signal_frequency, validate_signal_config,
)
from core.tq.alert import TQAlert
```

`create_session`:

```python
def create_session(db: Session, data: dict, mode: str) -> dict:
    """创建 session。新会话默认 alert；老会话由迁移保持 auto。"""
    execution_mode = data.get("execution_mode") or "alert"
    session = LiveSession(
        name=data["name"], mode=mode, status="stopped",
        engine=data.get("engine") or "iquant",
        execution_mode=execution_mode,
    )
    ...
    return {"id": session.id, "status": session.status,
            "engine": session_engine(session),
            "execution_mode": session.execution_mode}
```

`get_session` and `list_sessions` serialization add `"execution_mode": s.execution_mode`. `list_sessions` builds dict around line 500; add there.

`build_engine` signature add `execution_mode: str = "auto"`. At start:

```python
    if execution_mode == "alert":
        dispatcher = AlertDataDispatcher()
    elif dispatcher_cls is None:
        ...
```

Pass to `LiveEngine`:

```python
        execution_mode=execution_mode,
        formula_id_by_strategy=strategy_formula,
```

`build_engine` also after `formula_by_strategy` built:

```python
    for port in portfolios:
        for ctx in port.strategies:
            ctx.formula_name = formula_by_strategy.get(ctx.strategy_id, "")
            ctx.formula_id = strategy_formula.get(ctx.strategy_id)
```

**监控器/调度器构造（在 `LiveEngine` 内部，避免依赖尚未存在的 engine `_emit`）**

`LiveEngine.__init__` 增加 `enable_signal_monitor: bool = False`（Task 8 的签名块同步加入）。默认 False 时与旧版完全一致，现有直接构造测试不受影响；`build_engine` 必须传 True。

1. 扩展 Task 8 的早赋值（位于 `self.ctx = EngineContext(...)` 之后、`self.market_data = MarketDataService(...)` 之前）：

```python
self.execution_mode = execution_mode
self._formula_id_by_strategy = formula_id_by_strategy or {}
self._tq_alert = None
self._signal_monitor = signal_monitor
if enable_signal_monitor and signal_monitor is None:
    from ...tq.alert import TQAlert
    from .live.signal_monitor import SignalMonitor
    self._tq_alert = TQAlert()
    self._signal_monitor = SignalMonitor(
        session_id, db_session_factory, self._tq_alert, self._emit,
        execution_mode,
    )
```

2. 在 `self.market_data = MarketDataService(...)` 之后、`self._order_machine = ...` 之前：

```python
self._signal_scheduler = signal_scheduler
if (enable_signal_monitor and execution_mode == "alert"
        and signal_scheduler is None):
    from .live.signal_scheduler import SignalScheduler
    self._signal_scheduler = SignalScheduler(
        self.ctx, self.market_data, lambda: self._calendar,
        self.market_data._formula_by_strategy,
        self.market_data._formula_count_by_name,
        self._formula_id_by_strategy,
        self._signal_monitor, self._tq_alert,
    )
```

`self._calendar` 在 `__init__` 首行已构建；`market_data` 的映射属性名已验证为 `_formula_by_strategy` / `_formula_count_by_name`。monitor 与 scheduler 共用同一个 `TQAlert` 实例。

3. `build_engine` 不构造 monitor/scheduler，只透传配置：

```python
return LiveEngine(
    ...,
    execution_mode=execution_mode,
    formula_id_by_strategy=strategy_formula,
    enable_signal_monitor=True,
)
```

`start_session` 的精确改法（保留现有 heartbeat 分支，仅插入 alert 分支与预检）：

```python
    execution_mode = getattr(session, "execution_mode", None) or "auto"
    engine = build_engine(
        session_id, db, mode,
        db_session_factory=db_session_factory, dispatcher_cls=dispatcher_cls,
        engine=session_engine(session),
        execution_mode=execution_mode,
    )
    if not engine.dispatcher.heartbeat():
        channel = getattr(engine.dispatcher, "channel", "")
        detail = getattr(engine.dispatcher, "last_error", "") or ""
        if channel == "alert":
            return None, session, (
                503,
                "通达信行情未就绪：请先启动并登录通达信客户端（%s）"
                % (detail or "未取到行情"),
            )
        # ... 保留现有 ths_ui / tdx_tq / qmt / 桥 分支 ...
    preflight_err = preflight_signal_config(db, session_id, execution_mode)
    if preflight_err is not None:
        return None, session, preflight_err
    if execution_mode != "alert":
        engine.recover(db)
    return engine, session, None
```

`preflight_signal_config` 与两个辅助函数的完整实现：

```python
def _session_strategies(db: Session, session_id: int):
    links = (
        db.query(LiveSessionPortfolio)
        .filter(LiveSessionPortfolio.session_id == session_id)
        .all()
    )
    rows = []
    for link in links:
        strategies = (
            db.query(Strategy)
            .filter(Strategy.portfolio_id == link.portfolio_strategy_id)
            .order_by(Strategy.id)
            .all()
        )
        rows.extend((link.portfolio_strategy_id, st) for st in strategies)
    return rows


def _preflight_tq_alert():
    msg = TQAlert().preflight()
    return (503, msg) if msg else None


def _smoke_formula_outputs(db: Session, session_id: int):
    """alert 启动前用 1 只股票试算公式，确认输出线覆盖 FormulaSignal。"""
    formula = TQFormula()
    seen = set()
    for pid, st in _session_strategies(db, session_id):
        smoke_key = (st.formula_id, st.period)
        if st.formula_id is None or smoke_key in seen:
            continue
        seen.add(smoke_key)
        f = db.query(Formula).filter_by(id=st.formula_id).first()
        if f is None:
            return (503, "策略 %s 引用的公式不存在" % st.name)
        signals = db.query(FormulaSignal).filter_by(formula_id=f.id).all()
        if not signals:
            return (503, "公式 %s 未配置信号输出线" % f.name)
        codes = resolve_stock_codes(db, pid)
        if not codes:
            continue
        code = sorted(codes)[0]
        # 至少拉 200 根，避免周线 MACD 等长周期输出线因历史不足变 NaN
        count = max(f.formula_count or 200, 200)
        raw = formula.compute(
            f.name, "", [code], period=st.period,
            count=count, end_time=now_shanghai().strftime("%Y%m%d"),
        )
        if (not isinstance(raw, dict)
                or str(raw.get("ErrorId", "0")) not in ("0", "19")):
            detail = raw.get("Error") if isinstance(raw, dict) else "无返回"
            return (503, "公式 %s 试算失败：%s" % (f.name, detail or "无返回"))
        produced = MarketDataService._extract_latest_signal(raw, code)
        names = {o["name"] for o in produced}
        missing = [s.signal_name for s in signals if s.signal_name not in names]
        if missing:
            return (503, "公式 %s 输出缺少信号线：%s" % (
                f.name, "、".join(missing)))
    return None


def preflight_signal_config(db: Session, session_id: int, execution_mode: str):
    """启动前检查；返回 None 或 (http_status, message)。

    alert: TQ-only + 频率规则 + send_warn 可用 + 公式试算输出线。
    auto（P0 兼容）: 只查 TQ engine + 频率规则，不做 TQ 试算/告警探测，
    避免历史自动会话因客户端未就绪而无法启动。
    """
    if execution_mode not in ("alert", "auto"):
        return (400, "execution_mode 必须为 alert/auto")
    for pid, st in _session_strategies(db, session_id):
        if st.formula_id is None:
            if execution_mode == "alert":
                return (503, "策略 %s 未绑定公式" % st.name)
            continue
        f = db.query(Formula).filter_by(id=st.formula_id).first()
        if f is None:
            return (503, "策略 %s 引用的公式不存在" % st.name)
        if f.engine != "tq":
            return (503, "实盘只支持通达信导入公式（%s.engine=%s）" % (
                f.name, f.engine))
        msg = validate_signal_config(
            st.period, st.signal_frequency, st.signal_at,
            st.signal_weekday, st.signal_data_end,
        )
        if msg:
            return (503, msg)
        freq = effective_signal_frequency(st.period, st.signal_frequency)
        if execution_mode == "auto" and freq != st.period:
            return (503, "全自动 P0 仅支持 signal_frequency == period；"
                         "请改用 alert 或等待 P2（策略 %s）" % st.name)
    if execution_mode == "alert":
        err = _preflight_tq_alert()
        if err:
            return err
        err = _smoke_formula_outputs(db, session_id)
        if err:
            return err
    return None
```

`live_service` 新增 imports：

```python
from core.models import FormulaSignal
from core.engine.live.market_data import MarketDataService
from core.engine.live.timing import now_shanghai
```

`resolve_stock_codes(db, pid)` 已存在，可直接复用。`pid` 为 `portfolio_strategy_id`。

API `create_session` 在 engine 校验之后加入：

```python
    execution_mode = str(data.get("execution_mode") or "alert")
    if execution_mode not in ("alert", "auto"):
        return err(400, "execution_mode 非法，仅支持 alert(半自动) / auto(全自动)")
    data["execution_mode"] = execution_mode
```

API `start_session` 的 dispatcher 选择改为：

```python
        dispatcher_cls=(
            None if (session.execution_mode or "auto") == "alert"
            else _dispatcher_cls_for(session)
        ),
```

`build_engine` 的 alert 分支优先于 `dispatcher_cls`，因此即使传入也会被忽略。

**Step 3b: 现有集成测试显式使用 auto（否则默认 alert 会走 TQ 路径）**

`main/core/tests/integration/test_live_engine_api.py::_create_session` 增加参数并写入 payload：

```python
def _create_session(c, name="live-test", portfolio_ids=(1,), mode="simulation",
                    execution_mode="auto"):
    resp = c.post("/api/live/sessions", json={
        "name": name, "portfolio_ids": list(portfolio_ids), "mode": mode,
        "execution_mode": execution_mode,
    })
```

`main/core/tests/integration/test_live_engine_select_api.py` 中所有会 `/start` 的 session 创建 payload 增加 `"execution_mode": "auto"`（`test_start_tdx_session_reports_not_ready`、`test_start_qmt_session_reports_not_ready`）。不启动的创建用例可保持默认 alert。

同时给该文件 `client` fixture 补 `SessionLocal` 指向测试库（否则 auto `build_engine` 构造 `SignalMonitor` 会查真实 `dev.db`）：

```python
    app.dependency_overrides[get_db] = override_get_db
    from core.api import live as live_api
    live_api.SessionLocal = Session
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()
    live_api._ENGINES.clear()
    import core.db as db_mod
    live_api.SessionLocal = db_mod.SessionLocal
```

- [ ] **Step 4: Run tests**

Run:
```powershell
uv run --project main pytest main/core/tests/unit/test_live_alert_service.py main/core/tests/integration/test_live_engine_api.py main/core/tests/integration/test_live_engine_select_api.py -q
```
Expected: PASS. If existing integration fails due preflight, ensure auto path skips TQ smoke and `enable_signal_monitor` default behavior.

- [ ] **Step 5: Commit**

```powershell
git add main/core/services/live_service.py main/core/api/live.py main/core/engine/live_engine.py main/core/tests/unit/test_live_alert_service.py main/core/tests/integration/test_live_engine_api.py main/core/tests/integration/test_live_engine_select_api.py
git commit -m "feat: add alert execution mode preflight"
```

---

### Task 10: GET /signals

**Files:**
- Modify: `main/core/services/live_service.py`, `main/core/api/live.py`
- Test: `main/core/tests/integration/test_live_signals_api.py`

**Interfaces:**
- Produces: `session_signals(db, session_id, strategy_id=None, stock_code=None, signal_type=None, execution_mode=None, alert_status=None, start=None, end=None, limit=100, offset=0) -> dict | None`; route `GET /api/live/sessions/{id}/signals`.

- [ ] **Step 1: Write failing API test**

```python
# main/core/tests/integration/test_live_signals_api.py
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.db import get_db
from core.main import app
from core.models import (
    Base, Formula, LiveSession, LiveSignal, PortfolioStrategy, StockPool, Strategy,
)


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c, Session
    app.dependency_overrides.clear()


def _seed(Session):
    db = Session()
    db.add(LiveSession(id=1, name="s", mode="simulation", status="running",
                       engine="tdx", execution_mode="alert"))
    pool = StockPool(code="P", name="p")
    db.add(pool)
    db.flush()
    db.add(Formula(id=1, name="momentum", content="C"))
    port = PortfolioStrategy(id=1, name="p", stock_pool_id=pool.id,
                             initial_capital=Decimal("100000"))
    st = Strategy(id=1, portfolio_id=1, name="s1", formula_id=1,
                  period="1d", role="master")
    db.add_all([port, st])
    db.flush()
    db.add(LiveSignal(
        live_session_id=1, portfolio_strategy_id=1, strategy_id=1,
        stock_code="000001.SZ", period="1d", bar_time=datetime(2026, 10, 23, 15),
        formula_name="momentum", signal_name="buy", signal_type="OPEN",
        trigger_value=1, price=Decimal("12.34"), execution_mode="alert",
        status="alerted", alert_status="sent", tdx_run_id="7",
    ))
    db.commit()
    db.close()


def test_get_signals_filters_and_paginates(client):
    c, Session = client
    _seed(Session)
    resp = c.get("/api/live/sessions/1/signals?signal_type=OPEN&limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["total"] == 1
    item = body["data"]["items"][0]
    assert item["stock_code"] == "000001.SZ"
    assert Decimal(item["price"]) == Decimal("12.34")
    assert item["alert_status"] == "sent"


def test_get_signals_unknown_session_404(client):
    c, _ = client
    assert c.get("/api/live/sessions/99/signals").json()["code"] == 404
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --project main pytest main/core/tests/integration/test_live_signals_api.py -q`
Expected: FAIL 404 (route missing) or import error.

- [ ] **Step 3: Implement service + route**

`main/core/services/live_service.py` imports `LiveSignal`. Add:

```python
def serialize_signal(s: LiveSignal) -> dict:
    return {
        "id": s.id,
        "portfolio_id": s.portfolio_strategy_id,
        "strategy_id": s.strategy_id,
        "stock_code": s.stock_code,
        "period": s.period,
        "bar_time": s.bar_time.isoformat() if s.bar_time else None,
        "formula_name": s.formula_name,
        "signal_name": s.signal_name,
        "signal_type": s.signal_type,
        "trigger_value": s.trigger_value,
        "price": str(s.price) if s.price is not None else None,
        "execution_mode": s.execution_mode,
        "status": s.status,
        "alert_status": s.alert_status,
        "alert_error": s.alert_error,
        "tdx_run_id": s.tdx_run_id,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "alerted_at": s.alerted_at.isoformat() if s.alerted_at else None,
    }


def session_signals(db, session_id, strategy_id=None, stock_code=None,
                    signal_type=None, execution_mode=None, alert_status=None,
                    start=None, end=None, limit=100, offset=0):
    if db.query(LiveSession).filter(LiveSession.id == session_id).first() is None:
        return None
    q = db.query(LiveSignal).filter(LiveSignal.live_session_id == session_id)
    if strategy_id is not None:
        q = q.filter(LiveSignal.strategy_id == strategy_id)
    if stock_code:
        q = q.filter(LiveSignal.stock_code == stock_code)
    if signal_type:
        q = q.filter(LiveSignal.signal_type == signal_type)
    if execution_mode:
        q = q.filter(LiveSignal.execution_mode == execution_mode)
    if alert_status:
        q = q.filter(LiveSignal.alert_status == alert_status)
    if start is not None:
        q = q.filter(LiveSignal.bar_time >= start)
    if end is not None:
        q = q.filter(LiveSignal.bar_time <= end)
    total = q.count()
    rows = (q.order_by(LiveSignal.bar_time.desc(), LiveSignal.id.desc())
            .offset(offset).limit(min(max(limit, 1), 500)).all())
    return {"items": [serialize_signal(r) for r in rows], "total": total}
```

`main/core/api/live.py` add route after `get_session`:

```python
@router.get("/sessions/{session_id}/signals")
def session_signals(
    session_id: int,
    strategy_id: Optional[int] = None,
    stock_code: Optional[str] = None,
    signal_type: Optional[str] = None,
    execution_mode: Optional[str] = None,
    alert_status: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    def _dt(value):
        return datetime.fromisoformat(value) if value else None

    try:
        data = svc.session_signals(
            db, session_id, strategy_id=strategy_id, stock_code=stock_code,
            signal_type=signal_type, execution_mode=execution_mode,
            alert_status=alert_status, start=_dt(start), end=_dt(end),
            limit=limit, offset=offset,
        )
    except ValueError:
        return err(400, "start/end 必须为 ISO 时间")
    if data is None:
        return err(404, "资源不存在")
    return ok(data)
```

- [ ] **Step 4: Run tests**

Run: `uv run --project main pytest main/core/tests/integration/test_live_signals_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add main/core/services/live_service.py main/core/api/live.py main/core/tests/integration/test_live_signals_api.py
git commit -m "feat: add live signals query API"
```

---

### Task 11: 端到端 alert 集成 + 文档收尾

**Files:**
- Modify: `CLAUDE.md`, `AGENTS.md`, `docs/live-full-flow-design.md`
- Test: `main/core/tests/integration/test_live_alert_flow.py`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: one integration test proving alert has no orders and `send_warn` is called; docs describe new mode.

- [ ] **Step 1: Write failing integration test**

Use fakes to avoid real TQ/DB; the test constructs `LiveEngine` with `execution_mode="alert"`, fake `AlertDataDispatcher`-like quote, fake monitor recorder, and calls `_handle_alert_bar`.

```python
# main/core/tests/integration/test_live_alert_flow.py
from datetime import datetime
from types import SimpleNamespace

from core.engine.event import BarEvent
from core.engine.live_engine import LiveEngine


class Quote:
    def query_quote(self, code, period="1m", count=10):
        return [{"stime": "20261023150000", "open": 12.0, "high": 12.5,
                 "low": 11.9, "close": 12.34, "volume": 100, "amount": 1200}]


class Dispatcher:
    channel = "alert"

    def __init__(self):
        self.orders = 0

    def heartbeat(self):
        return True

    def query_quote(self, *a, **kw):
        return Quote().query_quote(*a, **kw)

    def query_calendar(self):
        return []

    def place_order(self, *a, **kw):
        self.orders += 1
        raise AssertionError("alert must not place order")


class Monitor:
    def __init__(self):
        self.calls = []

    def evaluate(self, portfolio, bar, cache, strategies, frequency):
        self.calls.append((bar.bar_time, frequency))


class Poller:
    stock_codes = ["000001.SZ"]

    def poll(self):
        pass


def test_alert_bar_never_places_order():
    disp = Dispatcher()
    mon = Monitor()
    eng = LiveEngine(
        session_id=1, portfolios=[], dispatcher=disp,
        bar_poller=Poller(), db_session_factory=lambda: None,
        execution_mode="alert", signal_monitor=mon,
        signal_scheduler=None,
    )
    eng.market_data.fill_signal_cache = lambda *a, **kw: None
    port = SimpleNamespace(portfolio_id=1, strategies=[])
    bar = BarEvent(stocks={"000001.SZ": {"close": 12.34}},
                   bar_time=datetime(2026, 10, 26, 14, 30), period="1d")
    eng._handle_alert_bar(port, bar)
    assert disp.orders == 0
    assert mon.calls == [(datetime(2026, 10, 26, 14, 30), "1d")]
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run --project main pytest main/core/tests/integration/test_live_alert_flow.py -q`
Expected: PASS after Task 8; if run before, fail on missing `execution_mode`.

- [ ] **Step 3: Update docs**

In `CLAUDE.md` / `AGENTS.md` add to the live section:

```markdown
### 实盘信号预警（P0）

- `live_sessions.execution_mode`：`alert`（默认，半自动）/ `auto`（现有自动交易）。
- `alert` 只走同进程 TQ 行情 + 通达信导入公式（`Formula.engine=tq`），落 `live_signals`，
  调 `send_warn` 推送通达信预警并发 SSE `signal`；不调用 `Portfolio.on_bar`/下单。
- `1w/1mon` 由 `SignalScheduler` 在周一/每月首个交易日 `signal_at` 用上一完整周期
  `TQFormula.compute(end_time=窗口结束日)` 求值；P0 只允许 `completed`。
- `GET /api/live/sessions/{id}/signals` 查询信号；Web 面板/重发属 P1。
- `auto` 保持现有下单链路；P0 只支持 `signal_frequency == period`。
```

In `docs/live-full-flow-design.md` add a short「信号预警支路」section pointing to the spec and this plan.

- [ ] **Step 4: Run full targeted backend suite**

Run:
```powershell
uv run --project main pytest main/core/tests/unit/test_live_signal_model.py main/core/tests/unit/test_signal_schedule.py main/core/tests/unit/test_strategy_signal_fields.py main/core/tests/unit/test_tq_alert.py main/core/tests/unit/test_alert_dispatcher.py main/core/tests/unit/test_signal_scheduler.py main/core/tests/unit/test_signal_monitor.py main/core/tests/unit/test_live_engine_alert.py main/core/tests/unit/test_live_alert_service.py main/core/tests/integration/test_live_signals_api.py main/core/tests/integration/test_live_alert_flow.py -q
uv run --project main pytest main/core/tests/unit/test_live_engine.py main/core/tests/integration/test_live_engine_api.py -q
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```powershell
git add CLAUDE.md AGENTS.md docs/live-full-flow-design.md main/core/tests/integration/test_live_alert_flow.py
git commit -m "docs: document live signal alert mode"
```

---

## Self-Review

**Spec coverage**
- `TQAlert` send_warn/send_message/preflight: Task 4.
- `live_signals` + migrations: Task 1.
- Strategy fields + validation: Tasks 1-3.
- `live_sessions.execution_mode` default/migration: Tasks 1, 9.
- `AlertDataDispatcher` + alert tick: Tasks 5, 8.
- `SignalScheduler`: Task 6.
- `SignalMonitor` + edge/dedup/SSE: Task 7.
- Preflight TQ-only / frequency / formula / send_warn: Task 9 (auto smoke intentionally deferred to preserve existing auto behavior; alert strict).
- `GET /signals`: Task 10.
- Unit/integration tests: each task + Task 11.
- P1 frontend/replay/retry/config: explicitly out of scope.

**Placeholder scan**
- No `TBD`/`TODO`/“similar to Task N”; every task has test code, implementation code, exact run command, and commit.

**Type consistency**
- `DueBatch.frequency` is the push frequency; `DueBatch.period` is the formula period.
- `SignalMonitor.evaluate(..., frequency=...)` filters `(st.signal_frequency or st.period) == frequency`; scheduler passes `batch.frequency`, engine passes `bar.period or "1m"`.
- `TQAlert.send_warn` returns `{"ok", "ErrorId", "Error", "run_id", "batches"}`; monitor reads exactly those keys.
- `LiveSignal` field names match the API serializer and migration.
- `AlertDataDispatcher.query_quote(code, period, count)` delegates positionally; `TdxTqDispatcher` accepts the same.
- `StrategyContext.formula_name/formula_id` are populated by `build_engine` after `formula_by_strategy` is built.
- `SignalScheduler.due` normalizes `signal_at/signal_weekday/signal_data_end` into the group key and `_fired` key, so NULL defaults and explicit defaults share one trigger (no double send) while different `signal_at` schedules fire independently.
- `LiveEngine._handle_bar` suppresses the old order `signal` SSE only when a monitor is present **and** the bar period is not `1w/1mon`; weekly/monthly have no monitor cache path in auto P0, so they keep the legacy SSE.
- Both `test_live_engine_api` and `test_live_engine_select_api` fixtures point `live_api.SessionLocal` at the test DB, because `build_engine(enable_signal_monitor=True)` constructs `SignalMonitor` (which reads `live_signals`) before `/start` heartbeat returns.

**Known deliberate P0 compatibility**
- Existing direct-constructor `LiveEngine` tests keep old `signal` SSE because `signal_monitor=None`; production `build_engine` sets `enable_signal_monitor=True`.
- Auto preflight checks only TQ engine + frequency; full formula smoke and `send_warn` strictness are alert-only in P0 to avoid breaking historical auto sessions. This is the only intentional narrowing versus the design text and should be recorded in the plan review.
