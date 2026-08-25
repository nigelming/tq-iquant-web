"""MarketDataService — 行情/信号缓存与周期 bar 分发（0010 步骤 2b）。

从 LiveEngine 抽出的行情数据协作者，独占：
- 预热/增量 bar 缓存（_preheat_cache）与公式信号缓存（signal_cache）；
- 公式注入配置（tq_formula / formula_by_strategy / formula_count* / 周期集合）；
- 启动预热、逐 bar 公式信号注入、周期边界 bar 分发、1w/1mon 通达信启动注入。

边界（铁律：协作者不反向 import LiveEngine）：
- 本服务不直接下单、不持有引擎引用。周期 bar 需驱动 on_bar 管线时，经构造注入的
  ``on_bar`` 回调（引擎传入其 _handle_bar 绑定方法），由引擎侧完成下单/风控。
- 共享状态（portfolios/dispatcher/code_period_count）经 EngineContext 读取。
- F5 桥可用持仓刷新后需写回 T+1 检查器，经注入的 ``set_available_map`` 窄回调，
  不直接抓执行层对象。

本步为纯搬移：方法体逻辑不变，仅把 self._dispatcher/self.portfolios 等改为
self.ctx.*、self._handle_bar 改为 self._on_bar；LiveEngine 保留同名薄委托方法/
property，既有调用点与测试直连（engine.signal_cache={...}、engine._preheat()、
LiveEngine._bars_to_formula_df(...) 等）全部穿透到本服务，行为不变。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Dict, List, Optional

from ..event import BarEvent
from ..http_bridge_dispatcher import BridgeUnavailableError
from ..bar_poller import parse_bar_time, to_ohlcv, latest_completed_bar
from ..portfolio import Portfolio
from .context import EngineContext
from .timing import _to_int

logger = logging.getLogger("core.engine.live.market_data")

# TQ 公式输出中需跳过的非变量键（同 backtest._FORMULA_META_KEYS）
_FORMULA_META_KEYS = ("Date", "ErrorId", "Error", "Time")

# C6(C)：1w/1mon 走通达信启动/日终注入（桥端 xtdata 拉不到），_fill_signal_cache 跳过不拉桥
_STARTUP_ONLY_PERIODS = ("1w", "1mon")


class MarketDataService:
    def __init__(
        self,
        ctx: EngineContext,
        bar_poller,
        *,
        on_bar: Callable[..., None],
        set_available_map: Callable[[Dict[str, int]], None],
        tq_formula=None,
        formula_by_strategy: Optional[Dict[int, str]] = None,
        formula_count: int = 200,
        formula_count_by_name: Optional[Dict[str, int]] = None,
    ) -> None:
        self.ctx = ctx
        self._bar_poller = bar_poller
        # 驱动一根 bar 的 on_bar 管线（引擎传入 self._handle_bar 绑定方法）；本服务
        # 不反向 import LiveEngine，周期/日终 bar 经此回调交回引擎完成下单/风控。
        self._on_bar = on_bar
        # F5：桥可用持仓刷新后写回 T+1 检查器的窄回调（LiveT1Checker.set_available_map）。
        self._set_available_map = set_available_map

        # 公式注入（0010）：tq_formula 封装内存注入链路；formula_by_strategy 预加载
        # {strategy_id: formula_name}，避免每 bar 查库；formula_count 为注入历史根数
        # （1m/5m 默认 200，够均线预热；不足时调大）。
        self._tq_formula = tq_formula
        self._formula_by_strategy: Dict[int, str] = formula_by_strategy or {}
        self._formula_count = formula_count
        # #27→#28：count 按公式配（Formula.formula_count），同公式恒定 → C4 去重 key
        # (code, period, formula) 无需 count 进 key。_formula_count 作全局兜底（老调用不破）。
        self._formula_count_by_name: Dict[str, int] = formula_count_by_name or {}
        # 每周期预拉最大 count：该周期策略所用公式的最大 formula_count——边界/日终分发
        # 预拉的 bars 够该周期最长公式（count 不够时注入会缺历史，信号 NaN 静默失效）。
        self._period_count: Dict[str, int] = {}
        # 实例所有策略的周期集合（含无公式策略，含 1d/1w/1mon）——_dispatch_period_bar
        # 边界分发 guard 用：只拉实例确有策略的周期，挡掉 periods_on_boundary 纯算术
        # 带出但实例无人用的周期（如 14:30 的 15m——minute%15==0 触发，却无 15m 策略，
        # 白拉 17 只 count=200 后 period 过滤全跳过）。比 _period_count 更宽：后者只收
        # 有公式映射的策略周期，无公式的 30m 策略靠此集合保住风控单的边界驱动。
        self._strategy_periods: set = set()
        for _p in ctx.portfolios:
            for _ctx in _p.strategies:
                self._strategy_periods.add(_ctx.period)
                _name = self._formula_by_strategy.get(_ctx.strategy_id)
                if not _name:
                    continue
                _cnt = self._formula_count_by_name.get(_name, self._formula_count)
                if _cnt > self._period_count.get(_ctx.period, 0):
                    self._period_count[_ctx.period] = _cnt

        # 预热缓存：(code, period) -> {"bars": [...], "last_stime": datetime, "count": int}
        # 启动 preheat() 填充（拉 code_period_count[(code,period)] 根历史）；
        # 运行期 _get_bars_with_increment 读它 + 增量拉新 bar 拼接（省去每 bar 全量重拉）；
        # 离线恢复 _tick_main 清空 → 下次走全量重建。跨 bar 生命周期（不像 df_cache 每 bar 重建）。
        self._preheat_cache: Dict[tuple, dict] = {}

        # 信号缓存：(strategy_id, stock_code, bar_time) -> [{"name": str, "value": int}]
        # 风控信号（止损/止盈/移动止损）由 Portfolio._check_risks 直接生成，无需缓存；
        # 公式信号（OPEN/ADD/REDUCE/CLOSE）需缓存命中才触发——fill_signal_cache 在
        # 每根 bar 前拉历史 → 内存注入算公式 → 填此 dict。测试可直接预置以验证下单链路。
        self.signal_cache: Dict = {}

    # ---------------- 预热 + 增量拼接（拉取优化）----------------
    def preheat(self) -> None:
        """启动预热：对每个 (code, period) 拉 code_period_count 根历史存 _preheat_cache。

        只预热 ctx.code_period_count 里的 (code,period)（实例真实有策略的，按需不浪费），
        跳过 1d/1w/1mon（1d 不预热走日终 _maybe_daily_bars；1w/1mon 走通达信 inject_startup_periods）。
        单 (code,period) 拉取失败不阻断启动（log warn，运行期该 key 走 _get_bars_with_increment
        的"缓存未命中全量补"自愈）。启动一次性同步调用，在 start() 里 inject_startup_periods 之后。
        """
        for (code, period), count in self.ctx.code_period_count.items():
            if period in ("1d", "1w", "1mon"):
                continue
            try:
                bars = self.ctx.dispatcher.query_quote(code, period=period, count=count)
            except BridgeUnavailableError:
                logger.warning("preheat failed (bridge unavailable) %s %s", code, period)
                continue
            except Exception:  # noqa: BLE001
                logger.exception("preheat failed %s %s", code, period)
                continue
            if not bars:
                continue
            self._preheat_cache[(code, period)] = self._make_cache_entry(bars, count)

    def _make_cache_entry(self, bars: list, count: int) -> dict:
        """bars → 排序截断到 count 根 + 算 last_stime，构造 _preheat_cache 条目。"""
        bars = self._sort_and_cap(bars, count)
        return {"bars": bars, "last_stime": self._max_stime(bars), "count": count}

    def get_bars_with_increment(self, code: str, period: str, count: int) -> list:
        """读预热缓存历史 + 增量拉新 bar 拼接，返回 count 根窗口（拉取优化核心）。

        1) 缓存命中：增量拉 query_quote(count=INCREMENT_COUNT) 筛 stime > cache.last_stime
           的新 bar，拼到 cache.bars 末尾，排序截断保持 count 长；无新 bar 直接返缓存（最省）。
        2) 缓存未命中（预热失败/离线清缓存后）：全量拉 count 根回填缓存（异常/首次路径，
           不背正常增量的负担）。
        桥拉取抛 BridgeUnavailableError 向上传播（交 on_bar/dispatch_period_bar 置离线）。
        """
        INCREMENT_COUNT = 10  # 增量拉取根数，够覆盖正常单边界增量（1-2 根）；离线恢复走清缓存全量重建
        cache = self._preheat_cache.get((code, period))
        if cache is None:
            bars = self.ctx.dispatcher.query_quote(code, period=period, count=count)
            if bars:
                self._preheat_cache[(code, period)] = self._make_cache_entry(bars, count)
            return bars
        # 缓存存在但请求 count > 缓存 count：缓存历史不够长公式的窗口 → 升级全量拉 count 根
        # （同 _fetch_cached_bars 升级语义：count 不够会缺历史，长均线 NaN 静默失效）。
        if count > cache["count"]:
            bars = self.ctx.dispatcher.query_quote(code, period=period, count=count)
            if bars:
                self._preheat_cache[(code, period)] = self._make_cache_entry(bars, count)
            return bars
        new_bars = self.ctx.dispatcher.query_quote(code, period=period, count=INCREMENT_COUNT)
        last = cache["last_stime"]
        fresh = [b for b in new_bars if self._bar_stime(b) is not None
                 and (last is None or self._bar_stime(b) > last)]
        if not fresh:
            return cache["bars"]
        merged = self._sort_and_cap(cache["bars"] + fresh, count)
        cache["bars"] = merged
        cache["last_stime"] = self._max_stime(merged)
        return merged

    @staticmethod
    def _bar_stime(bar: dict) -> Optional[datetime]:
        """bar → 结束时间 datetime（复用 parse_bar_time，兼容 stime/time/index）。"""
        return parse_bar_time(bar)

    @staticmethod
    def _sort_and_cap(bars: list, count: int) -> list:
        """按 bar stime 升序排序，截断保留最新 count 根（去重同 stime）。"""
        timed = [(parse_bar_time(b), b) for b in bars]
        seen: Dict[datetime, dict] = {}
        for bt, b in timed:
            if bt is not None and bt not in seen:
                seen[bt] = b
        ordered = [b for _, b in sorted(seen.items())]
        return ordered[-count:] if count > 0 else ordered

    @staticmethod
    def _max_stime(bars: list) -> Optional[datetime]:
        """bars 中最新 bar 的 stime（空 → None）。"""
        stimes = [parse_bar_time(b) for b in bars]
        stimes = [t for t in stimes if t is not None]
        return max(stimes) if stimes else None

    def dispatch_period_bar(self, period: str, boundary_time: datetime) -> None:
        """C6(A) 边界分发：period 边界到（1m stime 判定）→ 拉该周期 bar → 注入 → 驱动该周期策略。

        对每 code 拉 query_quote(period, count=formula_count) 一次，既供公式注入又供 BarEvent。
        取每 code「最新已完成 bar」（stime < 本批 latest）的 OHLCV 构造 BarEvent
        （bar_time=boundary_time，与 1m 节拍对齐）——不用 forming 最新一根（未来函数）。
        桥拉取抛 BridgeUnavailableError → 向上传播由 _on_bar 置离线。无完成 bar 的 code 跳过。

        周期 guard：periods_on_boundary 是纯算术（minute%15==0 等），不查实例有无该周期策略，
        会带出实例无人用的周期（如 14:30 的 15m）。此处按 _strategy_periods 过滤——实例无该
        周期策略直接 return，避免白拉（拉完 period 过滤全跳过，不出单纯浪费）。
        """
        if period not in self._strategy_periods:
            return
        bars_by_code: Dict[str, list] = {}
        stocks: Dict[str, dict] = {}
        # C4(#28)：预拉 count = 该周期策略最大 formula_count（够最长公式，注入不欠历史）
        count = self._period_count.get(period, self._formula_count)
        for code in self._bar_poller.stock_codes:
            # 按 (code, period) 取该股票该周期所需根数（比全局 _period_count 更细，按需）。
            # 走预热缓存 + 增量拼接（省去每边界全量重拉 count 根）。
            cp_count = self.ctx.code_period_count.get((code, period), count)
            bars = self.get_bars_with_increment(code, period, cp_count)
            if not bars:
                continue
            bars_by_code[code] = bars
            cb = latest_completed_bar(bars)
            if cb is None:
                continue
            stocks[code] = to_ohlcv(cb)
        if not stocks:
            return
        bar_event = BarEvent(stocks=stocks, bar_time=boundary_time, period=period)
        df_cache: Dict = {}
        raw_cache: Dict = {}
        for portfolio in self.ctx.portfolios:
            self._on_bar(
                portfolio, bar_event, bars_by_code=bars_by_code,
                df_cache=df_cache, raw_cache=raw_cache,
            )

    # ---------------- F5：桥可用持仓（SELL 减仓上限）----------------
    def refresh_available_map(self) -> None:
        """F5：拉桥 /positions，按 code(instrument.exchange) 聚合 m_nCanUseVolume。

        桥无该仓/拉取失败（离线）→ 空表 → get_available_shares 全量放行（券商端
        T+1 兜底，避免误伤正常卖出；G6 处理券商拒单）。
        """
        try:
            rows = self.ctx.dispatcher.query_positions()
        except BridgeUnavailableError:
            self._set_available_map({})
            return
        m: Dict[str, int] = {}
        for r in rows or []:
            inst = r.get("instrument")
            exch = r.get("exchange")
            avail = r.get("available")
            if inst and exch and avail is not None:
                m["%s.%s" % (inst, exch)] = int(avail)
        self._set_available_map(m)

    # ---------------- 公式信号注入（0010 + C4 #28 三维去重）----------------
    def fill_signal_cache(
        self,
        portfolio: Portfolio,
        bar: BarEvent,
        bars_by_code: Optional[Dict[str, list]] = None,
        df_cache: Optional[Dict] = None,
        raw_cache: Optional[Dict] = None,
    ) -> None:
        """实盘逐 bar 算公式信号填 signal_cache。预填模式（不改 Portfolio）。

        对每个策略 × bar.stocks 每只股票：
          bridge query_quote(code, period, count=N) 拉历史+实时 bar
          → _bars_to_formula_df 转 OHLCV DataFrame
          → TQFormula.compute_injected 内存注入算公式
          → _extract_latest_signal 取最后一条（当前 bar 信号）
          → 填 signal_cache[(strategy_id, code, bar.bar_time)]
        C6 节拍过滤：bar.period 非 None 时只注入匹配周期的策略（5m 边界 bar 不注入 1m 策略）；
        1w/1mon（_STARTUP_ONLY_PERIODS）走通达信启动/日终注入，不拉桥。
        bars_by_code：调用方已预拉好的 bars（边界/日终分发），避免二次拉桥。
        C4(#28) 三维去重（单 bar 生命周期，跨组合共享）：
          df_cache[(code, period)]   → 同 key 只 query_quote 一次（count 更大时升级重拉）
          raw_cache[(code,period,formula)] → 同 key 只 compute_injected 一次（TQ 计算最贵）
          count 不进 key 的前提：count 是 Formula.formula_count 公式级字段（#27），
          同公式 count 恒定 → 同 (code,period,formula) 的 count 必然相同。
        signal_cache key 仍带 strategy_id（隔离不变，值相同各自存一份）。
        无 tq_formula / 策略无公式映射 / 拉取为空 / 算失败 → 跳过（该股该 bar 无公式信号）。
        """
        if self._tq_formula is None or not self._formula_by_strategy:
            return
        if df_cache is None:
            df_cache = {}
        if raw_cache is None:
            raw_cache = {}
        for ctx in portfolio.strategies:
            formula_name = self._formula_by_strategy.get(ctx.strategy_id)
            if not formula_name:
                continue
            # C6：该 bar 只注入匹配周期的策略
            if bar.period is not None and ctx.period != bar.period:
                continue
            # C6(C)：1w/1mon 走通达信启动/日终注入，不拉桥
            if ctx.period in _STARTUP_ONLY_PERIODS:
                continue
            period = ctx.period
            # #27→#28：注入 count 来自 Formula.formula_count（公式级），非全局 200
            count = self._formula_count_by_name.get(formula_name, self._formula_count)
            for code in bar.stocks:
                # 股票池过滤：池外股票不拉公式（多组合共享行情 bar，各策略只算自己池内）
                if ctx.stock_pool is not None and code not in ctx.stock_pool:
                    continue
                try:
                    bars = self._fetch_cached_bars(
                        df_cache, bars_by_code, code, period, count
                    )
                except BridgeUnavailableError:
                    # 拉历史失败：跳过该股（不阻断 on_bar，风控信号仍可触发）
                    logger.warning("quote failed for formula inject %s %s", code, period)
                    continue
                raw_key = (code, period, formula_name)
                if raw_key not in raw_cache:
                    df = self._bars_to_formula_df(bars, code)
                    raw = None
                    if df is not None:
                        raw = self._tq_formula.compute_injected(
                            formula_name=formula_name, ohlcv_df=df,
                            stocks=[code], period=period,
                        )
                    raw_cache[raw_key] = self._extract_latest_signal(raw, code)
                outputs = raw_cache[raw_key]
                if outputs:
                    self.signal_cache[(ctx.strategy_id, code, bar.bar_time)] = outputs

    def _fetch_cached_bars(
        self,
        df_cache: Dict,
        bars_by_code: Optional[Dict[str, list]],
        code: str,
        period: str,
        count: int,
    ) -> list:
        """拉取去重：df_cache[(code, period)] 同 key 只实际拉取一次（单 bar 生命周期）。

        缓存值 (bars, used_count)。同 code+period 的公式 count 更大 → 升级重拉（过小会缺
        历史，公式长均线 NaN 静默失效）；bars_by_code 已按该周期最大 count 预拉 → 直接复用，
        记 used=(code,period) 最大 count，避免无谓升级重拉。
        bars_by_code 提供的 bars **不足 count**（BarPoller 本轮拉的 count 窗口，如 10 根）
        → 不能直接复用（拿 10 根喂 200 根窗口公式 = 长均线 NaN 静默失效），改走
        _reuse_provided_with_cache：并入预热缓存，缓存覆盖 count 则复用、否则增量补齐。
        底层实际拉取走 get_bars_with_increment（预热缓存 + 增量拼接），不再直接 query_quote
        全量——1m 算公式（bars_by_code=None）与升级重拉都受益。
        """
        key = (code, period)
        cached = df_cache.get(key)
        if cached is not None:
            bars, used = cached
            if count <= used:
                return bars
            bars = self.get_bars_with_increment(code, period, count)
            df_cache[key] = (bars, count)
            return bars
        if bars_by_code is not None and code in bars_by_code:
            provided = bars_by_code[code]
            # 提供 bars 已覆盖公式窗口 → 直接复用（used 记该 (code,period) 最大 count，
            # 同 bar 内更大 count 公式也直接复用不重拉）。
            if len(provided) >= count:
                used = max(count, self.ctx.code_period_count.get((code, period), self._formula_count))
                df_cache[key] = (provided, used)
                return provided
            # 提供 bars 不足 count：
            #   非轮询周期（5m/1d...边界分发预拉，本就按 code_period_count 全量，桥只返
            #   这么多历史）→ 直接复用（历史就这么多，不能无中生有）。
            #   轮询周期（1m，BarPoller 透传，count 窗口仅判完成用）→ 并入预热缓存复用/补齐。
            if period != self._bar_poller.period:
                used = max(count, self.ctx.code_period_count.get((code, period), self._formula_count))
                df_cache[key] = (provided, used)
                return provided
            bars = self._reuse_provided_with_cache(code, period, provided, count)
            df_cache[key] = (bars, count)
            return bars
        bars = self.get_bars_with_increment(code, period, count)
        df_cache[key] = (bars, count)
        return bars

    def _reuse_provided_with_cache(self, code: str, period: str, provided: list, count: int) -> list:
        """把调用方本轮已拉到的 bars（BarPoller 透传）并入预热缓存复用，零额外拉取。

        BarPoller 每轮已拉 1m（count 窗口，如 10 根），注入若再走 get_bars_with_increment
        增量拉（同样 count 窗口）就是同一批 bars 的双份冗余。把本轮已拉的并入缓存后：
          缓存历史已够 count 根（启动预热 code_period_count 根）→ 直接返回，零拉桥；
          缓存历史不够（未预热/离线清空/请求 count 更大）→ 回退 get_bars_with_increment
          全量/增量补齐（同原路径，冷启动安全）。
        """
        cache = self._preheat_cache.get((code, period))
        if cache is None:
            # 冷启动/离线清空：提供 bars 量小不足公式窗口，走全量拉补缓存（含这些 bars）。
            return self.get_bars_with_increment(code, period, count)
        merged = self._sort_and_cap(cache["bars"] + provided, count)
        cache["bars"] = merged
        cache["last_stime"] = self._max_stime(merged)
        if len(merged) >= count:
            return merged
        return self.get_bars_with_increment(code, period, count)

    @staticmethod
    def _bars_to_formula_df(bars: list, code: str) -> Optional[dict]:
        """桥 bar dict 列表 → {Amount/Volume/Close/Open/High/Low: pandas.DataFrame}。

        桥 bar 字段：stime(yyyymmddHHMMSS)/time(时间戳)/index(历史工具) + 小写 OHLCV。
        时间统一用 parse_bar_time（与 BarPoller 同规则），兼容 stime/time/index 各来源。
        输出：每字段单列 DataFrame（列=[code]，行=DatetimeIndex）。
        空 bars / 无有效时间 → None（调用方跳过）。
        """
        if not bars:
            return None
        # pandas 在此函数内首次按需 import（非顶部）：pandas 较重，且本函数仅在
        # 公式注入路径调用；避免模块导入期无条件加载（审计 #31：math 已提顶，pandas 刻意保留 lazy）。
        import pandas as pd

        times, o, h, l, c, v, a = [], [], [], [], [], [], []
        for b in bars:
            t = parse_bar_time(b)
            if t is None:
                continue

            def _num(key):
                val = b.get(key)
                if val is None or val == "":
                    return 0.0
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return 0.0

            times.append(t)
            o.append(_num("open"))
            h.append(_num("high"))
            l.append(_num("low"))
            c.append(_num("close"))
            v.append(int(_num("volume")))
            a.append(_num("amount"))
        if not times:
            return None
        idx = pd.DatetimeIndex(times)
        return {
            "Open": pd.DataFrame({"open": o}, index=idx).rename(columns={"open": code}),
            "High": pd.DataFrame({"high": h}, index=idx).rename(columns={"high": code}),
            "Low": pd.DataFrame({"low": l}, index=idx).rename(columns={"low": code}),
            "Close": pd.DataFrame({"close": c}, index=idx).rename(columns={"close": code}),
            "Volume": pd.DataFrame({"volume": v}, index=idx).rename(columns={"volume": code}),
            "Amount": pd.DataFrame({"amount": a}, index=idx).rename(columns={"amount": code}),
        }

    @staticmethod
    def _extract_latest_signal(raw: Optional[dict], code: str) -> List[dict]:
        """从 formula_process_mul_zb 返回取最后一条 bar 的信号 → [{"name", "value"}]。

        raw: {stock_code: {var_name: [{"Date","Value"}, ...]}, "ErrorId", ...}
        实盘逐 bar 算，注入 N 根算出 N 条输出，取最后一条即当前 bar 信号
        （避开回测的索引对齐全段逻辑）。ErrorId 非 0/19 → 空。
        """
        if not isinstance(raw, dict) or not raw:
            return []
        err = raw.get("ErrorId")
        if err is not None and str(err) not in ("0", "19"):
            return []
        stock_data = raw.get(code)
        if not isinstance(stock_data, dict) or not stock_data:
            return []
        outputs: List[dict] = []
        for var_name, val_list in stock_data.items():
            if var_name in _FORMULA_META_KEYS:
                continue
            if not isinstance(val_list, list) or not val_list:
                continue
            last = val_list[-1]
            if not isinstance(last, dict):
                continue
            v = last.get("Value")
            if v is None:
                continue
            outputs.append({"name": var_name, "value": _to_int(v)})
        return outputs

    def inject_startup_periods(self, daily_time: datetime) -> None:
        """C6(C)：1w/1mon 策略通达信注入——TQFormula.compute 自取历史 → 最新信号填 signal_cache。

        key=(strategy_id, stock_code, daily_time)，与日终 _maybe_daily_bars 驱动用的
        bar_time 一致，驱动时命中预填信号。桥端 xtdata 拉不到 1w/1mon，通达信是唯一通路。
        start() 启动调一次；_maybe_daily_bars 检测日切 cache miss 时补调。
        单策略/单股 compute 失败 → 跳过（不阻断其余）。
        """
        if self._tq_formula is None or not self._formula_by_strategy:
            return
        codes = list(self._bar_poller.stock_codes)
        for portfolio in self.ctx.portfolios:
            for ctx in portfolio.strategies:
                if ctx.period not in _STARTUP_ONLY_PERIODS:
                    continue
                formula_name = self._formula_by_strategy.get(ctx.strategy_id)
                if not formula_name:
                    continue
                for code in codes:
                    try:
                        raw = self._tq_formula.compute(
                            formula_name, "", [code], period=ctx.period, count=-1
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "startup period compute failed %s %s", ctx.period, code
                        )
                        continue
                    outputs = self._extract_latest_signal(raw, code)
                    if outputs:
                        self.signal_cache[(ctx.strategy_id, code, daily_time)] = outputs

    def startup_periods_missing(self, daily_time: datetime) -> bool:
        """1w/1mon 策略在 daily_time 的信号是否全部已预填（cache miss → 需补注入）。

        _maybe_daily_bars 日切检测用：新交易日的 daily_time 尚无 cache 键 → True。
        """
        for portfolio in self.ctx.portfolios:
            for ctx in portfolio.strategies:
                if ctx.period not in _STARTUP_ONLY_PERIODS:
                    continue
                for code in self._bar_poller.stock_codes:
                    if (ctx.strategy_id, code, daily_time) not in self.signal_cache:
                        return True
        return False
