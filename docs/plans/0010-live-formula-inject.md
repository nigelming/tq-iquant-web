# 0010 实盘公式计算接入 — 内存注入链路（桥拉 bar → formula_set_data → 公式 → signal_cache）

> 状态：**两项前置验证均已通过**（`verify_formula_inject.py` 内存注入 PASS + `verify_quote_history.py` 桥历史拉取 PASS，2026-08-05 真机），待实施
> 日期：2026-08-05
> 接续：0009 切片4 已打通 LiveEngine 端到端（BarPoller → Portfolio.on_bar → HttpBridgeDispatcher 下单 → 落库 + 持仓恢复），但 `LiveEngine.signal_cache` 仍是空字典（`live_engine.py:56`），**公式信号链路未接通**——实盘只有风控信号（止损/止盈/移动止损，由 `Portfolio._check_risks` 生成）能触发下单，公式信号（OPEN/ADD/REDUCE/CLOSE）全部哑火。本计划补上这条链路。

## 1. 背景与目标

### 1.1 问题：实盘公式计算闭环中断

回测公式计算走 `TQFormula.compute` → `tq.formula_process_mul_zb`（SDK **自取 `.lc1` 本地分钟文件**自算，`backtest.py:394`），预先算全段信号填 `signal_cache`，再逐 bar 查表（`backtest_engine.py:68`）。

实盘的 bar 来自 iQuant 桥 `xtdata.get_market_data_ex`（有今天实时），但公式引擎在通达信侧（读 `.lc1`，盘中只有昨日及更早，今天实时不入 `.lc1`）。**两个 SDK、两个数据源，闭环断在「实盘实时 bar 喂不进通达信公式引擎」**。

### 1.2 已验证的解法：内存注入（不写本地文件）

`verify_formula_inject.py`（`main/scripts/`）真机验证：用 `MACROSSPRO` 公式对比「正常自取调用」vs「内存注入调用」：

| 周期 | 正常调用 1 的个数 | 注入调用 1 的个数 | 逐条对比 |
|---|---|---|---|
| 1m | 17 | 17 | 200 条全等 |
| 5m | 16 | 16 | 200 条全等 |

链路：`get_market_data`（取 OHLCV）→ `formula_format_data`（转公式格式）→ `formula_set_data`（type=0 内存注入）→ `formula_process_mul_zb`（type=4 算公式）。**注入数据被公式引擎完全等价采用**，非虚假一致（两侧都有真实信号且分布相同）。

### 1.3 关键决策：实盘复用回测版通达信，不分 live 版

用户决策：**内存注入不读写本地 `.lc1` 文件**，当初分 `new_tdx64`（回测版）/ `new_tdx64_live`（实盘版）是为了「实盘写本地 1m/5m 文件怕污染回测版」而隔离。现在内存注入替代了写文件，隔离理由消失——**实盘连回测版 `new_tdx64` 即可**。

> **2026-08-06 更新**：已彻底放弃 live 版，全系统只用回测版 `new_tdx64`。`config.yaml` 的 `tdx_live_path` 已删除，统一为 `tdx_path`；`get_tq()` 只连回测版目录。

- 实盘 `LiveEngine` 主进程用 `get_tq()`（`tq/utils.py`，连回测版 `new_tdx64`）算公式
- 回测同步在请求线程跑（`backtest.py:744`，**未用 ProcessPoolExecutor**，CLAUDE.md 描述过时），与实盘同进程
- 两者共用 `get_tq()` 单例连接 + `get_tdx_lock()` 串行化（`tq/formula.py:13`），不并发冲突；单用户系统本就不会同时跑回测+实盘

### 1.4 目标

实盘每根 bar 到达时：拉历史 N 根 bar（桥 `/quote`）→ 内存注入 → 算公式 → 取最新一根信号 → 填 `signal_cache` → `Portfolio.on_bar` 命中公式信号下单。**不改 Portfolio/StrategyContext/BacktestEngine**，改动隔离在 `LiveEngine` + 新增 `TQFormula.compute_injected`。

## 2. 已验证结论

