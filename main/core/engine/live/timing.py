"""live 引擎的时间/周期/数值工具（从 live_engine.py 抽出，步骤 0/0010）。

这些是无状态模块级函数，与 LiveEngine 实例无关，归 live 协作者子包内部复用
（event_bus/market_data/order_machine/breaker/daily_closer）。
live_engine.py 通过 `from .live.timing import ...` re-export 其中被测试直接导入的
符号（now_shanghai/periods_on_boundary/_CST），保持 `core.engine.live_engine`
命名空间路径不变。
"""
import math
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import List, Optional

# Asia/Shanghai 固定时区——实盘日终 (14:30) 判定/时间记录按上海时间，
# 不依赖本机时区（Core 部署 UTC 服务器时本机 14:30 ≠ 上海 14:30，日终会哑火）。
_CST = timezone(timedelta(hours=8))


def _parse_insert_utc(insert_date, insert_time):
    """桥 /orders 的 insert_date(YYYYMMDD)+insert_time(HHMMSS，上海本地) → UTC naive。

    无法解析返回 None。桥 insert 时间是 Asia/Shanghai 本地 naive，Core created_at 是
    UTC naive，比较前需换算。取前 6 位（兼容 HHMMSSsss 毫秒后缀）。
    """
    ds = str(insert_date or "").strip()
    ts = str(insert_time or "").strip()
    if len(ds) != 8 or len(ts) < 6:
        return None
    try:
        local_naive = datetime.strptime(ds + ts[:6], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return local_naive - timedelta(hours=8)  # Asia/Shanghai → UTC


def now_shanghai() -> datetime:
    """当前上海时间（naive，已剥时区）。实盘固有时点判定专用。

    naive 与引擎内其余 datetime（均 naive）比较一致；aware 与 naive 比较会抛 TypeError。
    """
    return datetime.now(tz=_CST).replace(tzinfo=None)


def periods_on_boundary(bar_time: Optional[datetime]) -> List[str]:
    """1m bar 结束时刻 → 命中的边界周期列表（可累积，只读 bar stime，不引入本机时钟）。

    minute%5==0→5m、%15→15m、%30→30m、minute==0→1h。可累积：10:30 → [5m,15m,30m]，
    11:00 → [5m,15m,30m,1h]。非边界时刻（如 10:03）→ []。
    """
    if bar_time is None:
        return []
    result: List[str] = []
    minute = bar_time.minute
    if minute % 5 == 0:
        result.append("5m")
    if minute % 15 == 0:
        result.append("15m")
    if minute % 30 == 0:
        result.append("30m")
    if minute == 0:
        result.append("1h")
    return result


def _to_int(val) -> int:
    """数值转 int（公式 trigger_value）；NaN/None/无法解析 → 0。同 backtest._to_int。"""
    if val is None:
        return 0
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, (int, float)):
        try:
            if math.isnan(val):
                return 0
        except (TypeError, ValueError):
            pass
        return int(val)
    try:
        return int(Decimal(str(val)))
    except (ValueError, ArithmeticError):
        return 0
