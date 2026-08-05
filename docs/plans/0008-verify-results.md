# 实盘引擎可行性验证结论

> 验证脚本：[main/scripts/verify_live_engine.py](../../main/scripts/verify_live_engine.py)
> 验证日期：2026-08-04（交易日 10:27-10:35 盘中）
> 关联方案：[docs/plans/0008-live-engine-simulation-pipeline.md](0008-live-engine-simulation-pipeline.md)

## 验证目的

确认 0008 实盘方案的几个关键风险点，决定方案是否需要调整。

## 结论总览

| # | 风险点 | 结论 | 影响 |
|---|---|---|---|
| 1 | TDX mode 切换 | **源码定论：import 后不可运行时切换** | get_tq 需按 mode 注入路径；回测子进程天然隔离 |
| 2 | 回调→喂数据→公式闭环 | **源码定论：内存注入，非写文件** | 链路改为 hq通知→get_market_data→formula_format_data→formula_set_data→formula_process_mul_zb |
| 3 | subscribe_hq 回调格式 | **实测：仅 `{"Code","ErrorId"}` 通知，无 OHLCV** | hq 只是「有更新」通知，行情数据需主动 get_market_data 拉 |
| 4 | get_market_data 盘中实时性 | ~~环境问题~~ → **TDX 机制：分钟线盘后下载，盘中只有昨天** | 见风险点5：盘中今天数据走 subscribe_hq+get_market_snapshot |
| 附 | subscribe_hq 上限 | **100 只硬上限** | 多组合股票并集超 100 需分批 |
| 5 | 盘中实时数据来源 | **实测：get_market_snapshot 返回今日累计日线 OHLCV（非分钟bar）** | 数据流需重设；1d 路径用 snapshot 拼 bar，1m 路径需引擎内 tick→bar 聚合器 |
| 6 | TDX 能否订阅 1m bar | **源码定论：不能。subscribe_quote 是废代码，无 K 线订阅 API** | 分钟 bar 必须引擎自行从 snapshot 聚合；1d 无需聚合 |
| 7 | subscribe_hq 推送频率 | **实测：tick 级，每分钟约 20 次（每股约10次）** | snapshot 实时更新；1d 直接覆盖拼 bar，1m 用聚合器（tick 充足） |
| 8 | snapshot Max/Min 是窗口内还是累计 | **实测：今日开盘至今累计（Max 单调不降、Min 单调不升）** | 1d 直接当日线 High/Low；1m 不能用 snapshot Max/Min |
| 9 | 1m 真 bar 是否可得 | **定论：不可得。Now 是单点采样，漏 tick；无任何真分钟 bar 来源** | **1m 实盘不可行（硬约束）**；首期只能 1d |
| 10 | iQuant 是否有 tick/分钟 bar | **源码+实测：有（xtquant.xtdata 独立模块，subscribe_quote period='tick'/'1m'）**，但需 miniQMT 客户端认证（账号+userdata_mini 不够，需 `xtminiqmt.exe` 运行） | 推翻风险点9 的 iQuant 侧限制；次期 1m 可走 iQuant，但需 NATS 网关加行情通道 + miniQMT 交易客户端启动登录（当前环境缺该进程，待用户在 iQuant 侧解决） |

## 逐项详述

### 风险点1：TDX mode 切换 — 已定论

`tqcenter.py` 源码（`initialize` / `_auto_initialize`）证实：

```python
def initialize(cls, path: str, dll_path: str = ''):
    cls._connection_path = path        # path 是连接标识，不是 TDX 目录
    ...
# _auto_initialize 里：
cls.file_name = cls._connection_path.encode('utf-8')   # 作通信句柄名
cls.run_mode = int(arguments[1])                        # run_mode 来自命令行 --run_tdx
```

- `initialize(path)` 的 `path` 是**连接标识**（用调用方文件路径作唯一标识），不是 TDX 安装目录。
- TDX 目录由 `sys.path` 注入哪个 `tqcenter.py` 决定 —— **import 后无法运行时切换**。
- `tq` 是单例类（全 `@classmethod`），同进程只能持一个 TDX 目录连接。
- `run_mode` 来自命令行 `--run_tdx` 参数，非 API 控制。

