"""core.engine.live — LiveEngine 的内部协作者子包（0010 引擎分解）。

从 live_engine.py 逐步抽出的协作者：
- timing：无状态时间/周期/数值工具（now_shanghai/periods_on_boundary 等，步骤 0）
- event_bus.EventBus：SSE 事件多播 + ping 心跳（步骤 1）
- context.EngineContext：集中持有共享可变状态 + clock + db 工厂 + dispatcher（步骤 2a）
- market_data.MarketDataService：bar 缓存 / 信号求值 / 周期 bar 分发（步骤 2b）
- order_machine.OrderStateMachine：委托状态机 / 成交回填 / 在途门 / T+1（步骤 3）
- breaker.BreakerService：熔断编排 + 副作用（计数持久化/risk 事件/手动恢复，步骤 4）
- daily_closer.DailyCloser：日终/收盘三件套的时点判断（步骤 5）
- calendar.TradingCalendar：交易日历与交易总闸（live 专属，0010 后续迁入）

这些是 LiveEngine 的内部实现细节，外部代码仍应经
`core.engine.LiveEngine` / `core.engine.live_engine.LiveEngine` 使用引擎，
不直接依赖本子包（calendar 旧路径 core.engine.trading_calendar 保留 re-export shim）。
"""
