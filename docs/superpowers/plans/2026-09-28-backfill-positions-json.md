# 回填老回测 positions_json Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用成交流水 + 存量市值为 10 条老回测记录的 15,022 条组合快照回填 `positions_json`，让「机遇Alpha · 中性Alpha」栏目第八节（行业归因）从「需重新回测」变为可用。

**Architecture:** 新建 `main/core/services/positions_backfill.py`，含 3 个纯函数（`replay_cash` / `replay_positions` / `value_positions`，零 I/O，可离线单测）+ 1 个编排函数 `backfill_record`（唯一做 DB/TQ I/O 的层）。新建 `main/scripts/backfill_positions_json.py` 作为薄 CLI，默认 dry-run。

**Tech Stack:** Python 3.13、SQLAlchemy 2.x、SQLite、pytest、TQ SDK（经 `build_index_close_series`）。

设计文档：`docs/superpowers/specs/2026-09-28-backfill-positions-json-design.md`（commit `2cdd4cd`）

## Global Constraints

- 金额一律 `Decimal`，禁止 `float`。`Decimal` 入参统一用 `Decimal(str(x))` 归一化。
- **`market_value` 落库前量化到 2 位小数**（`quantize(Decimal("0.01"))`）。缩放系数 `k` 是无限循环小数（3000/2700），不量化会写出 `999.9999999999999999999999999` 这种值。量化后 Σ 与存量市值的残差 ≤ 0.005×持仓数，对行业权重（÷total_value）影响在 1e-6 量级，可忽略。
- **`cost_value` 不量化**，严格用 `str(avg_cost × quantity)`，与 `Portfolio.positions_detail()`（`main/core/engine/portfolio.py:459-502`）逐字节一致。
- 回填产出的持仓明细字段必须与 `positions_detail()` **同构**：`stock_code` / `quantity` / `sellable_quantity` / `market_value` / `cost_value`，`stock_code` 升序，空仓写 `"[]"`。
- 交易日期取 `bar_time.date()`（`backtest_trades` **无 `trade_date` 列**）；`trade_type` 是大写 `"BUY"` / `"SELL"`。
- **快照含当日成交**：对 `snap_date D` 先应用 `bar_time.date() <= D` 的全部成交。已用 `cash` 列实证（662/662、3091/3091、3080/3080 全中）。
- 取价必须走 `build_index_close_series`（`main/core/services/backtest_service.py:319-367`），它内部持 `get_tdx_lock`；不要新写 TQ 调用。
- 缩放门限：`|k−1| > 0.05` 的记录**拒绝写入**，`force=True` 才覆盖。
- 现金校验为**硬断言**：任一日不匹配则整条记录失败、一行不写。
- 写库为**单次 commit**，不允许部分写入。
- 不新增 API 端点、不改前端、不改回测引擎。
- 所有 `uv run ...` 命令在 `D:\project\tq-iquant-web\main` 目录执行。
- 本仓库 `shared/` 需兼容 Python 3.7，但**本计划不碰 `shared/`**，`main/` 可用 3.13 语法。
- 遵循 `main/core/services/` 现有风格：模块级中文 docstring 说明「为什么」；`recompute_*` 族返回 `{"status": ..., "error": ...}` dict 而非抛异常。

### Decimal 行为备忘（写测试时必须知道，否则断言会假失败）

`Decimal` 除法/乘法不做十进制正规化，`str()` 的尾零取决于指数：

| 表达式 | 结果 | `str()` |
|---|---|---|
| `Decimal("1000.00") / Decimal("100")` | `Decimal("10.00")`（指数 2−0=2） | `"10.00"` |
| `Decimal("10.00") * 100` | `Decimal("1000.00")` | `"1000.00"` |
| `Decimal("10") * 100` | `Decimal("1000")`（指数 0） | `"1000"` |
| `Decimal("1000") / Decimal("2700")` | 28 位有效数字 | `"0.3703703703703703703703703703"` |

→ **测试里的成交价一律写成两位小数字符串**（`"10.00"` 而非 `"10"`），断言才可预期。

---

### Task 1: `replay_cash` — 纯算术重放累计现金流

**Files:**
- Create: `main/core/services/positions_backfill.py`
- Test: `main/core/tests/unit/test_positions_backfill.py`

**Interfaces:**
- Consumes: 无（首个任务）
- Produces: `replay_cash(trades, snap_dates) -> Dict[date, Decimal]`
  `trades` 为鸭子类型对象列表，需含 `.bar_time`(datetime) / `.trade_type`(str) / `.amount` / `.commission` / `.stamp_duty`；ORM 行与 `SimpleNamespace` 均可。返回 `{snap_date: 截至该日的累计净现金流}`，与初始资金无关。

- [ ] **Step 1: 写失败测试**

创建 `main/core/tests/unit/test_positions_backfill.py`：

```python
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.services.positions_backfill import replay_cash


def _t(bar_time, trade_type, amount, commission="0.00", stamp_duty="0.00",
       price="10.00", quantity=100, code="000001.SZ", strategy_id=1):
    """构造与 BacktestTrade 同形的鸭子类型成交。"""
    return SimpleNamespace(
        bar_time=bar_time, trade_type=trade_type, stock_code=code,
        strategy_id=strategy_id, price=Decimal(price), quantity=quantity,
        amount=Decimal(amount), commission=Decimal(commission),
        stamp_duty=Decimal(stamp_duty),
    )


def test_replay_cash_includes_same_day_trade():
    """快照含当日成交：snap_date 当日的成交必须计入该日累计。"""
    trades = [_t(datetime(2024, 1, 3, 10, 0), "BUY", "1000.00", "5.00")]
    out = replay_cash(trades, [date(2024, 1, 2), date(2024, 1, 3)])
    assert out[date(2024, 1, 2)] == Decimal("0")
    assert out[date(2024, 1, 3)] == Decimal("-1005.00")


def test_replay_cash_sell_deducts_fees():
    trades = [_t(datetime(2024, 1, 3, 10, 0), "SELL", "2000.00", "5.00", "2.00")]
    out = replay_cash(trades, [date(2024, 1, 3)])
    assert out[date(2024, 1, 3)] == Decimal("1993.00")


def test_replay_cash_accumulates_across_days():
    trades = [
        _t(datetime(2024, 1, 3, 10, 0), "BUY", "1000.00", "5.00"),
        _t(datetime(2024, 1, 5, 10, 0), "SELL", "1200.00", "5.00", "2.00"),
    ]
    out = replay_cash(trades, [date(2024, 1, 3), date(2024, 1, 4),
                               date(2024, 1, 5)])
    assert out[date(2024, 1, 3)] == Decimal("-1005.00")
    assert out[date(2024, 1, 4)] == Decimal("-1005.00")
    assert out[date(2024, 1, 5)] == Decimal("-1005.00") + Decimal("1193.00")


def test_replay_cash_rejects_unknown_trade_type():
    # 用真正未知的类型，而非小写 "buy"——_cash_flow 走 .upper() 归一化，
    # 小写是合法输入（与 live_engine.py:1139 / live_service.py:461 同策略）。
    trades = [_t(datetime(2024, 1, 3, 10, 0), "HOLD", "1000.00")]
    with pytest.raises(ValueError):
        replay_cash(trades, [date(2024, 1, 3)])


def test_replay_cash_accepts_lowercase_trade_type():
    """小写 trade_type 经 .upper() 归一化后正常处理，不抛错。"""
    trades = [_t(datetime(2024, 1, 3, 10, 0), "buy", "1000.00", "5.00")]
    out = replay_cash(trades, [date(2024, 1, 3)])
    assert out[date(2024, 1, 3)] == Decimal("-1005.00")


def test_replay_cash_without_trades_is_all_zero():
    out = replay_cash([], [date(2024, 1, 2), date(2024, 1, 3)])
    assert set(out.values()) == {Decimal("0")}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest core/tests/unit/test_positions_backfill.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.services.positions_backfill'`