**对方案的影响**：
- `get_tq(mode)` 按 mode 把对应 `tdx_live_path`/`tdx_backtest_path` 注入 `sys.path`，但同进程只能有一个 mode 的连接。
- 回测已在 `ProcessPoolExecutor` 子进程跑 —— 天然隔离，可持 backtest mode；实盘在主进程持 live mode。**两者不冲突**。
- ⚠️ 若主进程已用 live mode 初始化 `tq`，回测子进程是独立 Python 进程、独立 `sys.path`，不受影响 —— 现有架构天然满足。

### 风险点2：回调→喂数据→公式闭环 — 已定论

用户原决策「回调 bar 写本地文件 → 触发 TQ 公式」对应的**真实 SDK API 是内存注入，不是写文件**：

- `formula_format_data(data_dict)` —— 把 `get_market_data` 返回的 OHLCV pandas DataFrame 格式化成公式可识别的 `[{Date,Open,High,Low,Close,Volume,Amount}]` 结构。
- `formula_set_data(stock_code, stock_period, stock_data, count)` —— type=0，走 `dll.TdxFuncMain` **内存注入**股票数据。
- `formula_process_mul_zb(formula_name, ...)` —— type=4，走 `dll.TdxFuncMain` 用注入的数据算公式。
- **源码无任何「写本地 .day 文件」API**（grep `append/write_file/save_kline/落盘` 均无）。

**对方案的影响**：0008 方案 §3 数据流里「回调线程写文件」应改为：

```
TQ 回调线程:
    1. 收到 hq 通知 {Code, ErrorId}（仅告知某股票有更新）
    2. asyncio.run_coroutine_threadsafe(engine.on_hq(code), main_loop)
主循环 on_hq(code):
    1. get_market_data(code, period, count=N) → 拉最新 bar（OHLCV DataFrame）
    2. formula_format_data → formula_set_data 注入
    3. formula_process_mul_zb 算公式 → signal_cache
    4. portfolio.on_bar(bar) → pending_orders
```

### 风险点3：subscribe_hq 回调格式 — 实测

盘中（10:27 交易时段）订阅 `['000001.SZ','600000.SH','000002.SZ']`，60 秒内抓到 9 次回调，**每次都是**：

```json
{"Code":"000001.SZ","ErrorId":"0"}
```

- 仅 `Code` + `ErrorId` 两个字段，**无 OHLCV / Price / Volume**。
- 推送频率约每 6-7 秒一次（9 次/60 秒），三只股票轮询。
- `subscribe_hq` 是**「行情更新通知」**（告知某股票行情有变动），**不是 bar 数据推送**。真正的 OHLCV 需在收到通知后主动 `get_market_data` 拉取。
- `subscribe_quote`（单股 K 线回调，源码 2442 行）源码注释写「暂无实际功能」，不可用。

**对方案的影响**：实盘驱动改为「**通知驱动 + 主动拉数**」而非「bar 推送驱动」。`LiveEngine` 在收到 hq 通知后调 `get_market_data` 拉最新 bar，再喂公式。这比纯 bar 推送多一次 RPC，但更可靠（拿到的是完整 OHLCV 而非增量）。

### 风险点4：get_market_data 盘中实时性 — 实测（环境问题）

盘中 10:31 调 `get_market_data(period="1m", count=3)`：

- 返回字段齐全：`['Open','High','Low','Close','Volume','Amount']`
- Close 最后 3 个时间点：`2026-07-24 14:58 / 14:59 / 15:00`
- **最新 bar 停在 2026-07-24 15:00，不是今天（8/4）** —— 滞后 11 天
- `formula_format_data` + `formula_set_data` 均成功（ErrorId=0），接口本身可用

**根因**：实盘版通达信 `D:\new_tdx64_live\TdxW.exe`（PID 9308）进程在跑，但**今天没在接收实时行情** —— `sz000001.lc1`（1m 文件）不在「8/4 今天修改」列表内，「8/1 以来修改的 lc1」为空。北交所 lday 文件有更新（可能是收盘批量），但 sz/sh 分钟数据未更新。

