"""hermes 对外接口（0017）：另一台电脑按日拉取实盘信号（无鉴权，局域网）。

统一响应 envelope（core.api.response）；无鉴权为用户确认的决策——
后端绑 0.0.0.0 后依赖局域网隔离。
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.api.response import err, ok
from core.db import get_db
from core.engine.live.timing import now_shanghai
from core.services import hermes_service

router = APIRouter(prefix="/api/hermes", tags=["hermes"])


@router.get("/health")
def health():
    return ok({"ok": True, "date": now_shanghai().date().isoformat()})


@router.get("/signals")
def signals(
    date: Optional[str] = None,
    portfolio_id: Optional[int] = None,
    strategy_id: Optional[int] = None,
    signal_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 500,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    day = None
    if date:
        try:
            day = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            return err(400, "date 必须为 YYYY-MM-DD")
    return ok(hermes_service.daily_signals(
        db, day, portfolio_id=portfolio_id, strategy_id=strategy_id,
        signal_type=signal_type, status=status, limit=limit, offset=offset,
    ))
