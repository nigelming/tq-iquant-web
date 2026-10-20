from typing import Callable, Dict, List, Optional
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP

from .portfolio import Portfolio
from .position import Position, is_convertible_bond, lot_size_for
from .execution_engine import ExecutionEngine, SimulatedDispatcher, SimulatedT1Checker
from .evaluator import Evaluator
from .event import BarEvent, OrderEvent, TradeEvent
from .decision import DecisionRecorder
from tq_iquant_shared.constants import TradeType


def _limit_ratio_for(stock_code: str) -> Decimal:
    """A 股涨跌停幅度：创业板(300/301)/科创板(688) 20%，北交所(4/8 前缀) 30%，其余 10%。

    ST 5% 不特判（v1 偏宽松可接受：对 ST 股拦截区间偏宽，少拦不错拦）。
    """
    prefix = stock_code.split(".")[0]
    if prefix.startswith(("300", "301", "688")):
        return Decimal("0.20")
    if prefix[:1] in ("4", "8"):
        return Decimal("0.30")
    return Decimal("0.10")


# W4 按周期路由：同一时间戳可能同时存在多个周期（1d/1w/1mon 均为 00:00）。
# 固定处理顺序（粗 → 细）：1mon > 1w > 1d > 60m > 30m > 15m > 5m > 1m。
# 每步只处理与当前周期相同的策略，价格索引按 (datetime, period) 键，杜绝跨周期
# 前视（日线 00:00 bar 驱动分钟策略）与同时间戳 OHLC 互相覆盖。
_PERIOD_ORDER: Dict[str, int] = {
    "1mon": 0, "1w": 1, "1d": 2, "60m": 3, "30m": 4, "15m": 5, "5m": 6, "1m": 7,
}


