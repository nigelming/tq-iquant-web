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
