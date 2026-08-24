# 0011 切片5 — 订单状态机 + 成交回报回填

> 状态：**待实施**（G3/G4 匹配键已真机定案 2026-08-10，前置验证无卡点）
> 日期：2026-08-10
> 接续：0009 切片4 已打通 LiveEngine 下单链路（BarPoller → Portfolio.on_bar → HttpBridgeDispatcher 下单 → 落库 + 持仓恢复）；0010 切片5 补公式注入链路。但下单后**成交确认仍是近似**（`status=accepted`、`filled_price=bar.close`、`commission=0`），真实成交回报未回填。本计划补上订单生命周期管理 + 真实成交回填。

## 1. 背景与目标

### 1.1 问题：成交确认缺失

当前 `_persist_trade`（`live_engine.py:379`）在 `HttpBridgeDispatcher.place_order` 返回后立即写 `LiveOrder(status=accepted) + LiveTrade`，成交价取 `order.price`（≈ bar.close），佣金/印花税为 0。这是**受理即成交**近似——对 prType=14 对手价单，实际成交价是盘口一档价（≠ close），佣金/印花税由券商计算，均未反映。

更深层问题：
- **无订单状态机**：LiveOrder `status` 只有 `accepted`，没有 `submitted/partial/filled/rejected/cancelled` 区分，拒单/部分成交无法表达
- **无成交回报回填**：桥 `/deals` 已返回真实成交价/佣金（G4 真机已验），但 Core 从未轮询回填
- **无拒单修正**：拒单后虚拟持仓/现金已 apply_trade 但真实未成交 → 背离
- **提交时序风险**：`_persist_trade` 先 `place_order` 再 commit，崩在中间 → DB 无记录但券商已成交（I4 命门窗口）

### 1.2 目标

1. **订单状态机**（G1）：LiveOrder 状态 `submitted → partial → filled / rejected / cancelled`，提交时序改为「先写 submitted+commit → 再 passorder」
2. **主循环轮询 /deals**（G2）：每轮查未完结 LiveOrder，按 `m_strOrderRef` 匹配桥成交回报，回填真实价/量/佣金
3. **拒单/部分成交修正**（G6）：拒单反向退回虚拟持仓/现金；部分成交按实际成交量修正
4. **跨重启挂回**（I4）：Core 重启后查 `status in (submitted, partial)` 挂回引擎待回填队列
5. **桥状态并入 API**（G7）：session 详情暴露桥在线/离线/最近回填时间等

### 1.3 不在本计划范围

- **对账逻辑**（D3）：recover 后虚拟 vs 桥 /positions 对比，独立于回填
- **轮询频率调优**（G5）：首期跟主循环同频（30s），后续可独立调
- **同 bar 多策略超卖**（F6）：设计决策待定，不依赖回填
- **LiveT1Checker 接桥 available**（F5）：独立改进，切片5 不改 T1 检查
- **三段式周期链路**（C6）：与切片5 平行，不阻塞

## 2. 已验证结论（真机实测 2026-08-10）

### 2.1 订单匹配键 = `m_strOrderRef`（G3 定案）

DEAL 与 ORDER 对象**共享同一 `m_strOrderRef`**（委托引用号），3 笔真实成交（BRIDGE×2 + GUI×1）全部对上：
- `...3499794`↔`...3499794`
- `...502163`↔`...502163`
- `...502165`↔`...502165`

`m_strOrderSysID`（合同号）同样一致。

**关键：passorder 返回 0 无法预知 OrderRef**，故匹配流程：
```
Core 下单 → 桥 passorder 受理(返回0)
→ Core 轮询桥 /orders，用组合键定位自己的委托(见 §3.2)
→ 取 m_strOrderRef 回写 LiveOrder
→ 轮询 /deals 按 OrderRef 关联成交
→ 回填
```

### 2.2 DEAL/ORDER 字段齐全（G4 定案）

