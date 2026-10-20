# -*- coding: utf-8 -*-
"""市场状态分段器（FR-5）：基准周线四态分段。

四态：
- ``bull``     趋势上涨：周收盘创 26 周新高且 MA10>MA30、收盘在 MA30 上方，连续 2 周确认
- ``bear``     趋势下跌：周收盘创 26 周新低且 MA10<MA30、收盘在 MA30 下方，连续 2 周确认
- ``rebound``  下跌反弹：处于下跌段，自该段近端低点反弹 ≥8%、收盘站上 MA10 且 MA10 拐头向上
- ``range``    震荡：以上条件均不满足（含数据不足的预热期）

设计原则
--------
1. **纯因果（无未来函数）**：第 t 周标签只使用 ≤t 的收盘价。转折点本身只能在
   发生后由「连续 2 周确认」识别，确认滞后 2 周是规则内置滞后（注释于
   ``CONFIRM_WEEKS``），不是未来数据。因此对任意截断子序列，已产出的全部周
   标签与全量序列完全一致（单测 TR-4.2 验证）。
2. **固定常量**：阈值不做前端可调（OQ2），评审若调整只改本文件常量。
3. **纯函数、无 TQ 依赖**：输入基准收盘价，输出 JSON 安全的 dict。

周频口径与 ``crisis_alpha`` 对齐：``resample("W-FRI")`` 等价——按 ISO 周聚合
日频收盘价（每周取最后一个交易日），周末标签日为该周**周五**。
"""
from datetime import date, timedelta
from typing import List, Tuple

# ---------------------------------------------------------------------------
# 规则常量（口径评审入口）
# ---------------------------------------------------------------------------
MA_FAST_WEEKS = 10        # 快均线周期（周）
MA_SLOW_WEEKS = 30        # 慢均线周期（周）；不足该周数的预热期一律标 range
BREAKOUT_WEEKS = 26       # Donchian 突破窗口：与此前 26 周（不含当周）极值比较
CONFIRM_WEEKS = 2         # 趋势信号确认周数：信号须连续 2 周成立（转折点滞后 2 周）
REBOUND_MIN_PCT = 0.08    # 反弹阈值：自下跌段近端低点反弹 ≥ 8%

REGIME_BULL = "bull"
REGIME_BEAR = "bear"
REGIME_RANGE = "range"
REGIME_REBOUND = "rebound"

REGIME_LABELS_CN = {
    REGIME_BULL: "趋势上涨",
    REGIME_BEAR: "趋势下跌",
    REGIME_RANGE: "震荡",
    REGIME_REBOUND: "下跌反弹",
}


def _normalize_prices(prices) -> List[Tuple[date, float]]:
    """归一化输入为按日期升序的 (date, float) 列表。

    支持：dict[date, number]、pandas Series（日期索引）、
    [(date, number), ...]。
    """
    if prices is None:
        return []
    # pandas Series（不强制依赖 pandas：仅在命中分支时取属性）
    if hasattr(prices, "index") and hasattr(prices, "iloc") and not isinstance(prices, dict):
        items = [(idx, prices.loc[idx]) for idx in prices.index]
    elif isinstance(prices, dict):
        items = list(prices.items())
    else:
        items = list(prices)

    norm = []
    for item in items:
        d, v = item[0], item[1]
        if hasattr(d, "date") and not isinstance(d, date):
            # pandas.Timestamp / datetime.datetime
            d = d.date()
        norm.append((d, float(v)))
    norm.sort(key=lambda x: x[0])
    return norm


def _to_weekly(daily: List[Tuple[date, float]]) -> List[Tuple[date, float]]:
    """日频收盘按 ISO 周聚合：每周取最后一个交易日的收盘价，标签日为该周周五。

    与 crisis_alpha 的 ``resample("W-FRI").last()`` 对齐。
    """
    weeks: List[Tuple[date, float]] = []
    cur_iso = None
    for d, v in daily:
        iso = d.isocalendar()[:2]
        if iso != cur_iso:
            # ISO 周一 weekday=1、周五=5 → 偏移 5-isoweekday() 天到该周周五
            friday = d + timedelta(days=5 - d.isoweekday())
            weeks.append([friday, v])  # type: ignore[list-item]
            cur_iso = iso
        else:
            weeks[-1][1] = v
    return [(w[0], w[1]) for w in weeks]