**这是环境问题，不是 SDK 接口问题**：实盘版通达信需要手动登录行情服务器、接收并下载今天的数据后，`get_market_data` 才能返回实时 bar。接口链路 `get_market_data → formula_format_data → formula_set_data → formula_process_mul_zb` 已验证全部走通（ErrorId=0）。

**对方案的影响**：无。实盘开发不依赖此刻有实时数据；联调时确保实盘版通达信已登录行情即可。

### 附：subscribe_hq 100 只上限

源码 2548 行：`if len(cls._sub_hq_update) > 100: raise ValueError("订阅数大于100")`。

**对方案的影响**：多组合股票并集超 100 只时需分批订阅，或按优先级裁剪。首期单组合/少股票场景不触及。

### 风险点5：盘中实时数据来源 — 实测（关键，推翻风险点4结论）

> ⚠️ 风险点4 把 `get_market_data` 停在 7/24 归为「环境问题」是**误判**。真实原因是：**通达信分钟线只能盘后下载到本地文件，盘中本地文件最新就是昨天收盘**，这是 TDX 机制不是 bug。盘中「今天的数据」必须走订阅+快照，`get_market_data` 拿不到。

盘中 11:00 探查三个接口：

**A. `get_market_snapshot(code)` — 返回 26 字段，含「今日累计」日线 OHLCV**：
```
LastClose=11.62  Open=11.58  Max=11.62  Min=11.42  Now=11.46
Volume=572771   Amount=65823.98   NowVol=130
Average=11.49   Buyp/Sellp/Buyv/Sellv=5档买卖盘   ...
```
- `Open/Max/Min/Now` = **今日开盘/最高/最低/最新价**（实时累计，Now 即当前 Close）
- `Volume/Amount` = **今日累计成交量/成交额**
- **这是日线级实时快照，不是分钟 bar**

**B. `get_pricevol(stock_list)` — 精简版**：`{stock: {LastClose, Now, Volume}}`

**C. `get_market_data(period="1m", count=2)` — 最新 bar 仍 2026-07-24 15:00**（确认：盘中分钟 bar 拿不到今天）

**官方推荐模式**（SDK 示例 `tdxdata_test.py:339-352`）：
```python
def my_callback_func(data_str):
    code_json = json.loads(data_str)             # {"Code":"...","ErrorId":"0"}
    report = tq.get_market_snapshot(code_json.get('Code'))   # ← 订阅通知后调 snapshot
```

**对方案的影响（核心设计分叉）**：
`formula_format_data` 需 OHLCV DataFrame（Open/High/Low/Close/Volume/Amount）。`get_market_snapshot` 给的是「今日累计日线快照」而非分钟 bar。两条可行路径：

1. **日线实盘路径**（最简）：策略周期用 `1d`，每日收盘前用 snapshot 的 `Open/Max/Min/Now/Volume/Amount` 拼一根「当日实时日线 bar」喂公式。snapshot 字段正好是日线 OHLCV，无需自行聚合。**适合首期**（多数策略日线级）。
2. **分钟线实盘路径**（需自行聚合）：策略周期用 1m/5m。订阅通知后每次拿 snapshot 的 `Now/Volume`，引擎本地按周期边界**自行聚合 OHLCV**（周期内第一笔 tick 价作 Open，后续 Max/Min/Last 更新 High/Low/Close，Volume 累加）。到周期结束（如整分钟）合成 bar 喂公式。**需在引擎内实现 tick→bar 聚合器**，是新增组件。

**待用户决策**：首期实盘策略周期选 1d（路径1，简单）还是 1m（路径2，需聚合器）？这决定 §3 数据流是否需新增 `BarAggregator` 组件。

### 风险点6：TDX 能否直接订阅 1m bar — 源码定论（不可订阅 K 线）

> 用户追问「通达信是否可以订阅 1m bar」。源码全面排查结论：**SDK 没有可用的 K 线订阅 API**。