**DEAL 对象**（桥 `query_deals` 已改，`iquant_bridge.py:245`）：
| 字段 | iQuant 属性 | 说明 |
|---|---|---|
| order_ref | m_strOrderRef | 匹配键 |
| order_sysid | m_strOrderSysID | 合同号 |
| trade_id | m_strTradeID | 成交编号 |
| instrument | m_strInstrumentID | 如 600000（无后缀） |
| exchange | m_strExchangeID | SH/SZ |
| direction | m_nDirection | 48=买/49=卖 |
| price | m_dPrice | 成交价 |
| volume | m_nVolume | 成交量 |
| amount | m_dTradeAmount | 成交金额 |
| commission | m_dCommission | 佣金 |
| trade_time | m_strTradeTime | 成交时间 |
| trade_date | m_strTradeDate | 成交日期 |
| source | m_strSource | BRIDGE/GUI |
| order_type | m_strOrderStrategyType | 函数下单/常规下单 |

**ORDER 对象**（桥 `query_orders` 已改，`iquant_bridge.py:215`）：
| 字段 | iQuant 属性 | 说明 |
|---|---|---|
| order_ref | m_strOrderRef | 匹配键 |
| order_sysid | m_strOrderSysID | 合同号 |
| instrument/exchange | m_strInstrumentID/m_strExchangeID | 同上 |
| direction | m_nDirection | 48/49 |
| limit_price | m_dLimitPrice | 委托价 |
| traded_price | m_dTradedPrice | 成交均价 |
| volume | m_nVolumeTotalOriginal | 委托量 |
| traded_volume | m_nVolumeTraded | 已成交量 |
| status | m_nOrderStatus | **官方码：48未报/49待报/50已报/51已报待撤/52部成待撤/53部撤(终态)/54已撤(终态)/55部成(非终态,剩余仍撮合)/56已成(终态)/57废单(终态)**。Core 终态处理：53/54→canceled、57→rejected、56→filled(deals回填)、55→在途不碰。⚠️ 55 是「部成」非「部撤」，旧代码误判已修正（2026-08-24 据 D:\iquant xtconstant.py 核对） |
| source | m_strSource | BRIDGE/GUI |
| insert_time/date | m_strInsertTime/m_strInsertDate | 委托时间 |
| cancel_amount | m_dCancelAmount | 撤单量 |

**POSITION 对象**（F5/D3 定案）：`m_nCanUseVolume` 精确反映 T+1 可用；`m_strInstrumentID`+`m_strExchangeID` 拼接对账可行。

### 2.3 桥模式要求

桥策略**必须以「实盘交易」模式运行**——模拟模式下 passorder 只出策略信号不发真实委托（迅投硬规则，2026-08-10 真机验证）。部署文档须注明。

## 3. 架构

### 3.1 订单状态机（G1）

```
                  passorder 受理
    ┌─────────── ─────────────── ──────────┐
    │                                       ▼
  created ──► submitted ──► partial ──► filled
                  │               │
                  ▼               ▼
              rejected        cancelled
```

状态语义：
- `created`：LiveOrder 已写库（commit），passorder 未发
- `submitted`：passorder 已发，等待券商确认（桥受理返回 0）
- `partial`：部分成交（`traded_volume < volume`）
- `filled`：全部成交（`traded_volume == volume`）
- `rejected`：券商拒绝（桥返回非 0 / 超时无回报 / ORDER status 非 54/56 且超时）
- `cancelled`：撤单（ORDER status=54 或 55）

**提交时序改造**（I4 闭合关键）：
```
① 写 LiveOrder(status=submitted) + commit   ← 崩在此后 DB 有记录
② place_order → 桥 passorder                ← 崩在此后 DB 有 submitted，G2 轮询可挂回
③ 据返回更新 LiveOrder.status/filled_*       ← 正常路径
④ 部分成交：后续 G2 轮询回填                  ← 异步路径
```

**submitted 阶段不做 apply_trade**：`ExecutionEngine.execute` 在 `place_order` 返回后才 apply，改造后 submitted 阶段不 apply（不扣现金/不建持仓），回填确认 filled 时才 apply。这是与现状最大的行为差异。

### 3.2 OrderRef 匹配流程

passorder 返回 0 无法预知 `m_strOrderRef`，需轮询桥 `/orders` 定位：