- [ ] **Step 3: 写最小实现**

创建 `main/core/services/positions_backfill.py`：

```python
# -*- coding: utf-8 -*-
"""回填老回测记录的每日持仓明细（positions_json）。

背景：持仓明细落库功能在提交 b7bc21c 才落地，当前库 10 条记录的组合快照
positions_json 全为 NULL，导致行业归因（栏目八）对所有记录显示「需重新回测」。
数据足以重建——成交流水 + 存量市值 + 事后可取的前复权日线。

本模块四个单元，前三个是纯函数（零 I/O，可离线单测），只有 backfill_record
做 DB/TQ I/O：
    replay_cash       累计现金流重放（零漂移校验信号）
    replay_positions  持仓重放（复用引擎 Position 的 FIFO + T+1 分桶）
    value_positions   估值 + 按存量市值缩放（消除除权漂移）
    backfill_record   编排：读库 → 取价 → 调上述三步 → 校验 → 写库
"""
from datetime import date
from decimal import Decimal
from typing import Dict, List, Sequence, Tuple

from core.engine.event import TradeEvent
from core.engine.position import Position
from tq_iquant_shared.constants import TradeType


def _trade_day(trade) -> date:
    return trade.bar_time.date()


def _cash_flow(trade) -> Decimal:
    """单笔成交的现金影响。买入 -(amount+commission+stamp_duty)，卖出反向。"""
    amount = Decimal(str(trade.amount))
    commission = Decimal(str(trade.commission))
    stamp_duty = Decimal(str(trade.stamp_duty))
    kind = str(trade.trade_type).upper()
    if kind == "BUY":
        return -(amount + commission + stamp_duty)
    if kind == "SELL":
        return amount - commission - stamp_duty
    raise ValueError("未知 trade_type: %r" % (trade.trade_type,))


def replay_cash(trades, snap_dates: Sequence[date]) -> Dict[date, Decimal]:
    """重放截至各快照日的累计净现金流。

    与初始资金无关：校验恒等式为
    `stored_cash[i] - stored_cash[0] == cumflow[i] - cumflow[0]`
    （由 cash[i] = 初始现金 + cumflow[i] 两式相减消掉初始现金得到）。
    必须减 cumflow[0]——直接比 cumflow[i] 隐含「首条快照不含当日成交」，
    首日有成交时会误判为不自洽。
    快照含当日成交，故对 snap_date D 累加所有 bar_time.date() <= D 的成交。
    """
    flows = sorted(((_trade_day(t), _cash_flow(t)) for t in trades),
                   key=lambda x: x[0])
    out: Dict[date, Decimal] = {}
    total = Decimal("0")
    i = 0
    for d in sorted(snap_dates):
        while i < len(flows) and flows[i][0] <= d:
            total += flows[i][1]
            i += 1
        out[d] = total
    return out
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest core/tests/unit/test_positions_backfill.py -q`
Expected: PASS — `6 passed`

- [ ] **Step 5: 提交**

```bash
git add core/services/positions_backfill.py core/tests/unit/test_positions_backfill.py
git commit -m "feat(backfill): positions_backfill 模块骨架 + replay_cash 累计现金重放"
```

---

### Task 2: `replay_positions` — 复用引擎 Position 重放持仓

**Files:**
- Modify: `main/core/services/positions_backfill.py`
- Test: `main/core/tests/unit/test_positions_backfill.py`

**Interfaces:**
- Consumes: Task 1 的 `_trade_day`
- Produces: `replay_positions(trades, snap_dates) -> Dict[date, List[dict]]`
  `trades` 额外需要 `.id`（可缺省为 0）、`.strategy_id`、`.stock_code`、`.price`、`.quantity`。每条明细与 `positions_detail()` 同构，但 `market_value` 恒为字符串 `"0"`（本函数无价格输入，由 `value_positions` 填），`cost_value` 已填好。

- [ ] **Step 1: 写失败测试**

追加到 `main/core/tests/unit/test_positions_backfill.py`：

```python
from core.services.positions_backfill import replay_positions


def test_replay_positions_t1_blocks_same_day_sellable():
    """T+1：买入当日的份额在当日快照不可卖，次日才可卖。"""
    trades = [_t(datetime(2024, 1, 3, 10, 0), "BUY", "1000.00", quantity=100)]
    out = replay_positions(trades, [date(2024, 1, 3), date(2024, 1, 4)])
    d3 = out[date(2024, 1, 3)]
    assert d3[0]["quantity"] == 100
    assert d3[0]["sellable_quantity"] == 0
    assert out[date(2024, 1, 4)][0]["sellable_quantity"] == 100


def test_replay_positions_cost_value_uses_avg_cost():
    """cost_value = avg_cost × quantity，不扣佣金（与引擎 Position.buy 一致）。"""
    trades = [
        _t(datetime(2024, 1, 3, 10, 0), "BUY", "1000.00", price="10.00",
            quantity=100),
        _t(datetime(2024, 1, 4, 10, 0), "BUY", "1200.00", price="12.00",
            quantity=100),
    ]
    slot = replay_positions(trades, [date(2024, 1, 4)])[date(2024, 1, 4)][0]
    assert slot["quantity"] == 200
    assert slot["cost_value"] == "2200.00"  # 均价 11.00 × 200
    assert slot["market_value"] == "0"      # 估值由 value_positions 填


def test_replay_positions_sell_is_fifo_and_flattens():
    """卖出按最早买入桶 FIFO 扣减；清仓后当日快照为空仓。"""
    trades = [
        _t(datetime(2024, 1, 2, 10, 0), "BUY", "1000.00", price="10.00",
            quantity=100),
        _t(datetime(2024, 1, 3, 10, 0), "BUY", "1000.00", price="10.00",
            quantity=100),
        _t(datetime(2024, 1, 4, 10, 0), "SELL", "1500.00", price="15.00",
            quantity=150),
        _t(datetime(2024, 1, 5, 10, 0), "SELL", "750.00", price="15.00",
            quantity=50),
    ]
    out = replay_positions(trades, [date(2024, 1, 3), date(2024, 1, 4),
                                    date(2024, 1, 5)])
    assert out[date(2024, 1, 3)][0]["quantity"] == 200
    # 150 股卖光 1/2 两桶（各 100），剩 50 股仍来自 1/3 桶
    # → 1/3 相对 1/4 已过 T+1，故可卖 50（与 portfolio.py:488 同一调用）
    held = out[date(2024, 1, 4)][0]
    assert held["quantity"] == 50
    assert held["sellable_quantity"] == 50
    assert out[date(2024, 1, 5)] == []


def test_replay_positions_aggregates_across_strategies():
    """同一只股票被两个策略持有 → 组合层按 stock_code 聚合，均价各自独立。"""
    trades = [
        _t(datetime(2024, 1, 2, 10, 0), "BUY", "1000.00", price="10.00",
            quantity=100, strategy_id=1),
        _t(datetime(2024, 1, 2, 10, 0), "BUY", "2000.00", price="20.00",
            quantity=100, strategy_id=2),
    ]
    out = replay_positions(trades, [date(2024, 1, 3)])
    assert len(out[date(2024, 1, 3)]) == 1
    slot = out[date(2024, 1, 3)][0]
    assert slot["quantity"] == 200
    assert slot["sellable_quantity"] == 200
    assert slot["cost_value"] == "3000.00"  # 10.00×100 + 20.00×100


def test_replay_positions_sorted_by_code_and_empty_before_first_trade():
    trades = [
        _t(datetime(2024, 1, 3, 10, 0), "BUY", "1000.00", price="10.00",
            quantity=100, code="600000.SH"),
        _t(datetime(2024, 1, 3, 10, 0), "BUY", "500.00", price="5.00",
            quantity=100, code="000001.SZ"),
    ]
    out = replay_positions(trades, [date(2024, 1, 2), date(2024, 1, 3)])
    assert out[date(2024, 1, 2)] == []
    assert [s["stock_code"] for s in out[date(2024, 1, 3)]] == [
        "000001.SZ", "600000.SH"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest core/tests/unit/test_positions_backfill.py -q`
