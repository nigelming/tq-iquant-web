# 计划 0001 — P0 补字段 + 级联删除 + 索引（新迁移）

> 状态：已批准，实施中
> 日期：2026-07-30
> 作者：Claude Code（从 opencode 迁移开发后）

## Context

代码现状与设计文档对照发现两个 schema 缺口：

1. **`live_session_portfolios` 缺 3 个字段**：设计 5.4 表 12 要求 `status`+`circuit_breaker_count`+`created_at`+`updated_at`，现状只有 `status`。`circuit_breaker_count` 是熔断手动恢复机制的数据基础（设计：max_drawdown 累计触发 3 次后 status 转 circuit_broken）。
2. **级联删除几乎全缺 + 索引全缺**：设计 7.2 节要求 9 组索引、7.3 节要求多组 CASCADE/RESTRICT。现状仅 1 处 ondelete（live_session_portfolios.session_id CASCADE），0 个索引。

用户决策：开启 SQLite 外键强制（`PRAGMA foreign_keys=ON`）让 CASCADE/RESTRICT 真正生效；新建增量迁移，不改已落地的 init 迁移。

目标：补齐字段、级联、索引，让 schema 与设计一致，并在开发期 SQLite 即生效外键约束（与生产 PG 行为一致）。

## 改动文件清单

### 1. `main/core/db.py`（开启外键 pragma）
在 engine 建立后注册 event listener，每个连接打开时执行 `PRAGMA foreign_keys=ON`。SQLite 的 pragma 是 per-connection 的，必须用 `connect` 事件逐连接设置：
```python
from sqlalchemy import event

# ... 现有 engine = create_engine(...) ...

@event.listens_for(engine, "connect")
def _enable_sqlite_fk(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
```
注意：`create_engine` 的 `connect_args={"check_same_thread": False}` 保持不变。外键开启后，RESTRICT 会阻止有引用的删除（如删 portfolio_strategies 但有 backtest_records 引用），CASCADE 会自动清子表（如删 backtest_records 自动删 trades/snapshots/evaluations）。

### 2. `main/core/models/live_session_portfolio.py`（补字段）
新增 3 列，与设计表 12 对齐：
```python
from sqlalchemy.sql import func

# 现有 id/session_id/portfolio_strategy_id/status 保留
circuit_breaker_count = Column(Integer, default=0)
created_at = Column(DateTime, server_default=func.now())
updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
```
`circuit_breaker_count` 默认 0，累积 max_drawdown 触发次数。

### 3. 各 model 的 ForeignKey 加 ondelete（级联规则）
按设计 7.3 节，给以下 FK 加 `ondelete=`：

| 文件 | FK | ondelete |
|---|---|---|
| `models/strategy.py` | `portfolio_id` → portfolio_strategies | CASCADE |
| `models/strategy.py` | `formula_id` → formulas | RESTRICT |
| `models/backtest_record.py` | `portfolio_strategy_id` → portfolio_strategies | RESTRICT |
| `models/backtest_trade.py` | `backtest_record_id` → backtest_records | CASCADE |
| `models/backtest_daily_snapshot.py` | `backtest_record_id` → backtest_records | CASCADE |
| `models/backtest_evaluation.py` | `backtest_record_id` → backtest_records | CASCADE |
| `models/live_session_portfolio.py` | `portfolio_strategy_id` → portfolio_strategies | RESTRICT（session_id 已 CASCADE）|
| `models/live_order.py` | `live_session_id` → live_sessions | CASCADE |
| `models/live_trade.py` | `live_session_id` → live_sessions | CASCADE |
| `models/formula_signal.py` | `formula_id` → formulas | CASCADE |
| `models/stock_pool_stock.py` | `pool_id` → stock_pools | CASCADE |
| `models/portfolio_strategy.py` | `stock_pool_id` → stock_pools | RESTRICT |

**不改的 FK**（设计未明确，保持默认无 ondelete）：backtest_trade.strategy_id/formula_signal_id、live_order.strategy_id/portfolio_strategy_id、live_trade.live_order_id/strategy_id/portfolio_strategy_id、strategy.master_strategy_id。

写法示例：`Column(Integer, ForeignKey("portfolio_strategies.id", ondelete="CASCADE"))`。

### 4. 各 model 加 Index（索引设计）
设计 7.2 节 9 组索引，2 组已是 UniqueConstraint（stock_pool_stocks、live_session_portfolios）无需再加。新增 7 组普通索引，用 `__table_args__` 声明：

| 文件 | 索引列 |
|---|---|
| `models/backtest_trade.py` | (backtest_record_id, bar_time) |
| `models/backtest_daily_snapshot.py` | (backtest_record_id, target_type, target_id, snap_date) |
| `models/backtest_evaluation.py` | (backtest_record_id, target_type, target_id) |
| `models/live_order.py` | (live_session_id, status)、(portfolio_strategy_id) |
| `models/live_trade.py` | (live_session_id, trade_time)、(live_order_id)、(portfolio_strategy_id, trade_time) |

写法：`from sqlalchemy import Index`，`__table_args__ = (Index("ix_backtest_trades_rec_bar", "backtest_record_id", "bar_time"),)`。给索引显式命名（ix_<表>_<语义>），便于迁移可读。