```
Core 下单(order_id=md5, code, op, volume)
  → 桥 passorder 受理(返回 0)
  → Core 轮询桥 GET /orders
  → 从 ORDER 列表筛选：
      source="BRIDGE"          ← 桥发的单（排除 GUI 手动单）
      instrument+exchange = code 匹配
      direction = 48(买)/49(卖) 匹配
      volume = 委托量匹配
      insert_time 在下单时间窗口内（±30s）
  → 取最新一条（全局限 1 session 串行，不会撞单）
  → 回写 LiveOrder.order_ref = m_strOrderRef
  → 后续 /deals 轮询按 order_ref 关联成交
```

**兜底**：首次轮询未找到匹配 → 标 `pending_ref`，下轮继续找；超 N 轮未找到 → 标 `rejected`（可能券商秒拒，ORDER 列表不出现）。

### 3.3 回填轮询（G2）

主循环 `_loop` 每轮（30s）在心跳+拉 bar 后，追加一步 `_poll_deals()`：

```
_poll_deals():
  1. 查 DB 未完结 LiveOrder：status in (submitted, partial)
  2. 对未回填 order_ref 的单：调桥 GET /orders 定位 order_ref（§3.2）
  3. 对有 order_ref 的单：调桥 GET /deals 全量查，按 order_ref 过滤
  4. 据成交回报更新 LiveOrder + 补写/更新 LiveTrade：
     - 部分成交：LiveOrder.status=partial, filled_quantity/filled_price 更新
     - 全部成交：LiveOrder.status=filled, LiveTrade 回填真实价/量/佣金
     - 拒单(ORDER.status=54 且 traded_volume=0)：LiveOrder.status=rejected, G6 修正
  5. filled/rejected 后：apply_trade 或 reverse_trade（见 §3.4）
```

### 3.4 拒单/部分成交修正（G6）

**拒单修正**（rejected）：
- 当前 `ExecutionEngine.execute` 在 `place_order` 成功后立即 `account.apply_trade + position.apply_trade`
- 改造后：**submitted 阶段不 apply**，回填确认 filled 时才 apply
- 拒单时无 apply 需撤回 → 无操作（因为没 apply 过）
- 但如果部分成交后最终被撤单（cancelled）：需对已 apply 的部分做**反向修正**
  - `account.apply_reverse(trade)`：加回现金（扣除实际成交额+佣金）
  - `position.apply_reverse(trade)`：减去成交量

**部分成交→filled**：
- 首次 apply 时用实际成交 TradeEvent（真实价/量/佣金），非近似值
- 后续部分成交追加 apply（增量）

**反向修正函数**：
```python
# Account
def apply_reverse(self, trade: TradeEvent) -> None:
    """拒单/撤单：撤回已 apply 的成交（反向操作）。"""
    if trade.trade_type == TradeType.BUY:
        # 买入被撤：加回现金（原扣了 price*qty+commission）
        self.cash += trade.amount + trade.commission + trade.stamp_duty
    else:
        # 卖出被撤：扣回现金（原加了 price*qty-commission-stamp）
        self.cash -= (trade.amount - trade.commission - trade.stamp_duty)

# Position
def apply_reverse(self, trade: TradeEvent) -> None:
    """拒单/撤单：撤回已 apply 的成交（反向操作）。"""
    if trade.trade_type == TradeType.BUY:
        self.quantity -= trade.quantity
        # 重建 avg_cost：如果 quantity>0，保持原 avg_cost；quantity=0 时重置
        if self.quantity <= 0:
            self.quantity = 0
            self.avg_cost = Decimal("0")
    else:
        self.quantity += trade.quantity
        # avg_cost 不变（卖出撤回，持仓回补，成本基础不变）
```

### 3.5 跨重启挂回（I4）

`LiveEngine.recover` 除重放 `live_trades` 外，追加：
```python
# 查未完结 LiveOrder
pending = db.query(LiveOrder).filter(
    LiveOrder.live_session_id == self.session_id,
    LiveOrder.status.in_(["submitted", "partial"]),
).all()
# 挂回引擎待回填队列
for lo in pending:
    self._pending_orders[lo.id] = lo  # _poll_deals 会在主循环处理
```

## 4. LiveOrder/LiveTrade 模型变更