def segment_regime(prices) -> dict:
    """对基准收盘价序列做周频四态分段。

    :param prices: 基准收盘价。dict[date, number] / pandas Series /
        [(date, number), ...]，日频或周频均可（内部按 ISO 周聚合）。
    :returns: ``{"weeks": [...], "segments": [...], "counts": {...}}``
        - weeks: 每周 ``{"date"(周五), "close", "label"}``
        - segments: 连续同标签段 ``{"label", "start_date", "end_date",
          "start_close", "end_close", "change_pct", "weeks"}``，按时间排序
        - counts: 四态周数统计
    """
    weekly = _to_weekly(_normalize_prices(prices))
    n = len(weekly)
    labels: List[str] = [REGIME_RANGE] * n

    if n >= MA_SLOW_WEEKS:
        closes = [v for _, v in weekly]

        def _ma(t: int, window: int) -> float:
            return sum(closes[t - window + 1:t + 1]) / window

        state = REGIME_RANGE
        bear_trough = None  # 当前下跌段的近端低点（含 bear/rebound 期间）

        # 确认需要 t 与 t-1 两周信号均可计算（MA30 从索引 29 起可算），
        # 故最早在 t=30 产出非 range 标签；索引 0..29 为预热期。
        for t in range(MA_SLOW_WEEKS, n):
            close = closes[t]
            ma10 = _ma(t, MA_FAST_WEEKS)
            ma30 = _ma(t, MA_SLOW_WEEKS)
            ma10_prev = _ma(t - 1, MA_FAST_WEEKS)

            prior_window = closes[t - BREAKOUT_WEEKS:t]

            def _signal(i: int) -> Tuple[bool, bool]:
                """第 i 周的原始牛/熊信号（不含确认）。调用处保证 i 可算。"""
                c = closes[i]
                f = sum(closes[i - MA_FAST_WEEKS + 1:i + 1]) / MA_FAST_WEEKS
                s = sum(closes[i - MA_SLOW_WEEKS + 1:i + 1]) / MA_SLOW_WEEKS
                win = closes[i - BREAKOUT_WEEKS:i]
                bull = c > max(win) and f > s and c > s
                bear = c < min(win) and f < s and c < s
                return bull, bear

            bull_now, bear_now = _signal(t)
            bull_prev, bear_prev = _signal(t - 1)
            confirm_bull = bull_now and bull_prev
            confirm_bear = bear_now and bear_prev

            # 信号优先级：趋势反转确认最高（与风控优先于公式的平台约定一致）
            if confirm_bear:
                state = REGIME_BEAR
                bear_trough = close
            elif confirm_bull:
                state = REGIME_BULL
                bear_trough = None
            elif state == REGIME_BEAR:
                bear_trough = min(bear_trough, close)
                rebound_pct = close / bear_trough - 1
                if (rebound_pct >= REBOUND_MIN_PCT
                        and close > ma10 and ma10 >= ma10_prev):
                    state = REGIME_REBOUND
            elif state == REGIME_REBOUND:
                # 再创下跌段新低 → 反弹失败回到下跌；新高新低反转由上面的确认处理
                if close < bear_trough:
                    state = REGIME_BEAR
                    bear_trough = close
            elif state == REGIME_BULL:
                # 周收盘跌破 MA30：趋势破坏，回到无方向状态（未破位前续用 bull）
                if close < ma30:
                    state = REGIME_RANGE

            labels[t] = state

    weeks = [{
        "date": d,
        "close": v,
        "label": labels[i],
    } for i, (d, v) in enumerate(weekly)]

    # 连续同标签合并为段
    segments = []
    for i, label in enumerate(labels):
        d, v = weekly[i]
        if not segments or segments[-1]["label"] != label:
            segments.append({
                "label": label,
                "start_date": d,
                "end_date": d,
                "start_close": v,
                "end_close": v,
                "weeks": 1,
            })
        else:
            seg = segments[-1]
            seg["end_date"] = d
            seg["end_close"] = v
            seg["weeks"] += 1
    for seg in segments:
        seg["change_pct"] = seg["end_close"] / seg["start_close"] - 1

    counts = {REGIME_BULL: 0, REGIME_BEAR: 0,
              REGIME_RANGE: 0, REGIME_REBOUND: 0}
    for label in labels:
        counts[label] += 1

    return {"weeks": weeks, "segments": segments, "counts": counts}
