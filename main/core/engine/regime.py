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
from typing import Dict, List, Optional, Tuple

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


class _WeekLabeler:
    """周频四态标签机（增量）。规则与历史 segment_regime 内联循环完全一致——
    segment_regime 与本类共用同一实现，杜绝批量/增量两路漂移。

    状态：weekly closes 序列 + 趋势状态机（state/bear_trough）。
    ``append_week`` 推进到新一周；``update_last_week`` 在周内用最新收盘价重算
    当周标签（回滚到处理当周前的机器状态再重放，保证与批量结果逐周一致）。
    """

    def __init__(self) -> None:
        self.closes: List[float] = []
        self.labels: List[str] = []
        self._state: str = REGIME_RANGE
        self._bear_trough: Optional[float] = None
        # 处理最后一周之前的机器状态快照（周内更新时回滚重算用）
        self._pre_state: str = REGIME_RANGE
        self._pre_bear_trough: Optional[float] = None

    def _ma(self, t: int, window: int) -> float:
        return sum(self.closes[t - window + 1:t + 1]) / window

    def _signal(self, i: int) -> Tuple[bool, bool]:
        """第 i 周的原始牛/熊信号（不含确认）。调用处保证 i 可算。"""
        closes = self.closes
        c = closes[i]
        f = sum(closes[i - MA_FAST_WEEKS + 1:i + 1]) / MA_FAST_WEEKS
        s = sum(closes[i - MA_SLOW_WEEKS + 1:i + 1]) / MA_SLOW_WEEKS
        win = closes[i - BREAKOUT_WEEKS:i]
        bull = c > max(win) and f > s and c > s
        bear = c < min(win) and f < s and c < s
        return bull, bear

    def _step(self, t: int) -> str:
        """计算第 t 周标签并推进机器状态。t < MA_SLOW_WEEKS 为预热期，恒 range。"""
        if t < MA_SLOW_WEEKS:
            return REGIME_RANGE
        closes = self.closes
        close = closes[t]
        ma10 = self._ma(t, MA_FAST_WEEKS)
        ma30 = self._ma(t, MA_SLOW_WEEKS)
        ma10_prev = self._ma(t - 1, MA_FAST_WEEKS)

        bull_now, bear_now = self._signal(t)
        bull_prev, bear_prev = self._signal(t - 1)
        confirm_bull = bull_now and bull_prev
        confirm_bear = bear_now and bear_prev

        # 信号优先级：趋势反转确认最高（与风控优先于公式的平台约定一致）
        if confirm_bear:
            self._state = REGIME_BEAR
            self._bear_trough = close
        elif confirm_bull:
            self._state = REGIME_BULL
            self._bear_trough = None
        elif self._state == REGIME_BEAR:
            self._bear_trough = min(self._bear_trough, close)
            rebound_pct = close / self._bear_trough - 1
            if (rebound_pct >= REBOUND_MIN_PCT
                    and close > ma10 and ma10 >= ma10_prev):
                self._state = REGIME_REBOUND
        elif self._state == REGIME_REBOUND:
            # 再创下跌段新低 → 反弹失败回到下跌；新高新低反转由上面的确认处理
            if close < self._bear_trough:
                self._state = REGIME_BEAR
                self._bear_trough = close
        elif self._state == REGIME_BULL:
            # 周收盘跌破 MA30：趋势破坏，回到无方向状态（未破位前续用 bull）
            if close < ma30:
                self._state = REGIME_RANGE
        return self._state

    def append_week(self, close: float) -> str:
        """新一周（以该周首个交易日收盘价开仓），返回当周标签。"""
        self._pre_state = self._state
        self._pre_bear_trough = self._bear_trough
        self.closes.append(close)
        label = self._step(len(self.closes) - 1)
        self.labels.append(label)
        return label

    def update_last_week(self, close: float) -> str:
        """周内更新：用最新收盘价重算当周标签（已完成周标签不回改）。"""
        if not self.closes:
            return self.append_week(close)
        self._state = self._pre_state
        self._bear_trough = self._pre_bear_trough
        self.closes[-1] = close
        label = self._step(len(self.closes) - 1)
        self.labels[-1] = label
        return label


def _label_weeks(weekly: List[Tuple[date, float]]) -> List[str]:
    """批量打标：逐周喂标签机（与 segment_regime 历史行为一致）。"""
    labeler = _WeekLabeler()
    for _, v in weekly:
        labeler.append_week(v)
    return labeler.labels


class CausalRegimeTracker:
    """逐日因果市场状态跟踪（引擎运行时 B5 用）。

    ``update(d, close)`` 只吸收 ≤d 的收盘价：新 ISO 周 → 开新周；同周 →
    用最新收盘重算当周标签。标签规则与 ``segment_regime`` 完全一致
    （共用 ``_WeekLabeler``），已产出标签不随后续数据回改（纯因果）。
    日期必须单调不减；同日重复 update 幂等（直接返回当前标签）。
    """

    def __init__(self) -> None:
        self._labeler = _WeekLabeler()
        self._cur_iso: Optional[Tuple[int, int]] = None
        self._last_date: Optional[date] = None
        self._label: str = REGIME_RANGE

    @property
    def label(self) -> str:
        return self._label

    def update(self, d: date, close: float) -> str:
        if self._last_date is not None:
            if d < self._last_date:
                raise ValueError(
                    "CausalRegimeTracker 只接受单调不减的日期：%s < %s"
                    % (d, self._last_date))
            if d == self._last_date:
                return self._label
        iso = d.isocalendar()[:2]
        if iso != self._cur_iso:
            self._cur_iso = iso
            self._label = self._labeler.append_week(float(close))
        else:
            self._label = self._labeler.update_last_week(float(close))
        self._last_date = d
        return self._label

    def feed_history(self, prices) -> str:
        """批量预喂历史收盘（回测预热段 / 实盘启动回放），返回最新标签。"""
        for d, v in _normalize_prices(prices):
            self.update(d, v)
        return self._label


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
    # 标签规则与 CausalRegimeTracker 共用 _WeekLabeler（单一实现）。
    labels: List[str] = _label_weeks(weekly)

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