### 4.1 LiveOrder 新增字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `order_ref` | String(30) | 桥 `m_strOrderRef`，匹配成交回报（G3 定案键） |
| `bridge_order_id` | String(32) | Core 生成的 MD5 order_id（透传给桥的幂等键） |

迁移：`alembic revision --autogenerate -m "add_order_ref_to_live_order"`

### 4.2 LiveOrder.status 值域扩展

从 `{accepted}` 扩展为 `{created, submitted, partial, filled, rejected, cancelled}`。

现有数据兼容：`accepted` 语义 ≈ `filled`（首期受理即成交），迁移时 `UPDATE live_orders SET status='filled' WHERE status='accepted'`。

### 4.3 LiveTrade 不变

LiveTrade 字段已齐全（price/quantity/amount/commission/stamp_duty/trade_time），回填时更新值即可。新增场景：部分成交可能产生多条 LiveTrade（每笔成交一条），通过 `live_order_id` 关联。

## 5. 实现清单

### 5.1 ExecutionEngine.execute 改造（`execution_engine.py`）

**现状**：`place_order` → 立即 `apply_trade` → 返回 TradeEvent

**改造后**：
```python
def execute(self, order, account, position) -> Optional[TradeEvent]:
    # ... BUY 资金审批 / SELL T+1 检查（不变）...
    trade = self._dispatcher.place_order(order)
    if not trade:
        return None
    # ★ 不再立即 apply_trade，由 LiveEngine._handle_bar 据 status 决定
    # 返回的 trade 仅作记录（status=submitted，价/量是请求值非成交值）
    return trade
```

`apply_trade` 调用移到 `_handle_bar` 的回填确认路径（filled 时）。

### 5.2 LiveEngine._handle_bar 改造（`live_engine.py`）

**现状**：`place_order` → `_persist_trade(status=accepted)` → commit

**改造后**：
```python
def _handle_bar(self, portfolio, bar):
    # ... update_peak / _fill_signal_cache / on_bar（不变）...
    for order in orders:
        # ① 先写 LiveOrder(status=submitted) + commit
        live_order = self._persist_order_submitted(db, order)
        db.commit()
        try:
            # ② 再 place_order
            trade = self._engine.execute(order, portfolio.account, pos)
        except BridgeUnavailableError:
            live_order.status = "rejected"
            live_order.error_message = "bridge unavailable"
            db.commit()
            continue
        if trade is None:
            live_order.status = "rejected"
            live_order.error_message = "bridge rejected"
            db.commit()
            continue
        # ③ 更新为 submitted（桥受理），等待 G2 轮询回填
        live_order.bridge_order_id = self._dispatcher._order_id(order)
        # submitted 阶段：不 apply_trade，不写 LiveTrade
        # 首期简化：同步查一次 /orders 取 order_ref（减少延迟）
        self._try_match_order_ref(live_order)
        db.commit()
```

### 5.3 新增 `_persist_order_submitted`（`live_engine.py`）

```python
def _persist_order_submitted(self, db, order) -> LiveOrder:
    """写 LiveOrder(status=submitted)，不写 LiveTrade。返回 LiveOrder 供后续更新。"""
    lo = LiveOrder(
        live_session_id=self.session_id,
        portfolio_strategy_id=order.portfolio_id,
        strategy_id=order.strategy_id,
        stock_code=order.stock_code,
        trade_type=order.trade_type.value.lower(),
        order_type="limit",
        price=order.price,
        quantity=order.quantity,
        filled_quantity=0,
        filled_price=None,
        status="submitted",
        signal_name=order.signal_name or None,
        signal_type=order.signal_type.value if order.signal_type else None,
        bar_time=order.bar_time,
    )
    db.add(lo)
    db.flush()  # 取 lo.id
    return lo
```

### 5.4 新增 `_try_match_order_ref`（`live_engine.py`）