SDK 仅两个订阅接口：
- `subscribe_hq(stock_list, callback)` —— 可用，但回调仅 `{Code,ErrorId}` 更新通知，**无 bar 数据**（见风险点3）。官方模式：通知后调 `get_market_snapshot` 拿实时快照（风险点5）。
- `subscribe_quote(stock_code, period, ..., callback)` —— **源码注释「暂无实际功能」，且是写了一半的废代码**：
  - 第 2511 行 `dll.SubscribeGPData(cls._get_run_id(), codestr, startimestr, endtimestr, periodstr, ...)` 引用 3 个**未定义变量**（`codestr/startimestr/endtimestr`，全文件仅此一处出现，从未赋值）
  - `dll.SubscribeGPData` **从未通过 `restype/argtypes` 注册**（DLL 导出函数清单第 28-34 行只有 `InitConnect/GetTdxDataStr/TdxFuncMain/GetOrderStr/SetMsgToMain/GetProDataInStr/Register_DataTransferFunc` 七个，无 `SubscribeGPData`）
  - 调用会直接 `NameError`，不可用
- DLL 与推送相关的导出仅 `Register_DataTransferFunc`（注册回调）+ `GetTdxDataStr` 的 type:102（`subscribe_hq` 用），**无 K 线推送通道**

**定论**：TDX SDK **不能直接订阅 1m/任意周期 K 线 bar**。盘中实时数据只能走 `subscribe_hq`（通知）→ `get_market_snapshot`（今日累计日线快照）。要分钟 bar 必须引擎自行从 snapshot 的 tick 字段聚合（风险点5 路径2）。

**对方案的影响**：
- **1d 实盘**：snapshot 的 `Open/Max/Min/Now/Volume/Amount` 正好是日线 OHLCV，直接拼 bar，无聚合器。**推荐首期**。
- **1m/5m 实盘**：必须引擎内 `BarAggregator`——按周期边界，每次 snapshot 更新 `High=max/Min=min/Close=Now/Volume累加`，周期首笔 `Open` 锁定，到周期结束合成 bar。**是新增组件**，且 snapshot 的 `Volume` 是今日累计（非单 bar 增量），聚合器需做差分（当前周期 Volume − 上一周期 Volume）。

### 风险点7：subscribe_hq 推送频率 — 实测（tick 级，每分钟约 20 次）

盘中 11:10-11:11（交易时段）订阅 `['000001.SZ','600000.SH']`，统计 60 秒：

- **共 20 次回调**（000001.SZ 10 次 + 600000.SH 10 次，两只股票轮询）
- snapshot 的 `Now/Volume/Max/Min` **每次都在变**：000001.SZ 10 次拉取出现 9 种不同 Volume 值，600000.SH 8 种 → **实时更新，非固定值**
- 首次 11:10:47.467，末次 11:11:35.631 → 约 48 秒 20 次，**每只股票每分钟约 10 次 snapshot 更新**

**定论**：`subscribe_hq` 是 **tick 级高频通知**（不是分钟级推送）。每分钟每只股票约 10 条 snapshot 记录。snapshot 的 `Open/Max/Min/Now/Volume/Amount` 是今日累计值，随每笔 tick 实时刷新。

**对方案的影响**：
- **1d 路径**：每次 snapshot 都能拿到最新「今日累计日线 OHLCV」，可直接拼当日实时日线 bar 喂公式。推送虽高频（约 10 次/分钟/股），但日线 bar 只需「覆盖更新」（每次用最新 snapshot 覆盖当日 bar 的 High/Min/Close/Volume），**无聚合复杂度**。可在每个通知后触发公式，或按固定频率（如每分钟）触发一次。
- **1m 路径**：tick 级推送（约 10 次/分钟/股）正是 `BarAggregator` 的输入。聚合器需：①周期首笔 tick 锁 Open；②每 tick 更新 High=max/Min=min/Close=Now；③Volume 做差分（当前累计 − 上周期末累计）；④到整分钟边界合成 1m bar 喂公式。**可行**，且推送频率足以保证 1m bar 粒度准确。