### 2.1 内存注入链路（`verify_formula_inject.py`，2026-08-05 真机）
- `formula_format_data(data_dict)`：要求 `{Amount/Volume/Close/Open/High/Low: pandas.DataFrame}`，列=股票代码，行=时间（`tqcenter.py:2999`）
- `formula_set_data(stock_code, stock_period, stock_data, count, dividend_type=0)`：type=0 注入内存（`tqcenter.py:3066`），`count` 须 ≤ `len(stock_data)` 且 ≤ 24000
- `formula_process_mul_zb`：type=4 算公式，注入后再调用会**用注入数据**（已验证等价）
- 公式输出：`{stock_code: {var_name: [{"Date":"YYYYMMDD","Value":float}, ...]}}`，`Value` 经 `_to_int` 转 int（`backtest.py:155`）

### 2.2 signal_cache 契约（回测/实盘共用）
- key：`(strategy_id, stock_code, bar_time)`，`bar_time` 是 `datetime`（`strategy_context.py:57`）
- value：`[{"name": str, "value": int}]`
- `StrategyContext.get_signal` cache 优先，miss 且无 `tq_compute` 回调 → 空信号（`strategy_context.py:58-64`）；`_match_signals` 按 `formula_signals` 配置匹配 `trigger_value`（`strategy_context.py:68-88`）
- `Portfolio.on_bar(bar, signal_cache=...)` 透传给 `get_signal`（`portfolio.py:77`）——**实盘只要在调 `on_bar` 前填好 `signal_cache` 即可命中**

### 2.3 实盘逐 bar 算（vs 回测预先算全部）
- 回测 `build_signal_cache` 一次性算全段（`backtest.py:368`），分钟级按 `_bar_times_by_code` 索引对齐（`backtest.py:315`）
- 实盘 bar 逐根到达（`BarPoller.poll` 相对变化判定，`bar_poller.py:126`），**不能预先算**。但实盘只关心**当前这根 bar** 的信号——注入历史 N 根算出 N 条输出，**取最后一条**即当前 bar 信号，避开索引对齐复杂性

## 3. 已验证（实施前必跑，已通过 2026-08-05）

### 3.1 桥 `/quote` 能拉前几天历史（`verify_quote_history.py` ✅ PASS）
实盘公式注入需要足够长历史（均线公式要 N 根）。桥 `_fetch_quote` 用 `xtdata.get_market_data_ex(count=N)` + `download_history_data(HISTORY_DAYS=30)`（`iquant_bridge.py:247-257`），**已实机验证 PASS**：

```bash
cd main
uv run python scripts/verify_quote_history.py --code 600000.SH --count1m 1200 --count5m 240
```

**结果（2026-08-05 17:56 真机）**：
- 1m count=1200 → 返回 1200 根，最早 `2026-07-30 09:35`，最晚 `2026-08-05 15:00`，跨度 6 天 → 能拉前几天
- 5m count=240 → 返回 240 根，最早 `2026-07-30 09:35`，跨度 6 天 → 能拉前几天
- 判定：**PASS**，实盘注入数据充足，按本计划实施

**修复的桥 bug（验证过程中发现并修复，`iquant_bridge.py`）**：原缓存 key 不含 count（`(code, period)`）且定时刷新固定用 `QUOTE_COUNT=10`，导致大 count 请求被 10 根缓存钉死、永远只返回当天 10 根。修复：缓存 key 加 count → `(code, period, count)`；`_refresh_quote_cache` 只刷 `count == QUOTE_COUNT` 的 key，大 count 历史条目不被覆盖。**验证脚本也修了 bar 时间字段识别**（桥返回 `index`/`time` 字段，非 `stime`）。


## 4. 架构

```
┌─ LiveEngine._handle_bar(portfolio, bar) ───────────────────────────┐
│  ① _fill_signal_cache(portfolio, bar)   ← 新增                       │
│     对每个策略 × bar.stocks 每只股票:                                 │
│       bridge.query_quote(code, period, count=200) 拉历史+实时 bar     │
│       → 拼 pandas DataFrame(Amount/Volume/Close/Open/High/Low)        │
│       → TQFormula.compute_injected(formula_name, df, period)          │
│           formula_format_data → formula_set_data → process_mul_zb     │
│       → 取最后一条输出 → 填 signal_cache[(sid,code,bar.bar_time)]     │
│  ② portfolio.on_bar(bar, signal_cache=self.signal_cache)  ← 命中公式信号│
│  ③ engine.execute → 桥下单 → 落库（0009 切片4 已就绪）                 │
└─────────────────────────────────────────────────────────────────────┘
        │ bridge HTTP /quote                    │ get_tq() 回测版通达信
        ▼                                       ▼
┌─ iQuant 桥 (127.0.0.1:8790) ──┐    ┌─ 通达信 new_tdx64 ────────────┐
│  xtdata.get_market_data_ex     │    │  formula_set_data 内存注入     │
│  count=N 拉历史+实时 bar        │    │  formula_process_mul_zb 算公式 │
└────────────────────────────────┘    └────────────────────────────────┘
```