```python
def _try_match_order_ref(self, live_order: LiveOrder) -> None:
    """下单后同步查桥 /orders 定位 m_strOrderRef 回写。"""
    try:
        orders = self._dispatcher.query_orders()
    except BridgeUnavailableError:
        return  # 下轮 _poll_deals 再找
    code = live_order.stock_code
    op_dir = 48 if live_order.trade_type == "buy" else 49
    now = datetime.now()
    for o in orders:
        if o.get("source") != "BRIDGE":
            continue
        inst = o.get("instrument", "")
        exch = o.get("exchange", "")
        if "%s.%s" % (inst, exch) != code:
            continue
        if o.get("direction") != op_dir:
            continue
        if o.get("volume") != live_order.quantity:
            continue
        # 时间窗口：下单时间 ±60s
        insert_time = o.get("insert_time") or ""
        insert_date = o.get("insert_date") or ""
        # ... 解析时间，判在窗口内 ...
        live_order.order_ref = o.get("order_ref")
        return
```

### 5.5 新增 `_poll_deals`（`live_engine.py`）

```python
def _poll_deals(self) -> None:
    """主循环每轮调：查未完结 LiveOrder → 轮询桥 /orders+/deals → 回填。"""
    db = self._db_session_factory()
    try:
        pending = db.query(LiveOrder).filter(
            LiveOrder.live_session_id == self.session_id,
            LiveOrder.status.in_(["submitted", "partial"]),
        ).all()
        if not pending:
            return
        # 1. 未回填 order_ref 的单：尝试定位
        for lo in pending:
            if lo.order_ref is None:
                self._try_match_order_ref(lo)
        # 2. 有 order_ref 的单：查 /deals 回填
        try:
            deals = self._dispatcher.query_deals()
        except BridgeUnavailableError:
            return
        for lo in pending:
            if lo.order_ref is None:
                continue
            matched = [d for d in deals if d.get("order_ref") == lo.order_ref]
            if not matched:
                continue
            self._backfill_order(db, lo, matched)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("_poll_deals error")
    finally:
        db.close()
```

### 5.6 新增 `_backfill_order`（`live_engine.py`）

```python
def _backfill_order(self, db, live_order, matched_deals) -> None:
    """据成交回报回填 LiveOrder + LiveTrade + apply_trade。"""
    total_qty = sum(int(d.get("volume", 0)) for d in matched_deals)
    total_amount = sum(Decimal(str(d.get("amount", 0))) for d in matched_deals)
    avg_price = total_amount / total_qty if total_qty > 0 else Decimal("0")
    total_commission = sum(Decimal(str(d.get("commission", 0))) for d in matched_deals)

    # 更新 LiveOrder
    live_order.filled_quantity = total_qty
    live_order.filled_price = avg_price

    if total_qty >= live_order.quantity:
        live_order.status = "filled"
    else:
        live_order.status = "partial"

    # 写/更新 LiveTrade（按 order_ref 汇总为一笔）
    # 查已有 LiveTrade（部分成交追加场景）
    existing = db.query(LiveTrade).filter(
        LiveTrade.live_order_id == live_order.id
    ).first()
    trade_time = matched_deals[-1].get("trade_time")  # 最后一笔成交时间
    if existing:
        existing.price = avg_price
        existing.quantity = total_qty
        existing.amount = total_amount
        existing.commission = total_commission
        existing.trade_time = trade_time
    else:
        db.add(LiveTrade(
            live_session_id=self.session_id,
            live_order_id=live_order.id,
            portfolio_strategy_id=live_order.portfolio_strategy_id,
            strategy_id=live_order.strategy_id,
            stock_code=live_order.stock_code,
            trade_type=live_order.trade_type,
            price=avg_price,
            quantity=total_qty,
            amount=total_amount,
            commission=total_commission,
            stamp_duty=Decimal("0"),  # TODO: 从 DEAL 取印花税（字段待验证）
            trade_time=trade_time,
        ))

    # filled 时 apply_trade（首次 apply 用真实价/量/佣金）
    if live_order.status == "filled":
        self._apply_filled_trade(live_order, avg_price, total_qty, total_amount, total_commission)
```

### 5.7 新增 `_apply_filled_trade`（`live_engine.py`）