注意 `backtest_daily_snapshot` 的 4 列复合索引含 `target_type+target_id`——若将来该查询模式变高频可再调，现按设计落地。

### 5. 新建 Alembic 增量迁移
在 `main/` 目录生成：
```bash
uv run alembic revision --autogenerate -m "add cascade delete, indexes, live_session_portfolio fields"
```
**预期迁移内容**：
- `op.add_column('live_session_portfolios', circuit_breaker_count/created_at/updated_at)`
- 对 12 个 FK 加 ondelete：SQLite **不支持 ALTER FK**，autogenerate 可能生成 `op.drop_constraint`+`op.create_constraint` 或无法直接改。**这是本计划最大技术风险点**，见下方"SQLite ALTER FK 限制"。
- `op.create_index(...)` × 7

**SQLite ALTER FK 限制**：SQLite 不能直接 ALTER 已有 FK 的 ondelete 行为。标准做法是 batch mode 重建表（`op.batch_alter_table` 复制表→重建带新 FK→拷数据）。autogenerate 对 SQLite 的 FK 变更支持有限，**很可能需要手工编辑迁移文件**用 batch_alter_table。计划：autogenerate 生成骨架后，检查其对 FK 变更的处理，若不正确则手工改写为 batch_alter_table 形式。

由于现有 dev.db 已有表和数据（哪怕是空的），batch 重建表是安全的标准操作，会保留现有数据。

### 6. `main/alembic/env.py`（batch mode 配置）
SQLite batch_alter_table 需要 `render_as_batch=True` 传给 `context.configure`，否则 batch 操作无法正确识别现有表结构。在 env.py 的 `run_migrations_offline` 和 `run_migrations_online` 的 `context.configure(...)` 调用中加 `render_as_batch=True`：
```python
context.configure(
    connection=connection,
    target_metadata=target_metadata,
    render_as_batch=True,  # SQLite batch_alter_table 支持
)
```
offline 模式同理加 `render_as_batch=True`。

## 实施顺序

1. 改 `db.py` 开启外键 pragma
2. 改 `live_session_portfolio.py` 补 3 字段
3. 改 12 个 model 的 FK ondelete
4. 改 5 个 model 加 Index（7 组）
5. 改 `alembic/env.py` 加 `render_as_batch=True`
6. `alembic revision --autogenerate` 生成迁移骨架
7. **检查迁移文件**：验证字段/索引正确；FK 变更部分若 autogenerate 处理不当，手工改写为 batch_alter_table
8. `alembic upgrade head` 应用迁移
9. 验证

## 验证

1. **迁移可应用**：`uv run alembic upgrade head` 无报错，新 revision 出现在 `alembic current`。
2. **字段存在**：
   ```bash
   uv run python -c "from sqlalchemy import inspect; from core.db import engine; i=inspect(engine); print([c['name'] for c in i.get_columns('live_session_portfolios')])"
   ```
   预期含 `circuit_breaker_count/created_at/updated_at`。
3. **索引存在**：
   ```bash
   uv run python -c "from sqlalchemy import inspect; from core.db import engine; i=inspect(engine); print([ix['name'] for ix in i.get_indexes('backtest_trades') + i.get_indexes('live_trades')])"
   ```
   预期含新建的 7 个索引名。
4. **外键 pragma 生效**：
   ```bash
   uv run python -c "from core.db import engine; from sqlalchemy import text; c=engine.connect(); print(c.execute(text('PRAGMA foreign_keys')).fetchone()[0])"
   ```
   预期输出 `1`。
5. **级联生效**：写一个临时脚本——插一条 backtest_record + 一条 backtest_trade，删 record，确认 trade 自动被 CASCADE 删除（外键开启后应级联）。
6. **RESTRICT 生效**：插 portfolio_strategies + 一条 backtest_records 引用它，直接删 portfolio_strategies 应抛 IntegrityError（被 RESTRICT 拦截）。
7. **测试**：`uv run pytest` 仍 9 passed。conftest 用内存 SQLite `create_engine("sqlite:///:memory:")`——**注意 conftest 的 engine 没开 pragma**，若测试涉及级联断言会失败；但现有测试不涉及删除级联，应仍绿。记录此差异，不本次改 conftest。
8. **启动**：`uv run uvicorn core.main:app` 启动无报错，`/api/live/sessions` 仍正常。

## 风险

- **主风险**：SQLite ALTER FK 限制导致 autogenerate 迁移不正确。缓解：手工审查+batch_alter_table 改写，batch 重建表是 SQLite 官方推荐做法，数据安全。
- **conftest engine 未开 pragma**：测试库外键不强制，但现有测试不依赖级联，不影响。若后续测试需级联断言，再给 conftest 加同样的 event listener（本次不改）。
- **现有 dev.db 数据**：batch 重建表保留数据，dev.db 现基本为空，无数据丢失风险。
- **RESTRICT 对现有操作的破坏**：开启外键后，若有代码删除被引用的父行会报 IntegrityError。grep 确认现有 API 无删除 portfolio_strategies/formulas/stock_pools 的操作（这些 DELETE 接口都未实现），故无破坏。