### 风险点8：snapshot Max/Min 是「时间窗口内」还是「今日累计」 — 实测（今日累计）

盘中 11:16（交易时段）盯住 `000001.SZ`，每 2 秒拉一次 snapshot，共 15 次（约 30 秒），记录 `Open/Max/Min/Now/Volume` 轨迹：

```
  #  时间        Open    Max    Min    Now     Volume
  1  11:16:28    11.58   11.62  11.42  11.47    616498
  2..15          11.58   11.62  11.42  11.47    616498   (15 次完全相同)
```

- **Max 单调不降**：15 次全为 11.62，从未回落 → ✅ 累计
- **Min 单调不升**：15 次全为 11.42，从未上升/重置 → ✅ 累计

**判定为「今日累计」的三重依据**（本次 30 秒窗口未观察到 Max 上台阶后不回落的「强证据」，故综合判定）：
1. **字段语义**：`Open/Max/Min/Now` 文档明确为「今日开盘/最高/最低/最新」，与已实测确认累计的 `Volume/Amount` 同属当日累计字段族——同一快照里量额累计而价位却是窗口内的，几乎不可能。
2. **不回退/不重置**：15 次轮询 Max/Min 一次都没回落或重置（若是窗口内，跨窗口边界应观察到重置回当前 Now）。
3. **stage4 旁证**：stage4 实测 Now/Volume 每次回调都在变（10 次 9 种 Volume），证明快照本身在实时刷新；本次 Max/Min 不动是因为 Now(11.47) 没突破当日极值(11.62/11.42)，并非快照未更新。

**对方案的影响（直接决定 1d vs 1m 数据流）**：
- **1d 路径**：snapshot 的 `Open`=当日开盘、`Max`=当日最高、`Min`=当日最低、`Now`=最新价(实时 Close)、`Volume/Amount`=当日累计 —— **一发 snapshot 即拼出「当日实时日线 bar」**，喂 `formula_format_data`。零聚合，最干净。`Max/Min` 由通达信内部用完整 tick 流聚合，是当日真高低，准确。
- **1m 路径（见风险点9，不可行）**：见下文。

> ⚠️ 风险点 2/3 旧结论里写的「hq 通知 → `get_market_data` 拉最新 bar」与风险点 4/5 的修正矛盾：盘中 `get_market_data` 拿不到今天数据，真正的盘中数据流是 **`subscribe_hq` 通知 → `get_market_snapshot` 拉今日累计快照**（见风险点5 官方模式）。风险点 2 描述的 `formula_format_data→formula_set_data→formula_process_mul_zb` 三步链路本身正确，只是输入数据源应从 `get_market_data` 改为 `get_market_snapshot`（1d 路径）。

### 风险点9：1m 真 bar 是否可得 — 定论（不可得，硬约束）

> 用户指出「snapshot now 只是一次性读 无法集合」。排查后确认这是 **1m 实盘的根本性障碍**，非「加聚合器」可解决。

`get_market_snapshot` 的 `Now` 是**调用那一刻的最新价（单点读），不是 tick 序列**——两次调用之间的所有 tick 永远看不到。三条都堵死 1m 真 bar：

1. **采样漏 tick**：`subscribe_hq` 回调约 10 次/分钟/股（stage4 实测），即每分钟最多约 10 个 `Now` 采样点。活跃股票每分钟真实 tick 远多于 10，大量 tick 被漏。从采样到的 `Now` 拼 1m 的 High/Low 只是「这 ~10 个采样点的 max/min」，波动大的分钟真实高低可能全落在没采到的 tick 上。
2. **Max/Min 差分救不了**：snapshot 的 `Max/Min` 是当日累计，对大多数不破当日极值的分钟，差分（本分钟末累计 − 上分钟末累计）得不出本分钟的高低——只有「破了当日新极值的那一分钟」差分才有意义，覆盖率极低。
3. **无真分钟 bar 来源**：`subscribe_quote` 是废代码（风险点6）、`get_market_data` 盘中只有昨天（风险点4）、`snapshot.Now` 是漏 tick 的采样——SDK 内没有任何能给出真分钟 OHLCV 的通道。