Expected: FAIL — `ImportError: cannot import name 'replay_positions'`

- [ ] **Step 3: 写最小实现**

在 `main/core/services/positions_backfill.py` 的 `replay_cash` 之后追加：

```python
def _to_trade_event(trade) -> TradeEvent:
    """ORM 行 / SimpleNamespace → 引擎 TradeEvent（Position.apply_trade 的入参）。"""
    return TradeEvent(
        strategy_id=trade.strategy_id, portfolio_id=0,
        stock_code=trade.stock_code,
        trade_type=TradeType(str(trade.trade_type).upper()),
        price=Decimal(str(trade.price)), quantity=trade.quantity,
        amount=Decimal(str(trade.amount)),
        commission=Decimal(str(trade.commission)),
        stamp_duty=Decimal(str(trade.stamp_duty)),
        trade_time=trade.bar_time, signal_type=None,
    )


def _aggregate(positions: Dict[Tuple[int, str], Position],
               snap_date: date) -> List[dict]:
    """策略层持仓 → 组合层按 stock_code 聚合，输出 positions_detail 同构结构。

    刻意复用引擎 Position.available_shares_on，使 T+1 分桶与 FIFO 扣减语义
    与原始回测 100% 一致，不重写一套（重写必然随引擎演进而漂移）。
    """
    agg: Dict[str, dict] = {}
    for pos in positions.values():
        if pos.quantity == 0:
            continue
        slot = agg.setdefault(pos.stock_code, {
            "stock_code": pos.stock_code, "quantity": 0,
            "sellable_quantity": 0, "cost_value": Decimal("0"),
        })
        slot["quantity"] += pos.quantity
        slot["sellable_quantity"] += pos.available_shares_on(snap_date)
        slot["cost_value"] += pos.avg_cost * pos.quantity
    return [
        {"stock_code": agg[code]["stock_code"],
         "quantity": agg[code]["quantity"],
         "sellable_quantity": agg[code]["sellable_quantity"],
         "market_value": "0",
         "cost_value": str(agg[code]["cost_value"])}
        for code in sorted(agg)
    ]


def replay_positions(trades, snap_dates: Sequence[date]) -> Dict[date, List[dict]]:
    """按 (strategy_id, stock_code) 重放持仓，返回 {snap_date: 持仓明细列表}。

    持仓按策略分桶（与引擎一致：策略层持仓独立于组合层），组合层再聚合。
    快照含当日成交，故对 snap_date D 先应用 bar_time.date() <= D 的全部成交。
    market_value 恒为 "0"——本函数无价格输入，由 value_positions 填充。
    """
    positions: Dict[Tuple[int, str], Position] = {}
    for t in trades:
        positions.setdefault((t.strategy_id, t.stock_code),
                             Position(t.stock_code))
    ordered = sorted(trades,
                     key=lambda t: (t.bar_time, getattr(t, "id", 0) or 0))
    out: Dict[date, List[dict]] = {}
    i = 0
    for d in sorted(snap_dates):
        while i < len(ordered) and _trade_day(ordered[i]) <= d:
            t = ordered[i]
            positions[(t.strategy_id, t.stock_code)].apply_trade(
                _to_trade_event(t))
            i += 1
        out[d] = _aggregate(positions, d)
    return out
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest core/tests/unit/test_positions_backfill.py -q`
Expected: PASS — `11 passed`

- [ ] **Step 5: 提交**

```bash
git add core/services/positions_backfill.py core/tests/unit/test_positions_backfill.py
git commit -m "feat(backfill): replay_positions 复用引擎 Position 重放持仓（T+1/FIFO 语义一致）"
```

---

### Task 3: `value_positions` — 估值 + 按存量市值缩放

**Files:**
- Modify: `main/core/services/positions_backfill.py`
- Test: `main/core/tests/unit/test_positions_backfill.py`

**Interfaces:**
- Consumes: Task 2 的明细结构
- Produces:
  ```python
  value_positions(detail, close, snap_date, stored_market_value)
      -> Tuple[List[dict], Dict[str, object]]
  ```
  `close: Dict[code, Dict[date, Decimal]]`。返回新 list（不改入参）+ notes，notes 键固定为
  `carried`(int) / `cost_fallback`(int) / `no_price`(List[str]) / `k`(Decimal|None) / `unscaled`(bool)。

- [ ] **Step 1: 写失败测试**

追加到 `main/core/tests/unit/test_positions_backfill.py`：

```python
from core.services.positions_backfill import value_positions


def _slot(code="000001.SZ", qty=100, sellable=100, cost="1000.00"):
    return {"stock_code": code, "quantity": qty, "sellable_quantity": sellable,
            "market_value": "0", "cost_value": cost}


def test_value_positions_uses_exact_date_price():
    d = date(2024, 1, 3)
    out, notes = value_positions(
        [_slot()], {"000001.SZ": {d: Decimal("10")}}, d, Decimal("1000"))
    assert out[0]["market_value"] == "1000.00"   # 量化到 2 位
    assert notes["carried"] == 0
    assert notes["cost_fallback"] == 0
    assert notes["k"] == Decimal("1")
    assert notes["unscaled"] is False


def test_value_positions_carries_forward_last_price():
    """停牌/缺 bar → 结转该股最近一个已知收盘价。"""
    d = date(2024, 1, 4)
    out, notes = value_positions(
        [_slot()],
        {"000001.SZ": {date(2024, 1, 2): Decimal("10"),
                       date(2024, 1, 3): Decimal("11")}},
        d, Decimal("1100"))
    assert out[0]["market_value"] == "1100.00"   # 结转 1/3 的 11 × 100
    assert notes["carried"] == 1
    assert notes["k"] == Decimal("1")


def test_value_positions_falls_back_to_avg_cost_when_no_price_at_all():
    """该股全程无价 → 用 avg_cost(=cost_value/quantity) 兜底，与 positions_detail 一致。"""
    d = date(2024, 1, 3)
    out, notes = value_positions([_slot()], {}, d, Decimal("1000"))
    assert out[0]["market_value"] == "1000.00"
    assert notes["cost_fallback"] == 1
    assert notes["no_price"] == ["000001.SZ"]


def test_value_positions_scales_to_stored_market_value():
    """事后前复权价有除权漂移 → 缩放到快照存量市值，使行业权重之和为 100%。"""
    d = date(2024, 1, 3)
    detail = [_slot("000001.SZ", 100, cost="1000.00"),
              _slot("600000.SH", 100, cost="2000.00")]
    close = {"000001.SZ": {d: Decimal("9")}, "600000.SH": {d: Decimal("18")}}
    out, notes = value_positions(detail, close, d, Decimal("3000"))
    # 重算 raw = 900 + 1800 = 2700 → k = 3000/2700（无限循环小数）
    assert notes["k"] == Decimal("3000") / Decimal("2700")
    # 量化到 2 位后恰好回到存量市值
    assert [s["market_value"] for s in out] == ["1000.00", "2000.00"]
    assert sum(Decimal(s["market_value"]) for s in out) == Decimal("3000.00")


def test_value_positions_does_not_scale_when_stored_missing():
    d = date(2024, 1, 3)
    out, notes = value_positions(
        [_slot()], {"000001.SZ": {d: Decimal("10")}}, d, None)
    assert out[0]["market_value"] == "1000.00"
    assert notes["k"] is None
    assert notes["unscaled"] is True


def test_value_positions_does_not_mutate_input():
    d = date(2024, 1, 3)
    detail = [_slot()]
    value_positions(detail, {"000001.SZ": {d: Decimal("10")}}, d,
                    Decimal("1000"))
    assert detail[0]["market_value"] == "0"


def test_value_positions_empty_detail_is_noop():
    d = date(2024, 1, 3)
    out, notes = value_positions([], {}, d, Decimal("0"))
    assert out == []
    assert notes["k"] is None
    assert notes["carried"] == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest core/tests/unit/test_positions_backfill.py -q`
