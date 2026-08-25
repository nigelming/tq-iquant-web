"""EventBus — LiveEngine 的 SSE 事件多播总线（0010 步骤 1，从 live_engine 抽出）。

职责（与 HTTP/SSE 传输无关的纯进程内多播）：
- 维护订阅队列集合（每个 /stream 连接一个 asyncio.Queue）
- emit(event_type, payload) 向所有订阅者广播 {type, **payload}
- 跨线程安全：worker 线程（_loop/_deals_loop tick 内）emit 时经
  call_soon_threadsafe 回到事件线程投递（asyncio.Queue 非线程安全）
- stream() 作为异步迭代器 yield 事件，空闲超 ping 间隔 yield ping 心跳

LiveEngine 持有一个 EventBus 实例，自身的 _emit/stream_events 保留为薄委托
（测试与 live.py 仍经引擎公共接口使用）。
"""
import asyncio
from datetime import datetime
from typing import AsyncIterator, Callable, List, Optional

from .timing import now_shanghai


class EventBus:
    def __init__(
        self,
        ping_interval: float = 30.0,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        # 流空闲无事件时的 ping 心跳间隔（design §5.6.10，30s）。
        self._ping_interval = ping_interval
        # 可注入时钟（默认上海墙钟）；ping 时间戳与测试时间控制用。
        self._clock = clock or now_shanghai
        # 当前已连接客户端的订阅队列；emit 向各队列 put_nowait 广播。
        self._subscribers: List["asyncio.Queue"] = []
        # start() 时捕获运行中的事件循环；worker 线程内 emit 经
        # call_soon_threadsafe 回投。None=尚未 start（同步直调路径直接 put_nowait）。
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------ 状态挂钩
    @property
    def subscribers(self) -> List["asyncio.Queue"]:
        """订阅队列列表（LiveEngine 暴露 _stream_subscribers 兼容测试直连）。"""
        return self._subscribers

    @property
    def ping_interval(self) -> float:
        return self._ping_interval

    @ping_interval.setter
    def ping_interval(self, value: float) -> None:
        self._ping_interval = value

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """引擎 start() 时捕获事件循环。"""
        self._loop = loop

    def clear_loop(self) -> None:
        """引擎 stop() 后清空 loop 引用；之后同步直调走「无 loop」直接投递。"""
        self._loop = None

    # ------------------------------------------------------------------ 广播
    def emit(self, event_type: str, payload: dict) -> None:
        """向所有 SSE 订阅队列广播 {type, **payload}（signal/order/trade/position/risk）。

        线程安全（审计 #3）：emit 可能从事件线程（stream 消费侧、同步测试直调）
        或 worker 线程（_loop/_deals_loop tick 内 _handle_bar/_backfill_order）被调。
        asyncio.Queue.put_nowait 非线程安全 → worker 线程内经 call_soon_threadsafe
        回到事件线程投递；事件线程内（或未 start、loop=None 的同步路径）直接
        put_nowait（保留既有同步测试 get_nowait 立即可见的语义）。
        队列积压（订阅端消费慢）→ 丢弃该事件（EventSource 断线重连后见最新状态，
        design §5.6.10：浏览器自动重连，无需重放）。
        """
        ev = {"type": event_type, **payload}
        loop = self._loop
        if loop is None:
            # 未 start（同步测试 / start 前）：直接投递
            self._emit_to_subscribers(ev)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            # 事件线程内：直接投递
            self._emit_to_subscribers(ev)
        else:
            # worker 线程内：回到事件线程投递（asyncio.Queue 非线程安全）
            loop.call_soon_threadsafe(self._emit_to_subscribers, ev)

    def _emit_to_subscribers(self, ev: dict) -> None:
        """实际向各订阅队列 put_nowait（仅在事件线程执行）。积压 → 丢弃不阻塞。"""
        for q in list(self._subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass  # 积压丢弃，不阻塞引擎

    async def stream(self, is_running: Callable[[], bool]) -> AsyncIterator[dict]:
        """SSE 事件流：订阅事件队列逐条 yield；空闲超 ping 间隔 yield ping 心跳。

        每个 /stream 连接调用一次（/stream 端点 async for 消费）。连接结束（aclose）
        → finally 退订。引擎停止（is_running() 返回 False）→ 队列空则流结束
        （端点侧转 ping-only）。运行态以引擎为准，故以回调注入而非 bus 自存。
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.append(q)
        try:
            while is_running() or not q.empty():
                try:
                    ev = await asyncio.wait_for(
                        q.get(), timeout=self._ping_interval
                    )
                except asyncio.TimeoutError:
                    ev = {"type": "ping", "time": self._clock().isoformat()}
                yield ev
        finally:
            if q in self._subscribers:
                self._subscribers.remove(q)