**定论**：在此 SDK 下 **1m 实盘策略无真 bar 来源，不可行（硬约束，非「首期先做简单的」）**。首期只能 1d。

**对方案的影响**：
- 实盘首期范围**限定 1d**（且次期也需重新评估 1m 是否值得做——除非接入 iQuant 后有真 tick/分钟 bar 通道）。
- §3 数据流按 1d 落地：`subscribe_hq` 通知 → `get_market_snapshot` → 拼「当日实时日线 bar」→ `formula_format_data` → `formula_set_data` → `formula_process_mul_zb`。
- 不引入 `BarAggregator`（1m 专用，且做了也不准）。
- 策略周期=1d 的现有策略可直接上实盘；周期为 1m/5m 的策略需在实盘前确认是否改周期或暂不上实盘。

### 风险点10：iQuant 端是否有真 tick/分钟 bar — 源码+实测（有接口，但需认证）

> 用户要求「去 iquant 目录下探索」「探索 iquant 是否有 tick 数据」。结论：**iQuant 有完整 tick/分钟 bar 行情接口，且可在独立进程调用（非 ContextInfo 专属），推翻风险点9「1m 无真 bar 来源」在 iQuant 侧的限制**——但启用需交易端认证，受限于账号登录。

**A. iQuant Python API 文档**（`D:\iQuant\HTML\guosenPythonApiHelp\iQuant_Python_API_Doc.html`）列出的行情接口（`ContextInfo` 方法，跑在 iQuant 策略进程内）：
- `subscribe_quote(stock_code, period, dividend_type, callback)` — `period` 可选 **`'tick'`（分笔）**、`'1m'`/`'5m'`/`'15m'`、`'1d'`，还有 `'l2quoteaux'`（L2 快照）/`'l2transactioncount'`（L2 大单，需 L2 权限）。回调收 `{code: pd.DataFrame}`
- `get_full_tick(stock_code=[])` — 拉最新分笔，返回 `lastPrice/open/high/low/askPrice1-5/bidPrice1-5/askVol/bidVol` 等
- `unsubscribe_quote(subId)` / `get_all_subscription()`

**B. 关键发现：`xtquant.xtdata` 是独立行情模块，脱离 ContextInfo 可用**（`D:\iQuant\bin.x64\Lib\site-packages\xtquant\xtdata.py`）。`mpython/pythonbalance.py` 直接 `from xtquant.xtdata import get_full_tick`——证明可在 iQuant 策略进程外调用。主要函数：
| 函数 | 能力 |
|---|---|
| `subscribe_quote(stock_code, period='tick'/'1m'/'1d', callback)` | 订阅行情，回调 `datas={stock:[data1,...]}`，返回订阅号 |
| `get_full_tick(code_list)` | 拉盘口 tick `{stock:{lastPrice,五档,...}}` |
| `get_market_data_ex(field_list, stock_list, period, count)` | 拉 K 线，`period='tick'` 返回 numpy ndarray 序列 |
| `unsubscribe_quote(seq)` / `run()` | 反订阅 / 阻塞接收回调 |
| `get_l2_quote`/`get_l2_order`/`get_l2_transaction` | Level2 实时行情/委托/成交 |

连接机制：`get_client()` 自动连 `127.0.0.1:58610`（iQuant 行情服务，`xtdata.ini` 配置），无需手动 connect。

**C. 实测**（验证脚本 `live/scripts/verify_iquant_quote.py`，用 `live/` 的 Python 3.7）：
- ✅ `from xtquant import xtdata` 在独立进程 import 成功（pyd 是 cp37，`live/` 环境匹配）
- ✅ `get_client().is_connected() = True` — TCP 连上 58610（iQuant 客户端在跑，端口监听中）
- ❌ `get_full_tick(['000001.SZ'])` 返回 `not authenticated` 错误 — **行情服务要求认证，光 TCP 连接不够**