Expected: FAIL — `ImportError: cannot import name 'value_positions'`

- [ ] **Step 3: 写最小实现**

在 `main/core/services/positions_backfill.py` 末尾追加：

```python
_CENT = Decimal("0.01")


def _last_price(series: Dict[date, Decimal], snap_date: date):
    """该股在 snap_date（含）之前的最近一个已知收盘价；无则 None。"""
    best = None
    for d in series:
        if d <= snap_date and (best is None or d > best):
            best = d
    return None if best is None else series[best]


def value_positions(detail: List[dict], close: Dict[str, Dict[date, Decimal]],
                    snap_date: date,
                    stored_market_value) -> Tuple[List[dict], dict]:
    """给持仓明细填 market_value，返回 (新明细, notes)。不改入参。

    缺价策略：当天无 bar → 结转该股最近一个已知价；该股全程无价 → 回退
    avg_cost(=cost_value/quantity)，与 Portfolio.positions_detail 的
    `price is None or price <= 0 → price = pos.avg_cost` 口径一致。

    缩放：事后取到的是今日前复权价，与回测当时存在除权漂移。若直接写入，
    行业权重（market_value/total_value）之和会偏离 1。按 stored_market_value
    缩放后 Σ 市值精确回到回测自身存量市值，权重之和为 100%。
    k 是无限循环小数，故落库前量化到分——与库中 Numeric(15,2) 列一致。
    cost_value 不缩放：它没有对应的存量真值，且不参与行业归因。
    """
    notes = {"carried": 0, "cost_fallback": 0, "no_price": [],
             "k": None, "unscaled": False}
    if not detail:
        return [], notes
    out: List[dict] = []
    raw_sum = Decimal("0")
    for slot in detail:
        qty = slot["quantity"]
        series = close.get(slot["stock_code"]) or {}
        price = series.get(snap_date)
        if price is None:
            carried = _last_price(series, snap_date)
            if carried is not None:
                price = carried
                notes["carried"] += 1
            else:
                price = ((Decimal(slot["cost_value"]) / qty) if qty
                         else Decimal("0"))
                notes["cost_fallback"] += 1
                if not series:
                    notes["no_price"].append(slot["stock_code"])
        market_value = price * qty
        raw_sum += market_value
        out.append(dict(slot, market_value=str(market_value.quantize(_CENT))))
    if raw_sum <= 0:
        return out, notes
    if not stored_market_value:
        # 有持仓却无存量市值：跳过缩放并告警，不阻断
        notes["unscaled"] = True
        return out, notes
    k = Decimal(str(stored_market_value)) / raw_sum
    notes["k"] = k
    return [dict(s, market_value=str((Decimal(s["market_value"]) * k)
                                    .quantize(_CENT))) for s in out], notes
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest core/tests/unit/test_positions_backfill.py -q`
Expected: PASS — `17 passed`

- [ ] **Step 5: 提交**

```bash
git add core/services/positions_backfill.py core/tests/unit/test_positions_backfill.py
git commit -m "feat(backfill): value_positions 估值(结转/avg_cost兜底)+按存量市值缩放并量化"
```

---

### Task 4: `backfill_record` — 编排层（唯一做 I/O 的单元）

**Files:**
- Modify: `main/core/services/positions_backfill.py`
- Test: `main/core/tests/unit/test_positions_backfill.py`

**Interfaces:**
- Consumes: `replay_cash`、`replay_positions`、`value_positions`（Task 1-3）、`core.services.backtest_service.build_index_close_series`
- Produces:
  ```python
  backfill_record(db, record_id, force=False, close_provider=None) -> dict
  ```
  `close_provider(codes: List[str], start: date, end: date) -> Dict[code, Dict[date, Decimal]]`，
  缺省用 `build_index_close_series(codes, start, end, batch_size=32)`。

  返回 `{"status": "ok"|"skipped"|"failed", "record_id", "snapshots", "held_codes",
  "missing_price_codes": [...], "cash_check": {"ok", "mismatches", "first_mismatch"},
  "scale": {"max_dev", "max_dev_date", "mean_dev", "no_scale_days"},
  "carry_forward_days", "avg_cost_fallback", "written", "error"}`。
  `record_id` 不存在 / 无快照时只返回 `{"status", "error", "record_id"}`。

- [ ] **Step 1: 写失败测试**

追加到 `main/core/tests/unit/test_positions_backfill.py`：