## 5. 实现清单

### 5.1 新增 `TQFormula.compute_injected`（`main/core/tq/formula.py`）
封装"注入 + 算"链路（复用 `verify_formula_inject.py` 的 `_run_injected` 逻辑）：
```python
def compute_injected(self, formula_name, ohlcv_df, stocks, period,
                     dividend_type=1, formula_arg="") -> Optional[dict]:
    """内存注入算公式。ohlcv_df: {Amount/Volume/Close/Open/High/Low: DataFrame}。
    返回 formula_process_mul_zb 的 raw（同 compute）。"""
    with get_tdx_lock():
        tq = get_tq()
        formatted = tq.formula_format_data(ohlcv_df)
        for code in stocks:
            sd = tq.formula_set_data(code, period, formatted[code],
                                     len(formatted[code]), dividend_type=0)
            if not sd or sd.get("ErrorId") != "0":
                return None
        return tq.formula_process_mul_zb(
            formula_name=formula_name, formula_arg=formula_arg,
            return_count=-1, return_date=True, xsflag=-1,
            stock_list=stocks, stock_period=period,
            start_time="", end_time="", count=-1, dividend_type=dividend_type,
        )
```
- 注入后 `process_mul_zb` 传 `count=-1` 不传时间范围（让公式用注入的全部数据）
- `get_tdx_lock()` 串行化，与回测互不并发

### 5.2 新增 `LiveEngine._fill_signal_cache`（`main/core/engine/live_engine.py`）
```python
def _fill_signal_cache(self, portfolio, bar):
    """实盘逐 bar 算公式信号填 signal_cache。预填模式（不改 Portfolio）。"""
    for ctx in portfolio.strategies:
        formula = self._formula_by_strategy.get(ctx.strategy_id)  # 预加载
        if formula is None:
            continue
        period = ctx.period
        for code in bar.stocks:
            bars = self._dispatcher.query_quote(code, period=period, count=self._formula_count)
            df = _bars_to_formula_df(bars, code)  # 桥 bar → pandas DataFrame
            if df is None:
                continue
            raw = self._tq_formula.compute_injected(formula.name, df, [code], period)
            outputs = _extract_latest_signal(raw, code)  # 取最后一条 → [{"name","value"}]
            if outputs:
                self.signal_cache[(ctx.strategy_id, code, bar.bar_time)] = outputs
```
- `_formula_count`：注入历史根数（1m 默认 200，5m 默认 200；可配），够均线预热
- `_formula_by_strategy`：启动时从 DB 预加载 `{strategy_id: Formula}`，避免每 bar 查库
- 同 (formula_name, period, code) 多策略可优化缓存，首期按策略遍历（与回测 `build_signal_cache` 一致）

### 5.3 新增辅助函数
- `_bars_to_formula_df(bars, code)`：桥 bar dict 列表 → `{Amount/Volume/Close/Open/High/Low: pandas.DataFrame}`（列=[code]，行=DatetimeIndex）。桥 bar 字段 `open/high/low/close/volume/amount`（小写）→ 首字母大写；时间从 `stime`/`time` 解析
- `_extract_latest_signal(raw, code)`：从 `formula_process_mul_zb` 返回取**最后一条** bar 的 `{var_name: value}` → `[{"name": var, "value": int}]`。复用 `backtest.py:_convert_formula_output` 的 `ErrorId` 校验 + `_to_int`，但只取最后一根（实盘逐 bar 算，无需索引对齐全段）

### 5.4 `LiveEngine.__init__` 加配置 + 预加载
- 加 `tq_formula: TQFormula`（复用 `get_tq()` 单例）
- 加 `_formula_count: int`（注入历史根数，默认 200）
- 加 `_formula_by_strategy: Dict[int, Formula]`（启动时从 DB 预加载，`api/live.py:_build_engine` 传入或 LiveEngine 自己查）