```python
def _apply_filled_trade(self, live_order, price, qty, amount, commission) -> None:
    """回填确认 filled 后 apply_trade 更新虚拟持仓/现金。"""
    portfolio = next((p for p in self.portfolios
                      if p.portfolio_id == live_order.portfolio_strategy_id), None)
    if portfolio is None:
        return
    ctx = self._find_strategy(portfolio, live_order.strategy_id)
    if ctx is None:
        return
    pos = ctx.positions.get(live_order.stock_code)
    trade = TradeEvent(
        strategy_id=live_order.strategy_id,
        portfolio_id=live_order.portfolio_strategy_id,
        stock_code=live_order.stock_code,
        trade_type=TradeType(live_order.trade_type.upper()),
        price=price,
        quantity=qty,
        amount=amount,
        commission=commission,
        stamp_duty=Decimal("0"),
        trade_time=live_order.bar_time or datetime.now(),
    )
    if pos is None and trade.trade_type == TradeType.BUY:
        pos = Position(live_order.stock_code)
        ctx.positions[live_order.stock_code] = pos
    portfolio.account.apply_trade(trade)
    if pos is not None:
        pos.apply_trade(trade)
```

### 5.8 `_loop` 接入 `_poll_deals`（`live_engine.py`）

```python
async def _loop(self) -> None:
    while self._running:
        try:
            # ... 心跳 + poll + _maybe_daily_close（不变）...
            self._poll_deals()          # ← 新增：每轮回填
        except ...:
            ...
```

### 5.9 `recover` 挂回未完结订单（`live_engine.py`）

```python
def recover(self, db) -> None:
    # ... 原有 live_trades 重放（不变）...
    # 挂回未完结 LiveOrder
    pending = db.query(LiveOrder).filter(
        LiveOrder.live_session_id == self.session_id,
        LiveOrder.status.in_(["submitted", "partial"]),
    ).all()
    self._pending_orders = {lo.id: lo for lo in pending}
```

### 5.10 Account.apply_reverse / Position.apply_reverse（新增）

见 §3.4 伪代码。用于 cancelled 场景撤回已 apply 的部分成交。

### 5.11 桥状态并入 session API（G7）

`GET /api/live/sessions/{id}` 响应追加：
```json
{
  "bridge_online": true,
  "pending_orders": 2,
  "last_backfill_time": "2026-08-10 14:30:15"
}
```

### 5.12 Alembic 迁移

```bash
cd main
uv run alembic revision --autogenerate -m "add_order_ref_to_live_order"
```

新增列：`live_orders.order_ref`（String(30), nullable=True）、`live_orders.bridge_order_id`（String(32), nullable=True）。
数据迁移：`UPDATE live_orders SET status='filled' WHERE status='accepted'`。

## 6. 复用的现有函数（不重写）

- `HttpBridgeDispatcher.query_orders` / `query_deals` / `query_positions` — 桥查询已就绪（G4 已改字段）
- `HttpBridgeDispatcher._order_id` — 确定性 MD5 order_id 生成
- `HttpBridgeDispatcher.heartbeat` — 心跳
- `ExecutionEngine.execute` — 核心执行逻辑（改造 apply_trade 时机）
- `Portfolio.on_bar` — 信号/风控/主从，不改
- `LiveEngine.recover` — 持仓恢复，追加挂回逻辑

## 7. 测试（TDD）

### 7.1 订单状态机单测（`test_live_engine.py` 补充）

- `test_persist_order_submitted_writes_status_submitted`：mock DB，断言 LiveOrder.status=submitted，无 LiveTrade
- `test_handle_bar_submitted_no_apply_trade`：mock place_order 返回 trade → 断言 account.cash/position.quantity 未变（submitted 不 apply）
- `test_handle_bar_bridge_unavailable_marks_rejected`：mock place_order 抛 BridgeUnavailableError → LiveOrder.status=rejected
- `test_order_status_transitions`：submitted→partial→filled 完整路径

### 7.2 OrderRef 匹配单测

- `test_try_match_order_ref_finds_bridge_order`：mock query_orders 返回匹配 ORDER → order_ref 回写
- `test_try_match_order_ref_ignores_gui_orders`：source=GUI 的单不匹配
- `test_try_match_order_ref_no_match_leaves_null`：无匹配 → order_ref 保持 None

### 7.3 回填单测

