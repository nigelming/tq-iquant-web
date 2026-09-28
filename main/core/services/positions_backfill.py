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
import json
from datetime import date
from decimal import Decimal
from typing import Dict, List, Sequence, Tuple

from sqlalchemy.orm import Session

from core.engine.event import TradeEvent
from core.engine.position import Position
from core.models import BacktestDailySnapshot, BacktestRecord, BacktestTrade
from core.services.backtest_service import build_index_close_series
from tq_iquant_shared.constants import TradeType

SCALE_TOLERANCE = Decimal("0.05")


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
    avg_cost(=cost_value/quantity)，与 Portfolio.positions_detail 处
    `price is None or price <= 0 → price = pos.avg_cost` 口径一致。
    非正收盘价同样算缺价：行情源对停牌股会给出 0 占位，若当成真价，该槽市值为 0
    会压小 raw_sum、连带把其余持仓的 k 放大。
    缩放：事后取到的是今日前复权价，与回测当时存在除权漂移。若直接写入，
    行业权重（market_value/total_value）之和会偏离 1。按 stored_market_value
    缩放，Σ 市值精确回到回测自身存量市值，权重之和为 100%。
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
        if price is None or price <= 0:
            carried = _last_price(series, snap_date)
            if carried is not None and carried <= 0:
                carried = None
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
        # cost_value 同样量化到分：库中成交价为 Numeric(10,3)，重放出的
        # avg_cost 会带 3 位小数；不量化则与引擎当次回测写的 positions_json
        # （内存价 2 位小数 → cost_value 到分）不一致。
        out.append(dict(slot, market_value=str(market_value.quantize(_CENT)),
                        cost_value=str(Decimal(slot["cost_value"])
                                       .quantize(_CENT))))
    # raw_sum 回传编排层：Task 4 的「raw_sum<=0 拒绝」门禁与聚合偏离门限都依赖它
    notes["raw_sum"] = raw_sum
    if raw_sum <= 0:
        return out, notes
    if stored_market_value is None or stored_market_value <= 0:
        # 有持仓却无存量市值：跳过缩放并告警，不阻断。
        # 用 >0 而非真值判断：负存量市值会算出负 k，把行业权重写成负数。
        notes["unscaled"] = True
        return out, notes
    k = Decimal(str(stored_market_value)) / raw_sum
    notes["k"] = k
    return [dict(s, market_value=str((Decimal(s["market_value"]) * k)
                                     .quantize(_CENT))) for s in out], notes


def _deviation_stats(k_by_date: Dict[date, Decimal]) -> dict:
    """缩放系数偏离度统计。k_by_date 只含成功缩放的日期子集。"""
    if not k_by_date:
        return {"max_dev": "0", "max_dev_date": None, "mean_dev": "0"}
    devs = {d: abs(k - 1) for d, k in k_by_date.items()}
    worst = max(devs, key=lambda d: devs[d])
    mean = sum(devs.values(), Decimal("0")) / Decimal(len(devs))
    return {"max_dev": str(devs[worst]), "max_dev_date": worst.isoformat(),
            "mean_dev": str(mean)}


def _default_close_provider(codes: List[str], start: date, end: date):
    # batch_size=32：回填只涉及个股，代码数少；分批以防单次请求过大
    return build_index_close_series(codes, start, end, batch_size=32)