**D. 认证机制**（`xttrader.py` + `mpython/pythonbalance.py`）：
iQuant 认证通过交易端完成：`XtQuantTrader(userdata_mini_path, session).start() → .connect() → .subscribe(StockAccount(account))`。`userdata_mini_path` = `D:/iQuant/userdata_mini`（已存在，含 `miniqmtShmQuoteCache` 行情共享内存），`account` = 交易账号。**交易端登录后行情端才有权限**。账号通过命令行传入（`pythonbalance.py` 从 `sys.argv` 拿），不在配置文件。

**E. 用账号 110002348760 实测交易端认证 — 仍失败，根因定位**（2026-08-04 12:07-12:18 盘中）：

验证脚本加 `XtQuantTrader(D:/iQuant/userdata_mini, session).start().connect().subscribe(StockAccount("110002348760"))` 后重测：
- ❌ `trader.connect() = -1`（交易端连接失败，非 0）
- ❌ 认证后 `get_full_tick` 仍 `not authenticated`

**进程/端口实测**（`wmic` + `netstat`）：
| 端口 | 监听进程 | 可执行路径 | 角色 |
|---|---|---|---|
| 58610（行情） | PID 10440 | `D:\iQuant\bin.x64\miniquote.exe` | mini 行情客户端 |
| 58600（交易） | PID 12776 | `D:\iQuant\bin.x64\XtItClient.exe` | 主客户端 |

- 主客户端 `XtItClient.exe` **已登录账号 110002348760**（`userdata/log/XtClient_20260804.log` 有 `queryHistoryData ... account: 110002348760` 记录）—— 账号正确且已登录。
- `bin.x64` 下 **无 `xtminiqmt.exe` / `XtMiniQmt.exe`**（只有 `miniquote.exe` + `XtItClient.exe` + `BrokerProxy.exe`）。`config/xtminiqmt.lua` 配置存在（`appName="XtMiniQmt"`）但对应可执行文件未安装。
- `miniquote` 行情日志（`userdata_mini/log/XtMiniQuote_20260804.log`）：`IPythonApiServer::set rpc auth=true`（多次）+ 每次 xtdata 连上后 `IPythonApiServer::onConnected` 紧接 `IPythonApiServer::onError`（error 10054 对端强关）→ **行情服务端要求 RPC 认证，xtdata 客户端 TCP 连上后被以认证为由断开**。
- `%USERPROFILE%\.xtquant` 目录**不存在**，`xtdata_config.client_guid=""` → `load_global_config()` 返回空，xtdata 走默认 58610 连接但无 RPC 认证握手。

