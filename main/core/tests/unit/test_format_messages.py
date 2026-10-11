"""风险/熔断决策事件 message 数值精度（显示层）。"""
import re
from decimal import Decimal
from datetime import date

from core.engine.risk_manager import PortfolioRiskManager


class _ListRecorder:
    def __init__(self):
        self.events = []

    def record(self, **kw):
        self.events.append(kw)


def _mk(rec):
    rm = PortfolioRiskManager(
        max_drawdown=Decimal("0.20"), daily_loss_limit=Decimal("0.05"),
    )
    rm.recorder = rec
    return rm


def test_daily_loss_message_has_no_long_decimal():
    rec = _ListRecorder()
    rm = _mk(rec)
    initial = Decimal("1000000")
    rm.update_peak(Decimal("1369882.7275"), date(2014, 2, 24))        # 首日收盘
    rm.update_peak(Decimal("1369882.7275"), date(2014, 2, 25))        # 跨日：prev_close
    rm.update_peak(Decimal("1304160.2587"), date(2014, 2, 25))        # 当日最后一 bar 收盘
    rm.update_daily(Decimal("1304160.2587"), date(2014, 2, 25), initial)  # pnl≈-65722.47(6.57%) → 暂停
    msgs = [e.get("message", "") for e in rec.events if e.get("gate") == "daily_loss"]
    assert msgs, "应产生 daily_loss 事件"
    joined = " ".join(msgs)
    assert not re.search(r"\.\d{4,}", joined), joined
    assert "6.57%" in joined


def test_max_drawdown_message_has_no_long_decimal():
    rec = _ListRecorder()
    rm = _mk(rec)
    rm.update_peak(Decimal("1369882.7275"), date(2014, 2, 24))
    rm.update_peak(Decimal("1070000.2587"), date(2014, 2, 25))      # 回撤 21.9% → 熔断
    msgs = [e.get("message", "") for e in rec.events if e.get("gate") == "max_drawdown"]
    assert msgs, "应产生 max_drawdown 事件"
    joined = " ".join(msgs)
    assert not re.search(r"\.\d{4,}", joined), joined