- `test_poll_deals_backfills_filled_order`：mock query_deals 返回成交 → LiveOrder.status=filled + LiveTrade 写库 + apply_trade
- `test_poll_deals_partial_fill`：成交量 < 委托量 → status=partial
- `test_poll_deals_rejected_order`：无成交 + ORDER.status=54 → rejected + 无 apply
- `test_backfill_uses_real_price_and_commission`：断言 filled_price/amount/commission 是真实值非 bar.close
- `test_poll_deals_bridge_offline_skips`：query_deals 抛 BridgeUnavailableError → 不改状态，下轮重试

### 7.4 拒单修正单测

- `test_apply_reverse_buy`：BUY reverse → cash 加回，quantity 减回
- `test_apply_reverse_sell`：SELL reverse → cash 扣回，quantity 加回
- `test_cancelled_partial_reverse_only_filled_part`：部分成交后撤单 → 只 reverse 已成交部分

### 7.5 跨重启挂回单测

- `test_recover_finds_pending_orders`：DB 有 submitted/partial → 挂回 _pending_orders
- `test_recover_ignores_filled_orders`：filled/rejected 不挂回

### 7.6 模型/迁移单测

- `test_live_order_status_values`：status 字段接受 created/submitted/partial/filled/rejected/cancelled
- `test_live_order_order_ref_nullable`：order_ref 允许 null（下单时未知）

### 7.7 集成测

- `test_full_flow_submit_match_backfill`：下单 → match order_ref → 回填 → apply_trade → 持仓/现金正确

## 8. 验证

1. **迁移**：`cd main && uv run alembic upgrade head` → 两新列 + status 迁移
2. **单测**：`uv run pytest core/tests/unit/ -v` → 全绿（既有 + 新增）
3. **集成测**：`uv run pytest core/tests/integration/ -v` → 全绿
4. **全量回归**：`uv run pytest` → 既有不回归
5. **E2E（用户本地，需 iQuant 桥 + 实盘模式）**：
   - 起桥（实盘模式）→ Core 起实盘 session → 下单 → LiveOrder status=submitted
   - 等 30s → _poll_deals 回填 → LiveOrder status=filled + LiveTrade 有真实价/佣金
   - 验证虚拟持仓/现金与桥 /positions 对账一致
   - 测试拒单：超限/白名单外 → status=rejected + 无 apply
   - 测试跨重启：Core 重启 → recover 挂回 submitted → _poll_deals 回填 → filled

## 9. 已知限制 / 后续

- **印花税字段**：DEAL 对象可能含印花税字段（待验证），首期 stamp_duty=0，后续补
- **部分成交→撤单**：cancelled 场景的 reverse_trade 需要 apply_reverse，首期简化处理
- **OrderRef 匹配延迟**：同步查 /orders 在下单后立即调，可能券商还未回报（ORDER 列表尚未出现）→ 退回异步轮询，首期可接受
- **G5 轮询频率**：首期跟主循环 30s，成交回报通常秒级返回，30s 延迟可接受；高频策略需单独调
- **单 session 串行**：全局限 1 个 session（B6），OrderRef 匹配不会撞单；多 session 时需加 session_id 区分
- **F8 成交价近似消除**：回填后 LiveTrade.price 是真实成交价，F8 问题在本切片解决
- **F9 佣金/印花税**：回填后 LiveTrade.commission 是真实佣金，stamp_duty 待补字段
- **F10 status 拆分**：LiveOrder.status 从 accepted 扩展为 submitted/partial/filled/rejected/cancelled，F10 在本切片解决

## 10. 与其他切片的关系

| 切片 | 关系 |
|---|---|
| 0009 切片4 | 下单链路基础（HttpBridgeDispatcher + BarPoller + LiveEngine._handle_bar），本切片改造 _handle_bar + 补 _poll_deals |
| 0010 | 公式注入链路，不改（_fill_signal_cache 仍填 signal_cache） |
| C6（周期链路） | 平行工作，不阻塞。C6 改 _loop 轮询/分发，本切片补 _poll_deals 调用点，两者合入同一 _loop |
| F5（LiveT1Checker） | 独立改进，不改 T1 检查 |
| D3（对账） | 回填后虚拟持仓更准，对账基础更好，但 D3 逻辑独立 |
| I4（跨重启） | 本切片实现 I4 的「挂回未完结」部分，I4 的完整闭合依赖 G1+G2+G6 |