```python
import json
from datetime import datetime

from core.models import (BacktestDailySnapshot, BacktestRecord, BacktestTrade,
                         Formula, PortfolioStrategy, StockPool, StockPoolStock,
                         Strategy)
from core.services.positions_backfill import backfill_record


def _seed_record(db):
    pool = StockPool(code="BF", name="bf_pool")
    db.add(pool)
    db.flush()
    db.add(StockPoolStock(pool_id=pool.id, stock_code="000001.SZ",
                          stock_name="平安银行"))
    formula = Formula(name="f", content="REF(CLOSE,1)")
    db.add(formula)
    db.flush()
    ps = PortfolioStrategy(name="ps", stock_pool_id=pool.id,
                           initial_capital=Decimal("10000"),
                           max_drawdown=Decimal("0.2"),
                           daily_loss_limit=Decimal("0.05"))
    db.add(ps)
    db.flush()
    strat = Strategy(portfolio_id=ps.id, name="s1", formula_id=formula.id,
                     period="1d", role="master", capital_ratio=Decimal("1"),
                     max_positions=5, stop_loss_ratio=Decimal("0.05"),
                     take_profit_ratio=Decimal("0.2"),
                     trailing_stop_ratio=Decimal("0"))
    db.add(strat)
    db.flush()
    rec = BacktestRecord(portfolio_strategy_id=ps.id, name="bf",
                         start_date=date(2024, 1, 2), end_date=date(2024, 1, 5),
                         status="completed", progress=100)
    db.add(rec)
    db.commit()
    return rec, ps, strat


def _snap(db, rec, d, cash, market_value):
    db.add(BacktestDailySnapshot(
        backtest_record_id=rec.id, target_type="portfolio",
        target_id=rec.portfolio_strategy_id, snap_date=d,
        total_value=Decimal(str(cash)) + Decimal(str(market_value)),
        cash=Decimal(str(cash)), market_value=Decimal(str(market_value))))


def _trade(db, rec, strat, bar_time, kind, price, quantity, amount,
           commission="0.00", stamp_duty="0.00"):
    db.add(BacktestTrade(
        backtest_record_id=rec.id, strategy_id=strat.id,
        signal_name="open_sig", signal_type="OPEN", stock_code="000001.SZ",
        trade_type=kind, price=Decimal(price), quantity=quantity,
        amount=Decimal(amount), commission=Decimal(commission),
        stamp_duty=Decimal(stamp_duty), bar_time=bar_time))


D1, D2, D3, D4 = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4),
                  date(2024, 1, 5))


def _seed_backfillable(db):
    """4 天现金自洽：1/2 空仓 10000 → 1/3 买 100@10 → 1/4 加仓 100@12 → 1/5 持有不动。"""
    rec, _ps, strat = _seed_record(db)
    _snap(db, rec, D1, "10000.00", "0.00")
    _snap(db, rec, D2, "9000.00", "1000.00")
    _snap(db, rec, D3, "7800.00", "2200.00")
    _snap(db, rec, D4, "7800.00", "2200.00")
    _trade(db, rec, strat, datetime(2024, 1, 3, 15, 0), "BUY", "10.00", 100,
           "1000.00")
    _trade(db, rec, strat, datetime(2024, 1, 4, 15, 0), "BUY", "12.00", 100,
           "1200.00")
    db.commit()
    return rec


def _close_two_days():
    """价格 9/11 相对成本 10/12 有 -10% 漂移 → 缩放后 Σ 仍等于存量市值。"""
    return {"000001.SZ": {D2: Decimal("9"), D3: Decimal("11")}}


def test_backfill_record_writes_positions_json(db_session):
    rec = _seed_backfillable(db_session)
    out = backfill_record(db_session, rec.id, close_provider=_close_two_days)
    assert out["status"] == "ok"
    assert out["written"] is True
    assert out["cash_check"] == {"ok": True, "mismatches": 0,
                                 "first_mismatch": None}
    snaps = (db_session.query(BacktestDailySnapshot)
             .filter_by(backtest_record_id=rec.id, target_type="portfolio")
             .order_by(BacktestDailySnapshot.snap_date).all())
    assert [json.loads(s.positions_json) for s in snaps] == [
        [],
        [{"stock_code": "000001.SZ", "quantity": 100, "sellable_quantity": 0,
          "market_value": "1000.00", "cost_value": "1000.00"}],
        [{"stock_code": "000001.SZ", "quantity": 200, "sellable_quantity": 100,
          "market_value": "2200.00", "cost_value": "2200.00"}],
        [{"stock_code": "000001.SZ", "quantity": 200, "sellable_quantity": 200,
          "market_value": "2200.00", "cost_value": "2200.00"}],
    ]


def test_backfill_record_uses_carried_forward_price(db_session):
    """1/5 无 bar → 结转 1/4 的 11；首日空仓不参与缩放。"""
    rec = _seed_backfillable(db_session)
    out = backfill_record(db_session, rec.id, close_provider=_close_two_days)
    assert out["carry_forward_days"] == 1
    assert out["scale"]["no_scale_days"] == 1
    assert out["scale"]["max_dev"][:6] == "0.1111"  # 1/3 日 k = 1000/900


def test_backfill_record_reports_missing_price_codes(db_session):
    rec = _seed_backfillable(db_session)
    out = backfill_record(db_session, rec.id, close_provider=lambda *a: {})
    assert out["status"] == "ok"
    assert out["missing_price_codes"] == ["000001.SZ"]
    assert out["avg_cost_fallback"] == 3   # 1/2、1/3、1/5 三个有持仓的快照日


def test_backfill_record_rejects_scale_deviation_over_five_percent(db_session):
    """重建市值与存量偏离 >5% → 拒绝写入，DB 一行不改。"""
    rec = _seed_backfillable(db_session)

    def _drifted(codes, start, end):
        return {"000001.SZ": {D2: Decimal("5"), D3: Decimal("5.5")}}

    out = backfill_record(db_session, rec.id, close_provider=_drifted)
    assert out["status"] == "skipped"
    assert out["written"] is False
    assert Decimal(out["scale"]["max_dev"]) > Decimal("0.05")
    rows = (db_session.query(BacktestDailySnapshot)
            .filter_by(backtest_record_id=rec.id).all())
    assert all(s.positions_json is None for s in rows)


def test_backfill_record_force_overrides_scale_gate(db_session):
    rec = _seed_backfillable(db_session)

    def _drifted(codes, start, end):
        return {"000001.SZ": {D2: Decimal("5"), D3: Decimal("5.5")}}

    out = backfill_record(db_session, rec.id, force=True,
                          close_provider=_drifted)
    assert out["status"] == "ok"
    assert out["written"] is True


def test_backfill_record_accepts_trade_on_first_snapshot_day(db_session):
    """首条快照当日已含成交时仍须通过——差分校验减 cumflow[首日] 而非直接比 cumflow。"""
    rec, _ps, strat = _seed_record(db_session)
    _snap(db_session, rec, D1, "9000.00", "1000.00")
    _snap(db_session, rec, D2, "9000.00", "1000.00")
    _trade(db_session, rec, strat, datetime(2024, 1, 2, 15, 0), "BUY", "10.00",
           100, "1000.00")
    db_session.commit()
    out = backfill_record(db_session, rec.id, close_provider=_close_two_days)
    assert out["cash_check"]["ok"] is True
    assert out["status"] == "ok"


def test_backfill_record_fails_on_cash_mismatch(db_session):
    """现金差分校验不通过 → 整条记录失败，一行不写（零漂移硬断言）。"""
    rec, _ps, strat = _seed_record(db_session)
    _snap(db_session, rec, D1, "1000.00", "0.00")
    _snap(db_session, rec, D2, "-900.00", "1000.00")   # 与 -1000 的现金流不自洽
    _trade(db_session, rec, strat, datetime(2024, 1, 3, 15, 0), "BUY", "10.00",
           100, "1000.00")
    db_session.commit()
    out = backfill_record(db_session, rec.id, close_provider=_close_two_days)
    assert out["status"] == "failed"
    assert out["cash_check"]["ok"] is False
    assert out["cash_check"]["mismatches"] == 1
    assert out["cash_check"]["first_mismatch"] == "2024-01-03"
    rows = (db_session.query(BacktestDailySnapshot)
            .filter_by(backtest_record_id=rec.id).all())
    assert all(s.positions_json is None for s in rows)


def test_backfill_record_missing_record_returns_failed(db_session):
    out = backfill_record(db_session, 9999, close_provider=_close_two_days)
    assert out["status"] == "failed"
    assert out["error"] == "回测记录不存在"


def test_backfill_record_without_snapshots_returns_failed(db_session):
    rec, _ps, _strat = _seed_record(db_session)
    out = backfill_record(db_session, rec.id, close_provider=_close_two_days)
    assert out["status"] == "failed"
    assert "无组合日快照" in out["error"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest core/tests/unit/test_positions_backfill.py -q`
Expected: FAIL — `ImportError: cannot import name 'backfill_record'`

- [ ] **Step 3: 写最小实现**

在 `main/core/services/positions_backfill.py` 顶部 import 区补充：

```python
import json
from sqlalchemy.orm import Session

from core.models import BacktestDailySnapshot, BacktestRecord, BacktestTrade
from core.services.backtest_service import build_index_close_series

SCALE_TOLERANCE = Decimal("0.05")
```

并在文件末尾追加：