**根因定论**：iQuant 的 `xtquant`（xtdata 行情 + xttrader 交易）接口设计为**由 miniQMT 客户端 spawn 的策略进程使用**（`pythonbalance.py` 的 `by_cmd=0` 模式：`sys.argv` 由客户端填 `ud_path/session/account`，客户端预先在 `.xtquant\{guid}\` 建好 `xtdata.cfg`+`running_status` 完成认证握手）。**外部独立进程直连 58610/58600 会被 RPC 认证拒绝**，光有账号 + `userdata_mini` 路径 + `XtQuantTrader.connect()` 不够 —— 需要 **`xtminiqmt.exe`（miniQMT 交易客户端）独立运行并登录**，由它建立 58600 交易连接 + 58610 行情 RPC 认证上下文。

**联网核实**（Tavily 搜 `xtquant get_full_tick not authenticated`）：
- SkillHub 文档明确：「⚠️ Requires miniQMT running locally. xtdata communicates with miniQMT via TCP」
- 国金QMT趟坑：miniQMT 是独立 `xtminiqmt.exe` 进程，需 `xtminiqmt.exe linkMini` 单独登录
- 迅投知识库：`get_full_tick` 取「全推数据」是标准用法，前提 miniQMT 在跑

**当前环境缺的是 miniQMT 交易客户端进程**（`xtminiqmt.exe` 未安装/未启动），主客户端 `XtItClient.exe` 登录的账号不能替代它。这是 iQuant 客户端的部署/登录配置问题，需用户在 iQuant 客户端侧解决（安装/启动 miniQMT 模式，或确认本版 iQuant 是否集成 miniQMT 并开启 Python API 权限），非代码层可解决。

**对方案的影响（重大）**：
- **iQuant 侧 1m/5m/tick 真 bar 可行**——`subscribe_quote(period='1m')` 能推真分钟 bar DataFrame，`period='tick'` 推真分笔。这**推翻风险点9「1m 无真 bar 来源」的 iQuant 侧限制**（TDX 侧仍不可行）。
- **但需架构变更**：现有 NATS 网关（`live/iguant_gateway/main.py`）只有 5 个交易 subject，**无行情通道**。要让 Core 用 iQuant 行情，需：
  1. 网关用 `XtQuantTrader` 登录交易端完成认证（需账号 + `userdata_mini` 路径）
  2. 网关用 `xtdata.subscribe_quote(period='1m'/'tick')` 订阅，回调数据经 NATS 推回 Core
  3. Core 侧新增行情 subject + 消费逻辑
- **前置条件（硬约束）**：上述架构变更成立的前提是 **miniQMT 交易客户端（`xtminiqmt.exe`）在网关进程所在机器上运行并登录账号**。当前环境该进程缺失，需用户先在 iQuant 侧部署/启动 miniQMT 并确认 `xtdata.get_full_tick` 不再报 `not authenticated`，才能继续 iQuant 行情方案。
- **待用户确认**：①是否走 iQuant 行情（改架构）还是仍走 TQ-1d（首期简单）；②iQuant 账号/`userdata_mini` 配置怎么提供给网关；③是否有 L2 权限（决定能否用 `l2quoteaux`/千档盘口）；④**miniQMT 交易客户端能否在本机启动并登录**（决定 iQuant 行情方案是否可行）。
- 验证脚本 `verify_iquant_quote.py` 已就绪（支持 `--account` CLI 传账号 + `XtQuantTrader` 交易端认证），C/D/E（subscribe 推送 + 历史 1m）需先在 iQuant 侧解决 miniQMT 认证才能跑通。

## 对 0008 方案的修订

基于验证结论，方案 §3 数据流与 §5 复用表需修订：

1. **数据流改为「通知驱动 + 主动拉数」**（非 bar 推送驱动，非写文件）
2. **数据源修正**：盘中实时数据走 `subscribe_hq` 通知 → `get_market_snapshot`（今日累计日线 OHLCV），**非 `get_market_data`**（后者盘中只有昨天，风险点4/5）。`get_market_data` 仅用于历史/盘后数据与回测
3. **首期范围限定 1d**（风险点9 硬约束，TDX 侧）：snapshot 一发即拼当日日线 bar，零聚合、零差分、无新组件。**不引入 `BarAggregator`**；1m/5m 策略暂不可上实盘
4. **次期 1m 可走 iQuant 行情**（风险点10）：iQuant `xtquant.xtdata` 有真 tick/1m bar 订阅，推翻 TDX 侧的 1m 限制，但需 NATS 网关加行情通道 + 交易端认证。**待用户决策是否改架构**
5. **`get_tq(mode)` 实现确认**：按 mode 注入对应路径，主进程 live / 回测子进程 backtest 天然隔离（现有架构满足）
6. **100 只订阅上限**：首期不触及，文档标注
7. **联调前置条件**：实盘版通达信需登录行情服务器、接收当日数据；iQuant 行情需交易端认证（账号 + `userdata_mini`）

## 验证脚本复用

- 阶段1（源码探查）：任何时候可跑，`uv run python scripts/verify_live_engine.py --stage 1`
- 阶段2（连真机）：需通达信在跑，`uv run python scripts/verify_live_engine.py --stage 2`
  - 完整实时性验证需实盘版已登录行情接收当日数据
- 阶段3（实时快照字段探查）：`uv run python scripts/verify_live_engine.py --stage 3`
  - 探查 `get_market_snapshot`/`get_pricevol` 盘中返回字段，见下「风险点5」

## 逐项详述（续）
