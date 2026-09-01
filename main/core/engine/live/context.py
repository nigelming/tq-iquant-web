"""EngineContext — LiveEngine 协作者的共享状态容器（0010 步骤 2a）。

后续协作者（MarketDataService/OrderStateMachine/BreakerService/DailyCloser）都需要
读写引擎运行态，但不能各自抓整个 LiveEngine（会反向依赖、难单测）。EngineContext
是它们之间唯一的共享状态通道：一个窄的数据容器，不含业务逻辑。

本步为纯搬移：把 LiveEngine.__init__ 里本就存在的字段收拢到此，引擎上的同名属性
保留为 property 委托（self.ctx.xxx），调用点与测试无需改动。clock 默认可注入
（默认 now_shanghai），供后续协作者测试 patch 时间，不再依赖 monkeypatch
live_engine.datetime。

注意：positions 不归此容器——实盘持仓挂在各 StrategyContext.positions 上（与回测
一致），引擎层本就没有 self.positions 聚合字典；协作者经 portfolios 遍历访问。
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from ..portfolio import Portfolio
from ..http_bridge_dispatcher import HttpBridgeDispatcher
from .timing import now_shanghai


class EngineContext:
    """集中持有 LiveEngine 协作者共享的运行态。

    字段均为可变引用：协作者读状态、经此容器暴露的字段改状态。属性刻意保持公开
    （无 getter/setter 包装），让 property 委托的 LiveEngine 与测试的直接赋值都
    穿透到同一份对象。
    """

    def __init__(
        self,
        session_id: int,
        portfolios: List[Portfolio],
        dispatcher: HttpBridgeDispatcher,
        db_session_factory: Callable[[], Session],
        code_period_count: Optional[Dict[tuple, int]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.session_id: int = session_id
        self.portfolios: List[Portfolio] = portfolios
        self.dispatcher: HttpBridgeDispatcher = dispatcher
        self.db_session_factory: Callable[[], Session] = db_session_factory
        # (code, period) -> 预热/分发最大 count（见 live_engine 中说明）
        self.code_period_count: Dict[tuple, int] = code_period_count or {}
        # 可注入时钟：默认上海时区当前时间；协作者统一经 self.ctx.clock() 取"现在"，
        # 测试注入固定时钟即可，不再 monkeypatch 模块级 datetime。
        self.clock: Callable[[], datetime] = clock or now_shanghai
