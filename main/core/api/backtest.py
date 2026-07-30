from datetime import datetime, date
from decimal import Decimal
from typing import Dict, Optional

import polars as pl
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.db import get_db
from core.models import (
    PortfolioStrategy, Strategy, FormulaSignal,
    BacktestRecord, BacktestTrade, BacktestDailySnapshot, BacktestEvaluation,
)
from core.engine.portfolio import Portfolio
from core.engine.strategy_context import StrategyContext
from core.engine.risk_manager import StrategyRiskManager, PortfolioRiskManager
from core.engine.backtest_engine import BacktestEngine
from tq_iquant_shared.constants import SignalType

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class BacktestRequest(BaseModel):
    portfolio_strategy_id: int
    name: str
    start_date: date
    end_date: date


# ---------------------------------------------------------------------------
# 数据获取层（TQ 对接留后续切片，当前为桩；测试 monkeypatch 注入 mock 数据）
# ---------------------------------------------------------------------------
def build_klines(ps: PortfolioStrategy, start: date, end: date) -> dict:
    """从 TQ 取历史 K 线：{stock_code: {period: pl.DataFrame}}。"""
    return {}


def build_signal_cache(ps: PortfolioStrategy, klines: dict) -> dict:
    """预计算公式信号：{(strategy_id, stock_code, bar_time): [{name, value}]}。"""
    return {}


def build_open_prices(ps: PortfolioStrategy, klines: dict) -> dict:
    """构造 open 价表：{stock_code: {bar_time: Decimal}}。"""
    return {}


# ---------------------------------------------------------------------------
# 组装 Portfolio
# ---------------------------------------------------------------------------
def _signal_type_from_str(s: str) -> SignalType:
    for st in SignalType:
        if st.value == s:
            return st
    raise ValueError(f"unknown signal_type: {s}")


def _assemble_portfolio(ps: PortfolioStrategy, strategies: list, db: Session) -> Portfolio:
    pm = PortfolioRiskManager(
        max_drawdown=Decimal(str(ps.max_drawdown)),
        daily_loss_limit=Decimal(str(ps.daily_loss_limit)),
    )
    port = Portfolio(
        portfolio_id=ps.id,
        initial_capital=Decimal(str(ps.initial_capital)),
        risk_manager=pm,
    )
    for strat in strategies:
        ctx = StrategyContext(
            strategy_id=strat.id,
            period=strat.period,
            capital_ratio=Decimal(str(strat.capital_ratio)),
            max_positions=strat.max_positions,
        )
        # 从 formula_signals 表读信号配置
        sigs = db.query(FormulaSignal).filter_by(formula_id=strat.formula_id).all()
        ctx.formula_signals = [
            {
                "signal_name": s.signal_name,
                "signal_type": _signal_type_from_str(s.signal_type),
                "trigger_value": s.trigger_value,
            }
            for s in sigs
        ]
        ctx.strategy_risk = StrategyRiskManager(
            stop_loss_ratio=Decimal(str(strat.stop_loss_ratio)),
            take_profit_ratio=Decimal(str(strat.take_profit_ratio)),
            trailing_stop_ratio=Decimal(str(strat.trailing_stop_ratio)),
        )
        port.strategies.append(ctx)
    return port


# ---------------------------------------------------------------------------
# 持久化引擎产出
# ---------------------------------------------------------------------------
def _persist_result(
    db: Session, record_id: int, ps_id: int, result: dict, strategies: list
) -> None:
    strat_by_id = {s.id: s for s in strategies}
    for trade in result["trades"]:
        db.add(BacktestTrade(
            backtest_record_id=record_id,
            strategy_id=trade.strategy_id,
            formula_signal_id=None,
            signal_name="",
            signal_type=trade.signal_type.value,
            stock_code=trade.stock_code,
            trade_type=trade.trade_type.value,
            price=trade.price,
            quantity=trade.quantity,
            amount=trade.amount,
            commission=trade.commission,
            stamp_duty=trade.stamp_duty,
            bar_time=trade.trade_time,
        ))
    for snap in result["snapshots"]:
        db.add(BacktestDailySnapshot(
            backtest_record_id=record_id,
            target_type="portfolio",
            target_id=ps_id,
            snap_date=snap["snap_date"],
            total_value=snap["total_value"],
            cash=snap["cash"],
            market_value=snap.get("market_value", Decimal("0")),
            daily_return=snap.get("daily_return"),
            cumulative_return=snap.get("cumulative_return"),
            benchmark_value=snap.get("benchmark_value"),
        ))
    ev = result.get("evaluations") or {}
    db.add(BacktestEvaluation(
        backtest_record_id=record_id,
        target_type="portfolio",
        target_id=ps_id,
        total_return=ev.get("total_return"),
        annual_return=ev.get("annual_return"),
        max_drawdown=ev.get("max_drawdown"),
        volatility=ev.get("volatility"),
        sharpe_ratio=ev.get("sharpe_ratio"),
        sortino_ratio=ev.get("sortino_ratio"),
        calmar_ratio=ev.get("calmar_ratio"),
        win_rate=ev.get("win_rate"),
        profit_factor=ev.get("profit_factor"),
        total_trades=ev.get("total_trades"),
        benchmark_return=ev.get("benchmark_return"),
        avg_holding_days=ev.get("avg_holding_days"),
        var_95=ev.get("var_95"),
        cvar_95=ev.get("cvar_95"),
        avg_recovery_days=ev.get("avg_recovery_days"),
        max_recovery_days=ev.get("max_recovery_days"),
        ulcer_index=ev.get("ulcer_index"),
        return_stability=ev.get("return_stability"),
    ))


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.get("/records")
def list_records(db: Session = Depends(get_db)):
    return {"code": 0, "data": []}


@router.post("")
def run_backtest(req: BacktestRequest, db: Session = Depends(get_db)):
    ps = db.get(PortfolioStrategy, req.portfolio_strategy_id)
    if ps is None:
        raise HTTPException(status_code=404, detail="portfolio strategy not found")
    strategies = (
        db.query(Strategy)
        .filter_by(portfolio_id=ps.id)
        .all()
    )

    # 写 record（running）
    rec = BacktestRecord(
        portfolio_strategy_id=ps.id,
        name=req.name,
        start_date=req.start_date,
        end_date=req.end_date,
        status="running",
        progress=0,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    record_id = rec.id

    try:
        portfolio = _assemble_portfolio(ps, strategies, db)
        klines = build_klines(ps, req.start_date, req.end_date)
        signal_cache = build_signal_cache(ps, klines)
        open_prices = build_open_prices(ps, klines)

        def on_progress(p: int):
            rec.progress = p
            db.commit()

        engine = BacktestEngine()
        result = engine.run(
            portfolio,
            klines=klines,
            signal_cache=signal_cache,
            open_prices=open_prices,
            progress_callback=on_progress,
        )

        _persist_result(db, record_id, ps.id, result, strategies)

        rec.status = "completed"
        rec.progress = 100
        rec.completed_at = datetime.now()
        db.commit()
    except Exception as e:
        rec.status = "failed"
        rec.error_message = str(e)
        db.commit()
        raise

    return {
        "code": 0,
        "message": "ok",
        "data": {
            "record_id": record_id,
            "trades_count": len(result["trades"]),
            "snapshots_count": len(result["snapshots"]),
            "evaluations": result.get("evaluations") or {},
        },
    }