```python
def _deviation_stats(k_by_date: Dict[date, Decimal]) -> dict:
    """缩放系数偏离度统计。k_by_date 只含成功缩放的日子。"""
    if not k_by_date:
        return {"max_dev": "0", "max_dev_date": None, "mean_dev": "0"}
    devs = {d: abs(k - 1) for d, k in k_by_date.items()}
    worst = max(devs, key=lambda d: devs[d])
    mean = sum(devs.values(), Decimal("0")) / Decimal(len(devs))
    return {"max_dev": str(devs[worst]), "max_dev_date": worst.isoformat(),
            "mean_dev": str(mean)}


def _default_close_provider(codes: List[str], start: date, end: date):
    # batch_size=32：回填只涉及个股，代码数少；分批以免单次请求过大
    return build_index_close_series(codes, start, end, batch_size=32)


def backfill_record(db: Session, record_id: int, force: bool = False,
                    close_provider=None) -> dict:
    """回填一条记录的组合快照 positions_json。返回对账 dict（不抛异常）。

    流程：读组合快照 + 成交 → 现金差分硬校验 → 重放持仓（同时确定需取价的代码）
    → 取日线收盘价 → 逐日估值缩放 → 门限判定 → 单次 commit 写入。
    任一步失败则一行不写（原子性），返回 status=failed/skipped 及原因。
    """
    provider = close_provider or _default_close_provider
    rec = db.get(BacktestRecord, record_id)
    if rec is None:
        return {"status": "failed", "record_id": record_id,
                "error": "回测记录不存在"}
    snaps = (db.query(BacktestDailySnapshot)
             .filter_by(backtest_record_id=record_id, target_type="portfolio")
             .order_by(BacktestDailySnapshot.snap_date).all())
    if not snaps:
        return {"status": "failed", "record_id": record_id,
                "error": "该记录无组合日快照，无法回填"}
    trades = (db.query(BacktestTrade)
              .filter_by(backtest_record_id=record_id)
              .order_by(BacktestTrade.bar_time, BacktestTrade.id).all())
    dates = [s.snap_date for s in snaps]

    # 1) 现金差分硬校验：与初始资金无关，零漂移，能抓住任何数量/时序错误。
    #    减 cumflow[首日] 而非直接比 cumflow：首条快照若已含当日成交，
    #    后者会误判。恒等式为 stored[i] - stored[0] == cumflow[i] - cumflow[0]
    #    （因 stored[i] = 初始现金 + cumflow[i]，两式相减消掉初始现金）。
    cum = replay_cash(trades, dates)
    base_cash = Decimal(str(snaps[0].cash))
    first_cum = cum[dates[0]]
    mismatches = 0
    first_mismatch = None
    for s in snaps:
        stored = Decimal(str(s.cash)) - base_cash
        if abs(stored - (cum[s.snap_date] - first_cum)) > Decimal("0.01"):
            mismatches += 1
            if first_mismatch is None:
                first_mismatch = s.snap_date.isoformat()
    cash_check = {"ok": mismatches == 0, "mismatches": mismatches,
                  "first_mismatch": first_mismatch}
    if mismatches:
        return {"status": "failed", "record_id": record_id,
                "error": "现金重放与库中 cash 不自洽，拒绝写入",
                "cash_check": cash_check}

    # 2) 重放持仓（无 I/O），顺带确定实际持仓过的代码——只给这些代码取价
    replayed = replay_positions(trades, dates)
    held_codes = sorted({slot["stock_code"] for day in replayed.values()
                         for slot in day})
    close = (provider(held_codes, snaps[0].snap_date, snaps[-1].snap_date)
             if held_codes else {})
    missing = [c for c in held_codes if c not in (close or {})]

    # 3) 逐日估值 + 缩放
    k_by_date: Dict[date, Decimal] = {}
    no_scale_days = 0
    carry = 0
    fallback = 0
    payloads = []
    for s in snaps:
        detail, notes = value_positions(replayed[s.snap_date], close or {},
                                        s.snap_date, s.market_value)
        carry += notes["carried"]
        fallback += notes["cost_fallback"]
        if notes["k"] is not None:
            k_by_date[s.snap_date] = notes["k"]
        else:
            no_scale_days += 1
        payloads.append(json.dumps(detail, default=str))

    scale = _deviation_stats(k_by_date)
    scale["no_scale_days"] = no_scale_days
    report = {"status": "ok", "record_id": record_id, "snapshots": len(snaps),
              "held_codes": len(held_codes), "missing_price_codes": missing,
              "cash_check": cash_check, "scale": scale,
              "carry_forward_days": carry, "avg_cost_fallback": fallback,
              "written": False, "error": None}
    if Decimal(scale["max_dev"]) > SCALE_TOLERANCE and not force:
        report["status"] = "skipped"
        report["error"] = (
            "重建市值与存量偏离 %.2f%% 超过 %.0f%% 门限，拒绝写入"
            "（确认无误可用 force 覆盖）"
            % (float(scale["max_dev"]) * 100, float(SCALE_TOLERANCE) * 100))
        return report

    # 4) 单次 commit：不允许部分写入
    for s, payload in zip(snaps, payloads):
        s.positions_json = payload
    db.commit()
    report["written"] = True
    return report
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest core/tests/unit/test_positions_backfill.py -q`
Expected: PASS — `27 passed`

- [ ] **Step 5: 提交**

```bash
git add core/services/positions_backfill.py core/tests/unit/test_positions_backfill.py
git commit -m "feat(backfill): backfill_record 编排层（现金硬校验+缩放门限+单次提交）"
```

---

### Task 5: CLI 脚本 `backfill_positions_json.py`

**Files:**
- Create: `main/scripts/backfill_positions_json.py`
- Test: 无单测——一次性运维脚本，全部逻辑在 Task 1-4 已测过的 service 层

**Interfaces:**
- Consumes: `backfill_record`（Task 4）、`recompute_market_regime`（`core.services.backtest_service`）
- Produces: 命令行工具，无 Python API

- [ ] **Step 1: 写脚本**

创建 `main/scripts/backfill_positions_json.py`：