def backfill_record(db: Session, record_id: int, force: bool = False,
                    close_provider=None) -> dict:
    """回填一条记录的组合快照 positions_json。返回结果 dict（绝不抛异常）。

    流程：读组合快照 + 成交 → 现金差分校验 → 重放持仓（无 I/O）→ 按实际
    持仓过的代码取日线收盘价 → 逐日估值+缩放 → 门限判定 → 单次 commit 写入。
    任一步失败则一行不写（原子性），返回 status=failed/skipped 及原因。

    门限：
    - 现金硬门限：重放累计现金流与存量 cash 列对不上（容差 0.01）→ 整条失败。
    - raw_sum<=0 异常出口：有持仓但重放市值非正（k=None 且 unscaled=False）
      → 整条失败；空仓（detail 为空）是正常出口，不拒。
    - 缩放门限：成功缩放日的聚合偏离 |Σ存量市值-Σ重放市值|/Σ存量市值 > 5%
      → 跳过不写（force=True 覆盖）。用聚合而非单日最大偏离：单日缺价/结转
      造成的单日漂移不应否决整条记录，系统性价格漂移才会体现在聚合上。
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
    if not force and any(s.positions_json is not None for s in snaps):
        return {"status": "skipped", "record_id": record_id,
                "error": "该记录 positions_json 已回填，跳过（确认需重填可用 "
                          "force=True）",
                "snapshots": len(snaps), "written": False}
    trades = (db.query(BacktestTrade)
              .filter_by(backtest_record_id=record_id)
              .order_by(BacktestTrade.bar_time, BacktestTrade.id).all())
    dates = [s.snap_date for s in snaps]

    # 1) 现金差分校验：与初始资金无关，零漂移，能抓住任何数据错误。
    #    输出 cumflow[首日] 而非直接比 cumflow：首条快照若已含当日成交，
    #    后者会误判为不自洽。恒等式 stored[i] - stored[0] == cumflow[i] - cumflow[0]
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
                "error": "现金重放与快照 cash 列不自洽，拒绝写入",
                "cash_check": cash_check}

    # 2) 重放持仓（无 I/O），按实际持仓过的代码取价——只给这些代码取价
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
    raw_total = Decimal("0")
    stored_total = Decimal("0")
    payloads = []
    for s in snaps:
        detail, notes = value_positions(replayed[s.snap_date], close or {},
                                        s.snap_date, s.market_value)
        # raw_sum<=0 异常出口：有持仓却重不出正市值，疑似脏数据 → 整条拒绝。
        # 空仓（detail 为空）与无存量市值（unscaled=True）都是正常出口，不拒。
        if detail and notes["k"] is None and not notes["unscaled"]:
            return {"status": "failed", "record_id": record_id,
                    "error": "快照 %s 重放市值非正（raw_sum<=0），疑似脏数据，"
                              "拒绝写入" % s.snap_date.isoformat(),
                    "cash_check": cash_check, "written": False}
        carry += notes["carried"]
        fallback += notes["cost_fallback"]
        if notes["k"] is not None:
            k_by_date[s.snap_date] = notes["k"]
            raw_total += notes["raw_sum"]
            stored_total += Decimal(str(s.market_value))
        else:
            no_scale_days += 1
        payloads.append(json.dumps(detail, default=str))

    scale = _deviation_stats(k_by_date)
    scale["no_scale_days"] = no_scale_days
    # 聚合偏离：Σ|存量-重放|/Σ存量，只对成功缩放日求和（无存量市值的日期按设计跳过）
    agg_dev = (abs(stored_total - raw_total) / stored_total
               if stored_total > 0 else Decimal("0"))
    scale["agg_dev"] = str(agg_dev)
    report = {"status": "ok", "record_id": record_id, "snapshots": len(snaps),
              "held_codes": len(held_codes), "missing_price_codes": missing,
              "cash_check": cash_check, "scale": scale,
              "carry_forward_days": carry, "avg_cost_fallback": fallback,
              "written": False, "error": None}
    if agg_dev > SCALE_TOLERANCE and not force:
        report["status"] = "skipped"
        report["error"] = (
            "重建市值与存量市值聚合偏离 %.2f%% 超过 %.0f%% 门限，拒绝写入"
            "（确认无误可用 force 覆盖）"
            % (float(agg_dev) * 100, float(SCALE_TOLERANCE) * 100))
        return report

    # 4) 单次 commit：不允许部分写入
    for s, payload in zip(snaps, payloads):
        s.positions_json = payload
    db.commit()
    report["written"] = True
    return report
