"""core.engine.live — LiveEngine 的内部协作者子包（0010 引擎分解）。

从 live_engine.py 逐步抽出的协作者：
- timing：无状态时间/周期/数值工具（now_shanghai/periods_on_boundary 等，步骤 0）
- event_bus.EventBus：SSE 事件多播 + ping 心跳（步骤 1）
- 后续：market_data / order_machine / breaker / daily_closer

这些是 LiveEngine 的内部实现细节，外部代码仍应经
`core.engine.LiveEngine` / `core.engine.live_engine.LiveEngine` 使用引擎，
不直接依赖本子包。
"""