```python
# -*- coding: utf-8 -*-
"""回填老回测记录的每日持仓明细（positions_json），并可选重算市场态分析。

背景
----
持仓明细落库功能在提交 b7bc21c（2026-09-27）才落地，当前库中所有回测记录的组合
快照 positions_json 均为 NULL（早于该功能），导致「机遇Alpha · 中性Alpha」栏目
第八节（行业归因）对所有记录显示「需重新回测」。重跑长周期回测代价过高，而
成交流水 + 存量市值足以重建持仓明细。

判定标准
--------
[OK]   status=ok 且 written=True：现金重放逐日与库中 cash 自洽（零漂移硬校验），
       且缩放系数 |k-1| <= 5%
[SKIP] status=skipped：|k-1| > 5%，重建市值与存量偏离过大，未写库
       （事后前复权价与回测当时有除权漂移；确认无误可用 --force 覆盖）
[FAIL] status=failed：现金不自洽（持仓重放的数量或时序有误），一行未写

默认 dry-run：只打印对账表，不写库。--apply 才真正写入。

用法
----
    cd main
    uv run python scripts/backfill_positions_json.py --all-including-done
    uv run python scripts/backfill_positions_json.py --record 11
    uv run python scripts/backfill_positions_json.py --all-including-done --apply
    uv run python scripts/backfill_positions_json.py --all-including-done --apply --recompute
    uv run python scripts/backfill_positions_json.py --all-including-done --apply --force

注意：脚本需 import core.*，故自行把 main/ 插入 sys.path（main/scripts/ 下
其余脚本不 import core.*，无此先例）。取价需通达信客户端在运行。
"""
import argparse
import sys
from pathlib import Path

_MAIN = Path(__file__).resolve().parent.parent
if str(_MAIN) not in sys.path:
    sys.path.insert(0, str(_MAIN))

from core.db import SessionLocal                                  # noqa: E402
from core.models import (BacktestMarketRegimeAnalysis,            # noqa: E402
                         BacktestRecord)
from core.services.backtest_service import recompute_market_regime  # noqa: E402
from core.services.positions_backfill import backfill_record        # noqa: E402

_TAG = {"ok": "[OK]", "skipped": "[SKIP]", "failed": "[FAIL]"}


def _section(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def _print_report(r):
    if "cash_check" not in r:
        print("[FAIL] record %s: %s" % (r.get("record_id"), r.get("error")))
        return
    cc, sc = r["cash_check"], r["scale"]
    print("%s record %s  快照 %s  持仓代码 %s  已写入 %s"
          % (_TAG[r["status"]], r["record_id"], r["snapshots"],
             r["held_codes"], r["written"]))
    print("       现金校验 ok=%s 不符 %s 日%s"
          % (cc["ok"], cc["mismatches"],
             "" if cc["first_mismatch"] is None
             else "（首个 %s）" % cc["first_mismatch"]))
    print("       缩放 |k-1| 均值 %s 最大 %s（%s）未缩放日 %s"
          % (sc["mean_dev"][:8], sc["max_dev"][:8], sc["max_dev_date"],
             sc["no_scale_days"]))
    print("       结转 %s 日  avg_cost 兜底 %s 次  缺价代码 %s"
          % (r["carry_forward_days"], r["avg_cost_fallback"],
             r["missing_price_codes"] or "无"))
    if r.get("error"):
        print("       说明：%s" % r["error"])


def _target_ids(db, args):
    q = db.query(BacktestRecord).order_by(BacktestRecord.id)
    if args.record:
        return [r.id for r in q.filter(BacktestRecord.id == args.record).all()]
    ids = [r.id for r in q.all()]
    if not args.all_including_done:
        done = {row.backtest_record_id
                for row in db.query(BacktestMarketRegimeAnalysis).all()}
        ids = [i for i in ids if i not in done]
    return ids


def main():
    p = argparse.ArgumentParser(
        description="回填老回测记录的 positions_json（默认 dry-run）")
    p.add_argument("--record", type=int, help="只处理指定记录 id")
    p.add_argument("--all", action="store_true",
                   help="处理所有尚无市场态分析结果的记录")
    p.add_argument("--all-including-done", action="store_true",
                   help="处理所有记录（含已有市场态结果的）")
    p.add_argument("--apply", action="store_true",
                   help="真正写库（缺省只打印对账表）")
    p.add_argument("--force", action="store_true",
                   help="缩放偏离超 5%% 时仍写入")
    p.add_argument("--recompute", action="store_true",
                   help="回填后逐条重算市场态分析（需通达信在运行，较慢）")
    args = p.parse_args()
    if not (args.record or args.all or args.all_including_done):
        p.print_help()
        return 2

    db = SessionLocal()
    try:
        ids = _target_ids(db, args)
        if not ids:
            print("没有符合条件的记录")
            return 0
        _section("目标记录：%s" % ids)
        reports = []
        for rid in ids:
            rec = db.get(BacktestRecord, rid)
            _section("record %s  %s ~ %s" % (rid, rec.start_date, rec.end_date))
            r = backfill_record(db, rid, force=args.force)
            _print_report(r)
            reports.append((rid, r))

        _section("汇总")
        for status in ("ok", "skipped", "failed"):
            hit = [rid for rid, r in reports if r["status"] == status]
            print("%-8s %s" % (status, hit or "无"))
        if not args.apply:
            print("\n[dry-run] 未写库。确认无误后加 --apply 重跑。")
            return 0

        if args.recompute:
            _section("重算市场态分析（栏目八 行业归因）")
            for rid, r in reports:
                if r["status"] != "ok" or not r["written"]:
                    print("[SKIP] record %s 未写入，跳过重算" % rid)
                    continue
                out = recompute_market_regime(db, rid)
                status = (out or {}).get("status")
                print("[%s] record %s 市场态重算：%s %s"
                      % ("OK" if status == "completed" else "FAIL", rid,
                         status, (out or {}).get("error") or ""))
        return 0 if all(r["status"] == "ok" for _, r in reports) else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 手工验证 dry-run 不写库**

Run: `uv run python scripts/backfill_positions_json.py --record 11`
Expected: 打印 record 11 的对账表，末行 `[dry-run] 未写库。确认无误后加 --apply 重跑。`，退出码 0。

再确认库中 `positions_json` 仍全为 NULL：

Run: `uv run python -c "import sqlite3;print(sqlite3.connect('data/dev.db').execute('SELECT COUNT(*) FROM backtest_daily_snapshots WHERE positions_json IS NOT NULL').fetchone())"`
Expected: `(0,)`

- [ ] **Step 3: 提交**

```bash
git add scripts/backfill_positions_json.py
git commit -m "feat(backfill): backfill_positions_json 一次性 CLI（默认 dry-run，可选重算市场态）"
```

---

### Task 6: 集成测试 — 回填让行业归因越过双门禁

**Files:**
- Modify: `main/core/tests/unit/test_backtest_service.py`（追加到市场态重算测试区，约 `:953` 之后）
- Test: 同上（复用该文件既有的 `db_session` / `_seed_record` / `_FakeIndexTQ` / `_ca_daily_points` / `_CA_WEEKS_B` 辅助）

**Interfaces:**
- Consumes: `backfill_record`（Task 4）、`svc.recompute_market_regime`
- Produces: 无新 API

> 本任务**不走 RED**：被测行为在 Task 4 已实现，本任务是端到端验收。Task 1-4 的
> TDD 循环已覆盖全部新代码路径。若此测试失败，说明 Task 4 的编排层与真实
> recompute 链路之间有集成缺陷——修实现，不要放宽断言。

- [ ] **Step 1: 写测试**

在 `test_recompute_market_regime_upserts_and_detail_embeds` 之后插入：

```python
def test_backfilled_positions_unblock_industry_attribution(db_session, monkeypatch):
    """回填 positions_json 后，recompute_market_regime 的行业归因不再 needs_rerun。

    这是整个回填功能的端到端验收：双门禁
    （backtest_service._build_industry_attribution 与
    industry_attribution.compute_industry_attribution）都以
    `any(positions is not None)` 为条件。
    """
    from core.services.positions_backfill import backfill_record

    rec, ps = _seed_record(db_session)
    strat = db_session.query(Strategy).filter_by(portfolio_id=ps.id).one()
    bm_pts = _ca_daily_points([0.0] + _CA_WEEKS_B)
    days = sorted(bm_pts)

    # 两笔成交（首日 + 第 6 日），现金序列随之下移，使差分校验真正考察累积
    for i, d in enumerate(days):
        held = i >= 5
        cash = Decimal("997900.00" if held else "999000.00")
        market_value = Decimal("2100.00" if held else "1000.00")
        db_session.add(BacktestDailySnapshot(
            backtest_record_id=rec.id, target_type="portfolio", target_id=ps.id,
            snap_date=d, total_value=cash + market_value, cash=cash,
            market_value=market_value))
    for i, (price, amount) in enumerate(
            [("10.00", "1000.00"), ("11.00", "1100.00")]):
        db_session.add(BacktestTrade(
            backtest_record_id=rec.id, strategy_id=strat.id,
            signal_name="open_sig", signal_type="OPEN", stock_code="000001.SZ",
            trade_type="BUY", price=Decimal(price), quantity=100,
            amount=Decimal(amount), commission=Decimal("0.00"),
            stamp_duty=Decimal("0.00"),
            bar_time=datetime(days[0 if i == 0 else 5], 15, 0)))
    db_session.commit()

    monkeypatch.setattr(
        svc, "TQData",
        lambda: _FakeIndexTQ({"000300.SH": _ca_decimal_frames(bm_pts)}))
    svc.recompute_market_regime(db_session, rec.id)
    row = db_session.query(BacktestMarketRegimeAnalysis).filter_by(
        backtest_record_id=rec.id).one()
    assert json.loads(row.payload_json)[
        "industry_attribution"]["status"] == "needs_rerun"

    # 价格与存量市值逐日一致（加仓后均价 10.50 → 市值 2100），故 k ≡ 1
    prices = {d: (Decimal("10.00") if i < 5 else Decimal("10.50"))
              for i, d in enumerate(days)}
    out = backfill_record(db_session, rec.id,
                          close_provider=lambda codes, s, e: {
                              "000001.SZ": prices})
    assert out["status"] == "ok" and out["written"] is True
    assert out["cash_check"]["ok"] is True
    assert out["scale"]["max_dev"] == "0"   # 无漂移，无需缩放

    monkeypatch.setattr(
        svc, "TQData",
        lambda: _FakeIndexTQ({"000300.SH": _ca_decimal_frames(bm_pts)}))
    svc.recompute_market_regime(db_session, rec.id)
    row = db_session.query(BacktestMarketRegimeAnalysis).filter_by(
        backtest_record_id=rec.id).one()
    # fake TQ 未提供行业指数 → failed（而非 needs_rerun），证明已越过双门禁
    assert json.loads(row.payload_json)[
        "industry_attribution"]["status"] == "failed"
