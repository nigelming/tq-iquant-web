"""。"""
import logging
from datetime import datetime
from typing import Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from core.models import LiveSignal
from core.tq.alert import WarnItem
from core.engine.live.timing import now_shanghai

logger = logging.getLogger(__name__)


class SignalMonitor:
    def __init__(
        self, session_id: int, db_session_factory: Callable[[], Session],
        alert, emit: Callable[[str, dict], None], execution_mode: str,
        clock: Callable[[], datetime] = now_shanghai,
        send_warn_enabled: bool = True,
    ):
        self.session_id = session_id
        self._db = db_session_factory
        self._alert = alert
        self._emit = emit
        self.execution_mode = execution_mode
        self.clock = clock
        # send_warn 开关（live.alert_send_warn_enabled，0017）：False=信号只落库
        # +发 SSE，不推通达信预警。组件默认 True 保留历史行为，装配层注入配置值。
        self.send_warn_enabled = send_warn_enabled
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
        prev_values: Dict[tuple, Optional[int]] = {}
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
                        # 记录推送前的水位：send_warn 失败时回滚，下轮重试
                        prev_values[key] = previous
                    self._last_value[key] = current
        if candidates:
            if not self._persist_and_alert(candidates):
                # 推送失败（alert_status=failed）：恢复推送前水位，让同电平
                # 信号下一轮重新进入候选重试（DB 层按 bar_time 幂等去重不变，
                # 脉冲信号天然重触发不受影响）。
                for key, prev in prev_values.items():
                    self._last_value[key] = prev

    def _persist_and_alert(self, candidates: list) -> bool:
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
                return True
            if not self.send_warn_enabled:
                # send_warn 关闭（0017 hermes 信号模式）：信号只落库 + 发 SSE，
                # status 停留 new、alert_status 空。必须返回 True——返回 False
                # 会恢复推送前水位，导致同电平信号每轮重复进入候选（虽有 DB
                # 幂等兜底，但徒增无效循环）。
                for r in rows:
                    self._emit_signal(r)
                return True
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
                self._emit_signal(r)
            return bool(result.get("ok"))
        except Exception:
            db.rollback()
            logger.exception("persist live signals failed")
            return False
        finally:
            db.close()

    def _emit_signal(self, r) -> None:
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
