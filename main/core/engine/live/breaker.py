"""BreakerService — 熔断编排 + 副作用（0010 步骤 4，纯搬移）。

风控分两层：
- 规则/状态在 ``risk_manager.py::PortfolioRiskManager``（回测实盘共用，不动）；
- 编排 + 副作用（每 bar 推进峰值/回撤、日终推进日内亏损、计数持久化、3 次转手动、
  手动恢复、重启读回、risk 事件发射）原本散在 LiveEngine，本步收敛到此处。

边界（重要）：
- 本服务**单向依赖** risk_manager（调 update_peak/update_daily、读标志位），不反向。
- portfolio.py 里"熔断期剥 BUY 留 SELL"是回测实盘共用规则，**不搬**。
- 触发时点不归本服务：on_bar 节拍由引擎调 on_bar_update，14:30 由日终逻辑调
  on_daily_update——本服务只管"推进 + 副作用"，不管"什么时候"。
- 本服务不反向 import LiveEngine；经 EngineContext 读 session_id/portfolios/db 工厂，
  经注入的 emit 回调发射 SSE risk 事件。

搬移自 live_engine.py（commit 前版本）：
- _handle_bar 内 update_peak + max_drawdown 次日自动恢复检测 + _persist_breaker_count
- _persist_breaker_count（H4 计数落库 + 3 次转 circuit_broken + 触发幅度日志）
- recover_breaker（公共手动恢复入口）
- _maybe_daily_close 内 per-portfolio 的 update_daily + daily_loss 触发/恢复
- recover 内读回 LiveSessionPortfolio.circuit_breaker_count 的循环

逻辑原样保留，仅 self.xxx → self._ctx.xxx、self._emit → 注入回调、
self._breaker_count_written → self.counts_written。
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Callable, Dict

from sqlalchemy.orm import Session

from ...models import LiveSessionPortfolio
from ..portfolio import Portfolio
from .context import EngineContext

logger = logging.getLogger("core.engine.live.breaker")


class BreakerService:
    """熔断编排 + 副作用：推进 risk_manager 状态并落库/发事件。"""

    def __init__(
        self,
        ctx: EngineContext,
        emit: Callable[[str, dict], None],
    ) -> None:
        self._ctx = ctx
        self._emit = emit
        # D4/H4：每组合已持久化的熔断计数（LiveSessionPortfolio.circuit_breaker_count）。
        # recover 预置当前值；persist_count 只在计数变化时落库（避免每 bar 写）。
        # key=portfolio.portfolio_id。
        self.counts_written: Dict[int, int] = {}

    # ---------------- 每 bar：max_drawdown ----------------
    def on_bar_update(
        self, portfolio: Portfolio, total_value: Decimal, bar_date: date
    ) -> None:
        """每 bar 推进峰值/回撤 + max_drawdown 次日自动恢复检测 + 计数持久化。

        搬自 _handle_bar：分钟级 prev_close 只在跨日刷新（不误触 daily_loss）；
        update_peak 可能触发 max_drawdown（计数+1），随后 persist_count 落库。
        max_drawdown 次日自动恢复（非手动恢复）此前在 risk_manager 内静默置 False，
        这里补日志。
        """
        rm = portfolio.risk_manager
        was_broken = rm.circuit_breaker_active
        was_manual = rm.manual_recovery
        rm.update_peak(total_value, bar_date)
        # max_drawdown 次日自动恢复（非手动恢复）此前在 risk_manager 内静默置 False，这里补日志
        if was_broken and not rm.circuit_breaker_active and not was_manual:
            logger.info(
                "circuit breaker: portfolio %s max_drawdown 次日自动恢复 "
                "(session %s, %s)",
                portfolio.portfolio_id, self._ctx.session_id, bar_date,
            )
        # H4：熔断计数持久化——update_peak 可能触发 max_drawdown（计数+1），计数变化才落库
        self.persist_count(portfolio, total_value)

    def persist_count(
        self, portfolio: Portfolio, total_value: Decimal = None
    ) -> None:
        """H4：把组合 max_drawdown 累计触发次数持久化到 LiveSessionPortfolio.circuit_breaker_count。

        每 bar update_peak 后比对：计数未变则不落库（避免每 bar 写）；变化（熔断触发 / 达 3 次
        转手动）→ 写 count，达 3 次 status 转 circuit_broken（design §8.3）。找不到 link
        （组合未关联本 session）→ 跳过。写库失败不阻断交易，记日志。

        total_value：触发时的组合总市值（来自 _handle_bar），仅用于把 total/peak/回撤幅度
        打进触发日志，便于事后核算（不参与落库逻辑）。None 时日志退化为不带幅度。
        """
        rm = portfolio.risk_manager
        count = rm.consecutive_drawdown_triggers
        old = self.counts_written.get(portfolio.portfolio_id)
        if old == count:
            return
        # B5：计数递增（max_drawdown 熔断刚触发）→ 推送风控事件（首 bar old=None 不推）
        if old is not None and count > old:
            # 触发幅度（可观测性）：total/peak/回撤%/阈值。peak 在 update_peak 内是先抬峰再
            # 判回撤，触发那根 bar 的 peak 即触发热值；drawdown=(peak-total)/peak。
            if total_value is not None and rm.peak_value > 0:
                dd_pct = (rm.peak_value - total_value) / rm.peak_value * 100
                detail = (
                    " total=%s peak=%s drawdown=%.2f%% threshold=%s"
                    % (total_value, rm.peak_value, dd_pct, rm.max_drawdown)
                )
            else:
                detail = ""
            self._emit("risk", {
                "portfolio_id": portfolio.portfolio_id,
                "rule": "max_drawdown",
                "triggered": True,
                "count": count,
                "total_value": str(total_value) if total_value is not None else None,
                "peak_value": str(rm.peak_value),
                "drawdown_pct": (
                    float((rm.peak_value - total_value) / rm.peak_value)
                    if total_value is not None and rm.peak_value > 0 else None
                ),
                "message": "最大回撤熔断触发（累计 %d 次）" % count,
            })
            if count >= 3:
                logger.warning(
                    "circuit breaker: portfolio %s max_drawdown 触发%s "
                    "(累计 %d 次) → 转手动恢复，停新开仓等人工介入 (session %s)",
                    portfolio.portfolio_id, detail, count, self._ctx.session_id,
                )
            else:
                logger.warning(
                    "circuit breaker: portfolio %s max_drawdown 触发%s "
                    "(累计 %d 次，次日自动恢复) (session %s)",
                    portfolio.portfolio_id, detail, count, self._ctx.session_id,
                )
        db = self._ctx.db_session_factory()
        try:
            link = (
                db.query(LiveSessionPortfolio)
                .filter_by(
                    session_id=self._ctx.session_id,
                    portfolio_strategy_id=portfolio.portfolio_id,
                )
                .first()
            )
            if link is None:
                self.counts_written[portfolio.portfolio_id] = count
                return
            link.circuit_breaker_count = count
            if count >= 3:
                link.status = "circuit_broken"
            db.commit()
            self.counts_written[portfolio.portfolio_id] = count
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("persist breaker count error (portfolio %s)", portfolio.portfolio_id)
        finally:
            db.close()

    # ---------------- 手动恢复（公共入口）----------------
    def recover(self, portfolio_id: int) -> bool:
        """手动恢复某组合熔断：清零计数 + 解除手动恢复 + 落库 status=active。

        3 次转手动恢复（§8.3）的人工恢复入口。重置内存态 + DB LiveSessionPortfolio。
        peak_value 保持当前值（不重置历史峰值，回撤基准不变）。
        返回 True 若找到组合并恢复，False 若组合不属本 session。
        """
        port = next((p for p in self._ctx.portfolios if p.portfolio_id == portfolio_id), None)
        if port is None:
            return False
        rm = port.risk_manager
        rm.consecutive_drawdown_triggers = 0
        rm.circuit_breaker_active = False
        rm.manual_recovery = False
        rm.breaker_trigger_date = None
        # 同步已落库计数，否则下一 bar persist_count 比对 old==count 跳过回写
        self.counts_written[portfolio_id] = 0
        db = self._ctx.db_session_factory()
        try:
            link = db.query(LiveSessionPortfolio).filter_by(
                session_id=self._ctx.session_id,
                portfolio_strategy_id=portfolio_id,
            ).first()
            if link is not None:
                link.circuit_breaker_count = 0
                link.status = "active"
                db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("recover_breaker persist failed: portfolio %s", portfolio_id)
        finally:
            db.close()
        self._emit("risk", {
            "portfolio_id": portfolio_id, "rule": "max_drawdown",
            "triggered": False, "count": 0, "message": "熔断已手动恢复（计数清零）",
        })
        logger.info(
            "circuit breaker: portfolio %s 手动恢复（计数清零，解除停新开仓）(session %s)",
            portfolio_id, self._ctx.session_id,
        )
        return True

    # ---------------- 重启读回计数 ----------------
    def restore_counts(self, db: Session, ports_by_id: Dict[int, Portfolio]) -> None:
        """D4：recover 时读回 LiveSessionPortfolio.circuit_breaker_count——重启不丢累计次数。

        达 3 次 → 转手动恢复（manual_recovery + circuit_breaker_active=True 停新开仓等待人工，
        同 status=circuit_broken 语义）；<3 次的单日熔断当天已恢复，重启不补挂（单一计数模型，
        可接受）。预置 counts_written 避免首 bar 重复落库。
        """
        links = (
            db.query(LiveSessionPortfolio)
            .filter(LiveSessionPortfolio.session_id == self._ctx.session_id)
            .all()
        )
        for link in links:
            port = ports_by_id.get(link.portfolio_strategy_id)
            if port is None or not link.circuit_breaker_count:
                continue
            port.risk_manager.consecutive_drawdown_triggers = link.circuit_breaker_count
            self.counts_written[link.portfolio_strategy_id] = link.circuit_breaker_count
            if link.circuit_breaker_count >= 3:
                port.risk_manager.manual_recovery = True
                port.risk_manager.circuit_breaker_active = True
                logger.warning(
                    "circuit breaker: portfolio %s 重启读回累计 %d 次 → 转手动恢复，"
                    "停新开仓等人工介入 (session %s)",
                    link.portfolio_strategy_id, link.circuit_breaker_count, self._ctx.session_id,
                )
            else:
                logger.info(
                    "circuit breaker: portfolio %s 重启读回累计 %d 次（未转手动，正常运行）"
                    " (session %s)",
                    link.portfolio_strategy_id, link.circuit_breaker_count, self._ctx.session_id,
                )

    # ---------------- 日终（14:30）：daily_loss ----------------
    def on_daily_update(self, portfolio: Portfolio, today: date) -> None:
        """日终对单个组合推进日内亏损：update_daily + daily_loss 触发/次日恢复 + risk 事件。

        搬自 _maybe_daily_close 的 per-portfolio 循环体。日终总市值用组合最新现金 + 持仓市值
        （无最新 bar 时以当前持仓成本近似；有 bar 由 on_bar_update(update_peak) 已覆盖）。
        """
        try:
            # 日终总市值：用组合最新现金 + 持仓市值（无最新 bar 时以当前持仓市值近似）
            total = portfolio.account.cash
            for ctx in portfolio.strategies:
                for stock_code, pos in ctx.positions.items():
                    if pos.quantity == 0:
                        continue
                    # 用持仓成本计市值作为日终基准近似（无 bar close 时；有 bar 由 update_peak 已覆盖）
                    total += pos.avg_cost * pos.quantity
            was_paused = portfolio.risk_manager.daily_pause_active
            portfolio.risk_manager.update_daily(
                total, today, portfolio.account.initial_capital
            )
            now_paused = portfolio.risk_manager.daily_pause_active
            # B5：日内亏损熔断刚触发（daily_pause_active 变 True）→ 推送风控事件
            if now_paused and not was_paused:
                # 触发幅度（可观测性）：日终市值/昨收/日内盈亏/亏损%/阈值。
                dpm = portfolio.risk_manager
                prev = dpm.prev_close_value
                init_cap = portfolio.account.initial_capital
                if prev is not None and init_cap > 0:
                    pnl = total - prev
                    loss_pct = abs(pnl) / init_cap * 100
                    detail = (
                        " total=%s prev_close=%s pnl=%s loss=%.2f%% threshold=%s"
                        % (total, prev, pnl, loss_pct, dpm.daily_loss_limit)
                    )
                else:
                    detail = ""
                self._emit("risk", {
                    "portfolio_id": portfolio.portfolio_id,
                    "rule": "daily_loss",
                    "triggered": True,
                    "total_value": str(total),
                    "prev_close_value": str(prev) if prev is not None else None,
                    "message": "日内亏损熔断触发，当日暂停新开仓",
                })
                logger.warning(
                    "circuit breaker: portfolio %s daily_loss 触发%s "
                    "on %s (当日暂停新开仓，次日自动恢复) (session %s)",
                    portfolio.portfolio_id, detail, today, self._ctx.session_id,
                )
            elif was_paused and not now_paused:
                logger.info(
                    "circuit breaker: portfolio %s daily_loss 次日自动恢复 "
                    "on %s (session %s)",
                    portfolio.portfolio_id, today, self._ctx.session_id,
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "update_daily error (portfolio %s, date %s)",
                portfolio.portfolio_id, today,
            )