```

- [ ] **Step 2: 运行测试确认通过**

Run: `uv run pytest core/tests/unit/test_backtest_service.py -q -k backfilled_positions`
Expected: PASS — `1 passed`

- [ ] **Step 3: 运行全量测试确认无回归**

Run: `uv run pytest core/tests/ -q`
Expected: 全部 PASS（基线 783 passed + 本次新增 28 条）

- [ ] **Step 4: 提交**

```bash
git add core/tests/unit/test_backtest_service.py
git commit -m "test(backfill): 回填后行业归因越过 needs_rerun 双门禁（端到端验收）"
```

---

### Task 7: 在真实库上执行回填并验证栏目八

**Files:**
- 无代码改动（本任务只运行脚本 + 验证）
- Modify: `main/data/dev.db`（本地数据；按仓库 `.gitignore` 策略处理）

**Interfaces:**
- Consumes: Task 5 的 CLI
- Produces: 库中 15,022 条组合快照的 `positions_json` + 10 条刷新后的市场态 payload

- [ ] **Step 1: 全量 dry-run，人工审阅对账表**

Run: `uv run python scripts/backfill_positions_json.py --all-including-done`
Expected: 10 条记录全部 `[OK]`，且每条「现金校验 ok=True 不符 0 日」。
**若任一条现金校验不符，立即停止排查——那说明持仓重放的数量或时序有误，
绝不能 `--apply`。**

- [ ] **Step 2: 备份数据库**

Run: `Copy-Item data/dev.db data/dev.db.bak-before-backfill`

- [ ] **Step 3: 写入并重算**

Run: `uv run python scripts/backfill_positions_json.py --all-including-done --apply --recompute`
Expected: 10 条全部写入；重算阶段每条打印 `[OK] record N 市场态重算：completed`
（需通达信客户端在运行；128 个行业指数取数较慢，可能几十分钟）

- [ ] **Step 4: 验证落库结果**

Run:
```bash
uv run python -c "import sqlite3;c=sqlite3.connect('data/dev.db');print(c.execute('SELECT COUNT(*), SUM(positions_json IS NOT NULL) FROM backtest_daily_snapshots WHERE target_type=?',('portfolio',)).fetchone())"
```
Expected: 两个数字相等（15,022 / 15,022）

- [ ] **Step 5: 验证行业归因已脱离 needs_rerun**

Run:
```bash
uv run python -c "import sqlite3,json;c=sqlite3.connect('data/dev.db');[print(r[0], json.loads(r[1])['industry_attribution']['status']) for r in c.execute('SELECT backtest_record_id,payload_json FROM backtest_market_regime_analyses ORDER BY backtest_record_id')]"
```
Expected: 每条都不是 `needs_rerun`

- [ ] **Step 6: 前端验收（手动）**

启动 `uv run uvicorn core.main:app --reload` 与 `web/` 的 `npm run dev`，打开任一
老回测详情页，滚到「🚀 机遇Alpha · ⚖️ 中性Alpha 专题分析」，确认：
- 第八节不再是「该记录回测时未保存持仓明细，重新回测后可用」
- 第五/六/七/七-B 的「实际统计窗口」披露仍在
- 全部 7 张图正常渲染（回归自本轮修复的 boundaryGap 根因）

- [ ] **Step 7: 清理备份**

确认无误后 `Remove-Item data/dev.db.bak-before-backfill`；若第六步发现问题则用备份还原。

---

## 自审记录

- **Spec 覆盖**：`replay_cash`（Task 1）、`replay_positions`（Task 2）、`value_positions`
  含结转/avg_cost 兜底/缩放/unscaled/入参不可变（Task 3）、现金差分硬校验与缩放门限
  与单次提交（Task 4）、CLI 默认 dry-run/--apply/--force/--recompute（Task 5）、
  双门禁端到端验收（Task 6）、真实库执行与前端验收（Task 7）——spec 全部条目均有对应任务。
- **占位符扫描**：无 TBD/TODO；所有代码块为可直接粘贴的完整实现。
- **类型一致性**：四个函数名在实现步骤、测试代码、CLI 中一致；
  `close_provider(codes, start, end)` 签名在 Task 4 实现/测试、Task 6 测试、Task 5 CLI 中一致；
  `notes` 的 5 个键在 Task 3 实现与测试中一致。
- **Decimal 断言复核**：所有测试成交价用两位小数字符串，缩放后的 `market_value`
  经 `quantize` 后可精确断言；`test_backfill_record_uses_carried_forward_price` 的
  `max_dev` 用前缀比较（`"0.1111"`，因 1/900 是无限循环小数）避免脆弱的等值断言。
- **现金种子自洽复核**：`test_backfill_record_writes_positions_json` 的 4 天现金序列
  （10000 → 9000 → 7800 → 7800）与两笔成交（1/3 买 1000.00、1/4 买 1200.00）严格自洽，
  且首日 D1 无成交 → 差分校验首日必过。
- 预检修正（2026-09-28）：差分校验原写作 `stored[i]-stored[0] == cumflow[i]`，
  隐含「首条快照不含当日成交」。实测当前库 10 条记录首日成交数均为 0，前提成立，
  但已改为恒成立的 `cumflow[i] - cumflow[0]`（`cumflow[0]==0` 时两式等价），
  并补 `test_backfill_record_accepts_trade_on_first_snapshot_day` 覆盖该分支。
  Task 6 的夹具同步改为两笔成交 + 相应现金序列，使差分真正考察累积而非恒为 0。
- Task 1 实施修正（2026-09-28）：`test_replay_cash_rejects_unknown_trade_type` 原本
  用小写 `"buy"` 期望 `ValueError`，与实现里的 `.upper()` 归一化自相矛盾
  （小写归一化后是合法输入，与 `live_engine.py:1139` 同策略）。已改为用真正未知的
  `"HOLD"`，并补 `test_replay_cash_accepts_lowercase_trade_type` 固化归一化行为。
- 数量修正（2026-09-28）：组合快照实为 **15,022** 条（10 条记录），此前文档误写
  13,022。Task 7 Step 4 的期望值已同步。
- Task 1 评审修正（2026-09-28，commit `a947877`）：`replay_cash` 的**函数体正确但契约说明错**。
  Task 1 代码块里的 docstring 仍写着旧的 `stored_cash[i]-stored_cash[0] == cumflow[i]`
  （我最初只改了 Task 4 的编排代码，漏改了 Task 1 代码块内的 docstring），实施者忠实转录。
  该式仅在 `cumflow[0]==0` 时成立；首条快照当日有成交时恒等式偏一个 `F(0)`，会误判记录不自洽。
  Task 1 实施报告里给出的反例（BUY 1000.00+5.00 于 01-03、SELL 2000.00-5.00-2.00 于 01-05）
  实证了这一点。docstring 已同步为 `cash[i]-cash[0] == cumflow[i]-cumflow[0]`。
- Task 1 评审发现 `.upper()` 归一化零覆盖：删掉该行 5 个测试全绿。
  已补 `test_replay_cash_accepts_lowercase_trade_type`；实施者用变异测试确认
  「删 `.upper()` → 仅此测试失败」，确认真正锚定了该行为。
- Task 2 实施修正（2026-09-28）：`test_replay_positions_sell_is_fifo_and_flattens` 断言
  1/4 剩余 50 股 `sellable_quantity == 0`，与实现的 `available_shares_on` 矛盾。
  计划注释「当日不可卖」是推理失误——该夹具 1/4 **没有买入**，50 股来自 1/3 桶，
  `d < query_date` 判定 1/3 < 1/4 已过 T+1，故为 50。决定性证据：`portfolio.py:488`
  （当初写入 `positions_json` 的函数）用的正是同一调用，重建值必须与之一致。
  断言已改为 `== 50` 并订正注释；T+1 反向覆盖仍在其他用例（买入日 0、次日全量）。
  另：Step 4 期望数 `10 passed` 系 Task 1 fix wave 多加 1 个测试所致，实际 `11 passed`。