class BacktestEngine:
    def run(
        self,
        portfolio: Portfolio,
        klines: dict = None,
        signal_cache: dict = None,
        benchmark_data: object = None,
        on_progress: Callable[[int], None] = None,
        regime_tracker=None,
    ) -> dict:
        """逐 bar 回测主链路。

        klines: {stock_code: {period: pl.DataFrame}}，DataFrame 含 datetime + Open/High/Low/Close/Volume 列。
        成交时机：上一 bar 触发的订单在下一 bar open 成交（pending_orders 队列），
        成交价取 price_index 中该 bar 的 Open（open_prices 死参数已移除，#12）。
        regime_tracker：B5 可选因果市场状态跟踪器（CausalRegimeTracker，默认 None
        不启用）；逐 bar 喂当日基准收盘并把标签注入 portfolio.current_regime。
        on_progress：进度回调（#10），0–100 递增整数，末值 100，节流至最多约 100 次。
        """
        klines = klines or {}
        signal_cache = signal_cache if signal_cache is not None else {}

        # 决策闸门采集（调参可观测性）：注入组合与执行引擎，run 结束随结果返回
        recorder = DecisionRecorder()
        portfolio.set_recorder(recorder)
        # B5：默认无市场状态（effective_* 全部回落原参数，行为不变）
        portfolio.current_regime = None

        times = self._build_timeline(klines)
        total_bars = len(times)
        report_every = max(1, total_bars // 100)
        price_index = self._build_price_index(klines)
        pending_orders: List[OrderEvent] = []
        trades: List[TradeEvent] = []
        snapshots: List[dict] = []
        # #2：已记过 no_market_data 决策事件的订单 id（仅首次延后记 1 条，长停牌不刷屏）
        no_price_recorded: set = set()
        # #1 涨跌停约束：{(code, period) → 该周期上一根 bar 有效收盘(>0)}，逐 bar 末尾刷新；
        # 键含 period：混合周期下 1d 00:00 bar 的当日收盘不得污染 5m/1m 的"前收"判定
        # （否则分钟单会把当日收盘当昨日前收 → 涨跌停判定失真/前视）。单周期行为不变。
        prev_close_map: Dict[tuple, Decimal] = {}
        price_limit_recorded: set = set()
        # #2 成交量约束：已记过 volume_cap_defer 事件的订单 id（每订单只记 1 条）
        volume_defer_recorded: set = set()
        # 策略层快照：strategy_id → 快照序列。共享现金按 capital_ratio 归一化分摊。
        strategy_snapshots: Dict[int, List[dict]] = {
            ctx.strategy_id: [] for ctx in portfolio.strategies
        }
        # W4.4 每日一行快照：按日期 flush 上一日末值（pending_*），循环后补最后一日。
        pending_snap: Optional[dict] = None
        pending_strategy_snaps: List[dict] = []
        last_snap_date: Optional[date] = None
        # W4.5 regime：喂入"前一交易日"基准收盘（消除当日未来数据），逐日推进。
        last_regime_date: Optional[date] = None
        last_enter_date: Optional[date] = None

        for i, (t, period) in enumerate(times):
            # B5 市场状态闸：在新交易日首根 bar 用"前一交易日"基准收盘推进因果状态机
            # （当日收盘在 00:00 尚未确认，喂当日 = 未来函数）。基准缺失日跳过，沿用标签。
            if regime_tracker is not None:
                if last_regime_date is not None and t.date() != last_regime_date:
                    prev_bench = benchmark_data.get(last_regime_date) if benchmark_data else None
                    if prev_bench is not None:
                        regime_tracker.update(last_regime_date, float(prev_bench))
                portfolio.current_regime = regime_tracker.label
                if last_regime_date is None or t.date() != last_regime_date:
                    last_regime_date = t.date()

            # 0. 跨日补算上一日 daily_loss（在 on_bar 之前）：使日内亏损暂停从
            #    "亏损日的次一交易日"首根 bar 即生效、恢复日首根即解除（消 off-by-one）。
            #    W4：每交易日只调一次（enter_day 同日幂等；混合周期多 bar/日无重复副作用）。
            if last_enter_date != t.date():
                portfolio.risk_manager.enter_day(
                    t.date(), portfolio.account.initial_capital
                )
                last_enter_date = t.date()

            # 1. 成交上一 bar 的 pending_orders（用 t 的 open 价）
            deferred: List[OrderEvent] = []
            if pending_orders:
                bar_open_prices = self._bar_open_prices(price_index, t, period)
                # #4 自适应滑点（可选，默认关）：开启时透传本 bar 各票成交量
                # （手）与冲击系数，dispatcher 按订单量占比动态加滑点；
                # 关闭时不传（dispatcher 走固定滑点，原行为）。
                dispatcher = SimulatedDispatcher(
                    bar_open_prices,
                    bar_volumes=(
                        {code: ohlcv.get("volume")
                         for code, ohlcv in price_index.get((t, period), {}).items()}
                        if portfolio.adaptive_slippage_enabled else None
                    ),
                    slippage_impact_coef=(
                        portfolio.slippage_impact_coef
                        if portfolio.adaptive_slippage_enabled else None
                    ),
                    **portfolio.cost_params,
                )
                engine = ExecutionEngine(
                    dispatcher, SimulatedT1Checker(), recorder=recorder,
                    # 可转债 10 张取整（可选，默认关）：关闭时恒回 100，行为不变
                    lot_size_of=lambda code: lot_size_for(
                        code, portfolio.bond_lot_enabled
                    ),
                )
                # 未来函数闸门：日线级信号（signal_bar_date 非 None）须在
                # t.date() > signal_bar_date 的下一交易日成交，防止当日 00:00
                # 日线 bar 的信号在当日 09:30 分钟 bar 成交。未满足的订单延后。
                for order in pending_orders:
                    # W4 周期路由：订单只在其所属周期的 bar 上成交（1d 单等下一根 1d bar，
                    # 5m 单等下一根 5m bar），非本周期延后——杜绝跨周期前视成交。
                    if order.period is not None and order.period != period:
                        deferred.append(order)
                        continue
                    if order.signal_bar_date is not None and t.date() <= order.signal_bar_date:
                        deferred.append(order)
                        continue
                    # #2 静默丢弃修复：停牌/无行情（本 bar 该票无有效 open 价）→
                    # 延后至下一有价 bar 重试，不静默丢弃（此前直接丢弃导致
                    # 停牌期间止损/止盈单永久丢失）。仅首次延后记决策事件。
                    if not bar_open_prices.get(order.stock_code):
                        if id(order) not in no_price_recorded:
                            no_price_recorded.add(id(order))
                            recorder.record(
                                gate="no_market_data", layer="execution", action="defer",
                                portfolio_id=order.portfolio_id or 0,
                                strategy_id=order.strategy_id,
                                stock_code=order.stock_code,
                                bar_time=t,
                                message="停牌/无行情：本 bar 无开盘价，订单延后至下一有价 bar 重试",
                            )
                        deferred.append(order)
                        continue
                    # #1 涨跌停约束（可选，默认关）：以本 bar open 判定——BUY open ≥ 涨停价
                    # （前收×(1+幅度)，精确到分）或 SELL open ≤ 跌停价 → 延后至下一 bar
                    # 重试（仿 no_market_data，每订单仅首次记 1 条决策事件）。
                    # 无前收或前收 < 1 元跳过检查：前复权"加性压缩"使早期前收逼近 0/
                    # 失真，比率不可信，宁宽勿错（dual 后复权记账口径比率真实）。
                    if portfolio.price_limit_check_enabled:
                        prev_close = prev_close_map.get((order.stock_code, order.period))
                        if prev_close is not None and prev_close >= Decimal("1"):
                            open_px = bar_open_prices[order.stock_code]
                            ratio = _limit_ratio_for(order.stock_code)
                            limit_up = (prev_close * (1 + ratio)).quantize(
                                Decimal("0.01"), rounding=ROUND_HALF_UP)
                            limit_down = (prev_close * (1 - ratio)).quantize(
                                Decimal("0.01"), rounding=ROUND_HALF_UP)
                            is_buy = order.trade_type == TradeType.BUY
                            if (is_buy and open_px >= limit_up) or (
                                    not is_buy and open_px <= limit_down):
                                if id(order) not in price_limit_recorded:
                                    price_limit_recorded.add(id(order))
                                    recorder.record(
                                        gate="price_limit", layer="execution", action="defer",
                                        portfolio_id=order.portfolio_id or 0,
                                        strategy_id=order.strategy_id,
                                        stock_code=order.stock_code,
                                        bar_time=t,
                                        message="涨跌停约束：%s 开盘价 %s %s（前收 %s ×%s），订单延后至下一 bar 重试"
                                                % ("涨停买不进" if is_buy else "跌停卖不出",
                                                   open_px,
                                                   ("≥ 涨停价 %s" % limit_up) if is_buy
                                                   else ("≤ 跌停价 %s" % limit_down),
                                                   prev_close, ratio),
                                    )
                                deferred.append(order)
                                continue
                    # #2 成交量/流动性约束（可选，默认关）：单 bar 成交量 × ratio 为
                    # 成交股数上限（TDX Volume 单位=手，×100 转股数，向下取整手）。
                    # 订单量 ≤ 上限 → 照常；> 上限且上限 ≥ 一手 → 缩量成交（每次记）；
                    # 上限不足一手 → 延后至量足 bar（每订单仅首次记 1 条）。
                    if portfolio.volume_limit_enabled:
                        bar_volume = price_index.get((t, period), {}).get(
                            order.stock_code, {}).get("volume")
                        if bar_volume is not None:
                            # TQ Volume 单位=股（非手），cap = 占比 × 当日成交股数
                            cap_shares = int(
                                portfolio.volume_limit_ratio * Decimal(int(bar_volume))
                            )
                            if order.quantity > cap_shares:
                                vol_lot = lot_size_for(
                                    order.stock_code, portfolio.bond_lot_enabled
                                )
                                if cap_shares >= vol_lot:
                                    recorder.record(
                                        gate="volume_cap_shrink", layer="execution",
                                        action="shrink",
                                        portfolio_id=order.portfolio_id or 0,
                                        strategy_id=order.strategy_id,
                                        stock_code=order.stock_code,
                                        bar_time=t,
                                        requested_qty=order.quantity,
                                        final_qty=cap_shares,
                                        message="成交量约束：订单 %d 股 > 本 bar 上限 %d 股"
                                                "（当日成交 %s 股 ×%s），缩量成交"
                                                % (order.quantity, cap_shares, bar_volume,
                                                   portfolio.volume_limit_ratio),
                                    )
                                    order.quantity = cap_shares
                                else:
                                    if id(order) not in volume_defer_recorded:
                                        volume_defer_recorded.add(id(order))
                                        recorder.record(
                                            gate="volume_cap_defer", layer="execution",
                                            action="defer",
                                            portfolio_id=order.portfolio_id or 0,
                                            strategy_id=order.strategy_id,
                                            stock_code=order.stock_code,
                                            bar_time=t,
                                            requested_qty=order.quantity,
                                            message="成交量约束：本 bar 上限 %d 股不足一手"
                                                    "（当日成交 %s 股 ×%s），订单延后至量足 bar"
                                                    % (cap_shares, bar_volume,
                                                       portfolio.volume_limit_ratio),
                                        )
                                    deferred.append(order)
                                    continue
                    # 成交日 = 当前 bar 时间 t（T+1 据此判断）
                    order.bar_time = t
                    ctx = portfolio.find_strategy(order.strategy_id)
                    if ctx is None:
                        continue
                    # BUY 首次建仓：确保 Position 存在于 ctx.positions
                    pos = ctx.positions.get(order.stock_code)
                    if pos is None and order.trade_type.value == "BUY":
                        # 可转债 run 级开关（可选，默认关）：T+0 仅标记转债持仓
                        # （混合池股票仍 T+1）；手数按标的定档（转债 10 张 / 股票 100 股）
                        pos = Position(
                            order.stock_code,
                            same_day_sellable=(
                                portfolio.bond_t0_enabled
                                and is_convertible_bond(order.stock_code)
                            ),
                            lot_size=lot_size_for(
                                order.stock_code, portfolio.bond_lot_enabled
                            ),
                        )
                        ctx.positions[order.stock_code] = pos
                    trade = engine.execute(
                        order, portfolio.account, pos,
                        **self._exposure_kwargs(portfolio, ctx),
                    )
                    if trade is not None:
                        trades.append(trade)

            # 2. 构造 BarEvent（从预建索引 O(1) 查表；含当前周期标注）
            bar = self._build_bar(price_index, t, period)

            # 3. portfolio.on_bar → 新订单入 pending（下一根同周期 bar 成交）
            #    period=P 只驱动 period 相同的策略，杜绝混合周期前视。
            #    deferred（未成交/未到期订单）需携带至后续 bar 合并。
            new_orders = portfolio.on_bar(bar, signal_cache=signal_cache, period=period)
            pending_orders = deferred + new_orders

            # 4. 日终快照（W4.4：每交易日至多一行，取当日最后一根 bar 的末值）
            total_value = portfolio.total_value(bar)
            snap = portfolio.snapshot(t.date(), total_value, bar)
            # 填基准值：benchmark_data 是 {date: Decimal}，按当前 bar 日期取值；
            # 缺失（基准停牌/非交易日/区间外）直接为 None（不做 carry-forward），
            # Evaluator 喂基准序列时会过滤 None 值，全无则 benchmark_return 退化为 0。
            snap["benchmark_value"] = benchmark_data.get(t.date()) if benchmark_data else None

            # 4a. W2 现金管理增强（预留触发点，默认关）：命中阈值仅记决策事件，不产生订单。
            #     每交易日内部去重（_cash_enhance_last_date），混合周期多 bar/日只记一次。
            portfolio.maybe_cash_enhancement(bar, total_value)

            # 4b. 策略层快照：共享现金按 capital_ratio 归一化分摊，保证可加性
            #     Σ(策略总净值) == 组合总净值。供策略层独立净值曲线与评估。
            #     注入与组合同日的 benchmark_value，使策略层相对指标（FR-1）可算。
            day_strategy_snaps = self._strategy_snapshots(portfolio, bar, t.date())
            for s_snap in day_strategy_snaps:
                s_snap["benchmark_value"] = snap["benchmark_value"]

            # 4c. W4.4 每日一行：日期变化时 flush 上一日的末值快照（组合 + 策略），
            #     当前 bar 落入 pending，循环结束后补最后一日的 pending。
            if last_snap_date is not None and t.date() != last_snap_date:
                snapshots.append(pending_snap)
                for s_snap in pending_strategy_snaps:
                    strategy_snapshots[s_snap["target_id"]].append(s_snap)
            last_snap_date = t.date()
            pending_snap = snap
            pending_strategy_snaps = day_strategy_snaps

            # 5. 熔断检测：更新峰值/检测 max_drawdown，推进熔断次日恢复时序
            #    （daily_loss 已在本 bar 起始 enter_day 补算，此处只做峰值/回撤）
            portfolio.risk_manager.update_peak(total_value, t.date())

            # 6. #1 涨跌停约束：用本 bar 有效收盘(>0) 刷新该周期前收表（下一 bar 判定用）。
            #    开关关闭时零判定开销（仅一次 dict 写入/票）。
            for code, ohlcv in price_index.get((t, period), {}).items():
                close = ohlcv.get("close")
                if close is not None and close > 0:
                    prev_close_map[(code, period)] = close

            # 7. 进度上报：节流至最多约 100 次，末根 bar 必报 100（#10）
            if on_progress is not None and (i % report_every == 0 or i == total_bars - 1):
                on_progress(int((i + 1) * 100 / total_bars))

        # 6. flush 最后一日的 pending 快照（无后续跨日触发）
        if pending_snap is not None:
            snapshots.append(pending_snap)
            for s_snap in pending_strategy_snaps:
                strategy_snapshots[s_snap["target_id"]].append(s_snap)

        # 7. 补算最后一日 daily_loss（#8 对齐实盘 14:30 时点：循环内 update 只在
        #    跨日时补算上一日，最后一日无后续跨日触发，需显式收尾）
        portfolio.risk_manager.finalize_last_day(portfolio.account.initial_capital)

        # 基准序列喂 Evaluator
        # 形如 [{"value": Decimal}]。无任何基准值 → 传 None，benchmark_return 退化为 0。
        bench_series = (
            [{"value": s["benchmark_value"]} for s in snapshots if s.get("benchmark_value") is not None]
            or None
        )
        evaluations = Evaluator().evaluate(snapshots, benchmark_data=bench_series, trades=trades)
        # 策略层评估：复用 Evaluator，喂入该策略快照 + 该策略 trades（按 strategy_id 过滤）。
        # 传入 bench_series 使策略层也能计算基准相对指标（beta/alpha/IR/捕获率等）。
        strategy_evaluations: Dict[int, dict] = {}
        for ctx in portfolio.strategies:
            s_snaps = strategy_snapshots.get(ctx.strategy_id, [])
            if len(s_snaps) < 2:
                continue
            s_trades = [t for t in trades if t.strategy_id == ctx.strategy_id]
            strategy_evaluations[ctx.strategy_id] = Evaluator().evaluate(
                s_snaps, benchmark_data=bench_series, trades=s_trades
            )
        return {
            "trades": trades,
            "snapshots": snapshots,
            "evaluations": evaluations,
            "strategy_snapshots": strategy_snapshots,
            "strategy_evaluations": strategy_evaluations,
            # 决策闸门事件（止损/止盈/熔断/资金不足/超仓…逐触发一条），供落库与调参统计
            "decisions": [ev.to_dict() for ev in recorder.drain()],
        }

    def _exposure_kwargs(self, portfolio: Portfolio, ctx) -> dict:
        """#3 策略级敞口上限（可选，默认关）：开启时给出 execute 的两个 strategy_* 参数。

        上限 = capital_ratio × 组合初始资金；策略持仓市值用 _price_snapshot 估值
        （上一 bar 已刷新，口径与审批用的信号 bar 收盘一致）。关闭返回空 dict，
        execute 走默认 None（行为与旧逻辑一致）。实盘 LiveEngine 不调此方法。
        """
        if not getattr(ctx, "exposure_cap_enabled", False):
            return {}
        return {
            "strategy_position_value": portfolio.holdings_value(ctx),
            "strategy_capital_limit": ctx.capital_ratio * portfolio.account.initial_capital,
        }

    def _build_timeline(self, klines: dict) -> List[tuple]:
        """合并所有股票所有周期的时间轴，返回排序去重的 (datetime, period) 列表。

        W4 周期路由：同一 datetime 下不同周期分别成步（不再合并为一个 bar）。
        同 t 内按固定周期顺序（粗→细：1mon > 1w > 1d > 60m > 30m > 15m > 5m > 1m），
        纯排序确定性，避免同 00:00 的 1d/1w/1mon 步序随集合遍历次序抖动。
        """
        pairs = set()
        for _stock_code, periods in klines.items():
            for period, df in periods.items():
                if "datetime" not in df.columns:
                    continue
                for t in df["datetime"].to_list():
                    pairs.add((t, period))
        return sorted(pairs, key=lambda p: (p[0], _PERIOD_ORDER.get(p[1], 99)))

    def _build_price_index(self, klines: dict) -> Dict[tuple, Dict[str, dict]]:
        """预建 {(datetime, period): {stock_code: {open..volume}}} 索引。

        一次 O(total_rows) 扫描替代逐 bar 的 O(n²) polars df.filter()，将每 bar
        的 FFI 查表降为 O(1) 字典查找。

        W4：键含 period，1d/1w/1mon 同为 00:00 不再互相覆盖（原 #5 粗粒度碰撞
        问题由分键天然消解，故删除 _COARSE_PERIODS 特判）。每步只取当前周期的行，
        该周期策略只见本周期股票与价格。
        """
        index: Dict[tuple, Dict[str, dict]] = {}
        for stock_code, periods in klines.items():
            for period, df in periods.items():
                if "datetime" not in df.columns:
                    continue
                if "Open" not in df.columns:
                    continue
                times = df["datetime"].to_list()
                opens = df["Open"].to_list()
                highs = df["High"].to_list()
                lows = df["Low"].to_list()
                closes = df["Close"].to_list()
                volumes = df["Volume"].to_list()
                for i, t in enumerate(times):
                    row = {
                        "open": opens[i],
                        "high": highs[i],
                        "low": lows[i],
                        "close": closes[i],
                        "volume": volumes[i],
                    }
                    index.setdefault((t, period), {})[stock_code] = row
        return index

    def _bar_open_prices(self, price_index: dict, t: datetime, period: str) -> Dict[str, Decimal]:
        """O(1) 查表取 (t, period) 下所有股票的 open 价。"""
        row = price_index.get((t, period), {})
        return {code: data["open"] for code, data in row.items()}

    def _build_bar(self, price_index: dict, t: datetime, period: str) -> BarEvent:
        """O(1) 查表取 (t, period) 的 OHLCV 构造 BarEvent（含周期标注）。"""
        return BarEvent(stocks=price_index.get((t, period), {}), bar_time=t, period=period)

    def _strategy_snapshots(
        self, portfolio: Portfolio, bar: BarEvent, snap_date: date
    ) -> List[dict]:
        """各策略日终快照。共享现金按 capital_ratio 归一化分摊，保证可加性：
        Σ(策略总净值) == 组合总净值。
        策略总净值 = 策略持股市值 + 分摊现金；分摊现金 = 组合现金 × (该 ratio / Σratio)。
        Σratio=0（极端情况）退化为均分，避免除零。
        """
        sum_ratio = sum((ctx.capital_ratio for ctx in portfolio.strategies), Decimal("0"))
        cash = portfolio.account.cash
        # 与组合层 total_value 同口径：刷新价格快照后按最近已知价估每策略持仓市值，
        # 缺席（停牌/缺 bar）沿用昨收而非按 0，保证 Σ策略市值 == 组合市值。
        portfolio._refresh_price_snapshot(bar)
        snaps: List[dict] = []
        for ctx in portfolio.strategies:
            market_value = portfolio.holdings_value(ctx)
            if sum_ratio > 0:
                allocated_cash = cash * (ctx.capital_ratio / sum_ratio)
            else:
                n = max(len(portfolio.strategies), 1)
                allocated_cash = cash / Decimal(n)
            total_value = market_value + allocated_cash
            snaps.append({
                "target_type": "strategy",
                "target_id": ctx.strategy_id,
                "snap_date": snap_date,
                "total_value": total_value,
                "cash": allocated_cash,
                "market_value": market_value,
                # 策略层持仓明细（只含本策略，供 strategy 维度 positions_json）
                "positions": portfolio.positions_detail(snap_date, ctx=ctx),
            })
        return snaps
