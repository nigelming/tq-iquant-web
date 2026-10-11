# 修复批次 1：#7 资金可为负 + #3 信号对齐错位

## Context

回测审查报告中两个高优先级问题：

- **#7**：BUY 订单审批用信号 bar 收盘价（`portfolio._signal_to_order` 设 `order.price=close`），实际成交在下一 bar 开盘价（`SimulatedDispatcher.place_order` 用 `open_prices`）。跳空高开时 `open >> close`，`account.approve_order` 按收盘价通过，`apply_trade` 按开盘价扣款 → 现金为负。`deduct_cash` 无负值守卫。
- **#3**：`_bar_times_by_code(klines)` 合并所有周期时间戳。5m 策略公式输出（4 条）被按索引对齐到 `[1d 00:00, 5m 09:30, 5m 09:35, 1d 00:00, ...]` 混合时间轴 → 信号串位，cache key 落在错误时间点。

## 修复方案

### #7 资金可为负

**文件**：`main/core/engine/execution_engine.py`

**改法**：在 `ExecutionEngine.execute()` 中，`place_order` 返回 trade 后、`apply_trade` 前，用实际成交成本（`trade.amount + trade.commission + trade.stamp_duty`）二次校验现金。不足时用 `trade.price`（已含滑点）重新调 `account.approve_order`：缩量到 100 股整数倍或拒单。

```python
# execute() 内，place_order 之后、apply_trade 之前：
if apply and order.trade_type == TradeType.BUY:
    cost = trade.amount + trade.commission + trade.stamp_duty
    if cost > account.cash:
        approved, qty = account.approve_order(
            order.quantity, trade.price,
            position.market_value if position else Decimal("0"),
        )
        if not approved or qty < 100:
            self._rec_gate("insufficient_funds", "reject", order, "capital_gate",
                           requested=order.quantity, final=0,
                           param_name="cash", actual_value=float(account.cash),
                           message="跳空高开资金不足：成交价 %s，可用 %s" % (trade.price, account.cash))
            return None
        if qty < order.quantity:
            self._rec_gate("order_shrunk", "shrink", order, "capital_gate",
                           requested=order.quantity, final=qty,
                           param_name="cash", actual_value=float(account.cash),
                           message="跳空高开缩量：%d → %d @ %s" % (order.quantity, qty, trade.price))
            order.quantity = qty
            trade = self._dispatcher.place_order(order)
            if not trade:
                return None
```

**不动**：`account.py`（`deduct_cash` 不加负值守卫——由 execution 层拦截更合理，能在拒单时记录决策事件）、`backtest_engine.py`（不改动 pending orders 循环）。

### #3 信号对齐错位

**文件**：`main/core/services/backtest_service.py`

**改法**：新增 `_bar_times_by_period(klines, period)` 只提取指定周期的时间轴。在 `build_signal_cache` 中，将 `_bar_times_by_code(klines)`（全周期并集）替换为按策略周期过滤的调用。

```python
def _bar_times_by_period(klines: dict, period: str) -> Dict[str, List[datetime]]:
    """从 klines 提取每只股票指定周期的时间轴（升序）。"""
    result: Dict[str, List[datetime]] = {}
    for code, periods in klines.items():
        df = periods.get(period)
        if df is not None and "datetime" in df.columns:
            result[code] = sorted(df["datetime"].to_list())
    return result
```

`build_signal_cache` 内替换：
```python
# 旧：bar_times_by_code = _bar_times_by_code(klines)  # 全周期并集
#     bt = bar_times_by_code if period in _MINUTE_PERIODS else None

# 新：按策略周期过滤
bt = _bar_times_by_period(klines, period) if period in _MINUTE_PERIODS else None
```

保留 `_bar_times_by_code` 函数定义（不删，避免破坏其他可能的引用），但 `build_signal_cache` 不再调用它。

## 测试

### #7 测试（`main/core/tests/unit/test_execution_engine.py` 或 `test_backtest_engine.py`）

1. `test_gap_up_buy_rejected_when_cash_insufficient` — 收盘 10 审批 1000 股（需 10000=现金），次日开盘 12（实际需 12000>10000）→ 拒单，trade=None，cash 不变
2. `test_gap_up_buy_shrunk_when_partial_cash` — 收盘 10 审批 1000 股，次日开盘 11（需 11000>10000）→ 缩量到 900 股（9900<10000），trade.quantity=900
3. `test_normal_buy_no_gap_no_recheck` — 收盘=开盘=10，无跳空 → 正常成交，不受影响（回归）

### #3 测试（`main/core/tests/unit/test_backtest_data.py`）

1. `test_build_signal_cache_multi_period_filters_by_strategy_period` — klines 含 1d(2 bar) + 5m(4 bar)，5m 策略 → cache key 全部在 5m 时间点，不含午夜 1d 时间
2. `test_build_signal_cache_multi_period_daily_strategy_uses_date_match` — 同上 klines，1d 策略 → cache key 在午夜 1d 时间点（走 Date 匹配，不传 bar_times）

## 验证

```bash
# 后端
cd main && uv run pytest core/tests/ -q

# 前端（本批不涉及前端改动，确认无回归即可）
cd web && npx vitest run && npx vue-tsc -b
```