### 5.5 `LiveEngine._handle_bar` 接入
在 `orders = portfolio.on_bar(...)` 前加 `self._fill_signal_cache(portfolio, bar)`（`live_engine.py:129` 前）。

### 5.6 `api/live.py:_build_engine` 传 TQFormula + Formula 预加载
组装 LiveEngine 时传入 `TQFormula()` 实例 + 各策略的 Formula 映射（从 DB 查 `Strategy.formula_id` → `Formula`）。

### 5.7 复用的现有函数（不重写）
- `HttpBridgeDispatcher.query_quote`（`http_bridge_dispatcher.py:142`）— 拉 bar，已支持 count 参数
- `BarPoller.parse_bar_time`（`bar_poller.py:40`）— bar 时间解析，复用
- `backtest._to_int` / `_FORMULA_META_KEYS`（`backtest.py:155,270`）— 公式输出转 int，可 import 复用
- `Portfolio.on_bar` / `StrategyContext.get_signal`（不改，预填 cache 即命中）

## 6. 测试（TDD）

### 6.1 单测 `test_live_engine.py`（补充）
- `test_fill_signal_cache_populates_cache`：mock dispatcher.query_quote 返回固定 bars + mock TQFormula.compute_injected 返回固定 raw → `_fill_signal_cache` → 断言 `signal_cache[(sid,code,bar_time)]` 有正确 `[{"name","value"}]`
- `test_handle_bar_with_formula_signal_trades`：构造 portfolio（OPEN 信号配置）+ mock 公式返回最后一条 OPEN=1 → `_handle_bar` → 断言 live_trades 落了 BUY（公式信号触发，非仅风控）
- `test_bars_to_formula_df_field_mapping`：桥 bar dict（小写字段）→ DataFrame（大写字段 + DatetimeIndex）正确
- `test_extract_latest_signal_takes_last_bar`：raw 多条输出 → 只取最后一条

### 6.2 单测 `test_tq_formula.py`（新建或补充）
- `test_compute_injected_calls_set_data_then_process`：mock tq，断言先 `formula_set_data` 再 `formula_process_mul_zb`，且 process 传 `count=-1`

### 6.3 集成测 `test_live_engine_api.py`（补充）
- `test_start_session_fills_formula_mapping`：`_build_engine` 后 LiveEngine 持有 `_formula_by_strategy`

## 7. 验证

1. **桥历史拉取**（实施前必跑）：`uv run python scripts/verify_quote_history.py` → PASS 才继续
2. **内存注入**（已验证）：`uv run python scripts/verify_formula_inject.py` → PASS（已通过）
3. **单测**：`cd main && uv run pytest core/tests/unit/test_live_engine.py core/tests/unit/test_tq_formula.py -v` → 全绿
4. **集成测**：`uv run pytest core/tests/integration/test_live_engine_api.py -v` → 全绿
5. **全量回归**：`uv run pytest` → 既有不回归（6 个 backtest TDX 环境失败保持不变）
6. **E2E（用户本地，需 iQuant 桥 + 通达信回测版）**：
   - 起桥 → Core 起实盘 session → 拉 1m bar → `_fill_signal_cache` 注入算 MACROSSPRO → 公式信号触发下单 → live_trades 落库
   - 对比实盘信号与 `verify_formula_inject.py` 注入调用结果一致

## 8. 已知限制 / 后续

- **注入历史根数**：1m count=200 不到 1 天，均线公式可能预热不足。若 E2E 信号异常，调大 count（1m→1200 约 5 天）。`_formula_count` 可配
- **成交价近似**：首期成交价用 `bar.close` 记账（0009 切片4 已知近似），prType=14 实际成交价是盘口一档价，切片5 `/deals` 回填
- **桥拉取重复**：BarPoller 拉 `/quote` 判完成 + `_fill_signal_cache` 再拉一次算公式，同一只股票每 bar 拉两次。桥有 1s 缓存（`QUOTE_CACHE_TTL=1`），影响可控；后续可优化复用 BarPoller 数据
- **多策略重复算**：同股票池多策略按策略遍历重复算公式（与回测一致），首期可接受；后续按 (formula_name, period) 缓存
