"""hermes 对外信号服务（0017）：跨 session 按日查询 live_signals。

另一台电脑的 hermes 程序经 GET /api/hermes/signals 拉取当日信号 JSON，
由用户在 hermes 侧终端手动执行买卖——本系统不下单、不接收回传。
"""
from datetime import date as date_cls, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from core.engine.live.timing import now_shanghai
from core.models import LiveSession, LiveSignal
from core.services.live_service import serialize_signal


def daily_signals(
    db: Session, day: Optional[date_cls] = None,
    portfolio_id: Optional[int] = None, strategy_id: Optional[int] = None,
    signal_type: Optional[str] = None, status: Optional[str] = None,
    limit: int = 500, offset: int = 0,
) -> dict:
    day = day or now_shanghai().date()
    start = datetime(day.year, day.month, day.day)
    end = start + timedelta(days=1)
    q = (db.query(LiveSignal, LiveSession)
         .join(LiveSession, LiveSignal.live_session_id == LiveSession.id)
         .filter(LiveSignal.bar_time >= start, LiveSignal.bar_time < end))
    if portfolio_id is not None:
        q = q.filter(LiveSignal.portfolio_strategy_id == portfolio_id)
    if strategy_id is not None:
        q = q.filter(LiveSignal.strategy_id == strategy_id)
    if signal_type:
        q = q.filter(LiveSignal.signal_type == signal_type)
    if status:
        q = q.filter(LiveSignal.status == status)
    total = q.count()
    rows = (q.order_by(LiveSignal.bar_time.desc(), LiveSignal.id.desc())
            .offset(offset).limit(min(max(limit, 1), 500)).all())
    items = []
    for s, sess in rows:
        item = serialize_signal(s)
        item["session_id"] = s.live_session_id
        item["session_name"] = sess.name
        item["session_mode"] = sess.mode
        item["session_status"] = sess.status
        items.append(item)
    return {"date": day.isoformat(), "items": items, "total": total}
