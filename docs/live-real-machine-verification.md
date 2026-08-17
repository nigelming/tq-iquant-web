# Live 真机验证清单

> 生成：2026-08-17。覆盖 commit 9c3284c 三处修复 + 既有挂起项的真机验证缺口。
> 用法：每个交易日跑完后，对照本清单勾选触发到的项；触发条件部分不可控（iQuant 侧丢单/收盘后信号），自然触发到才验，不强造。
> 交叉引用：[live-flow-checklist.md](live-flow-checklist.md)（实现状态）、`logs/iquant-list/YYYYMMDD/reconcile-report.md`（每日对账）。

## 状态图例

| 标记 | 含义 |
|---|---|
| ⏳ | 待真机验证（代码+单测已就位，未真机触发） |
| ✅ | 已真机验证通过 |
| ❌ | 真机验证失败（有 bug） |
| — | 当日未触发（条件没发生，不算通过也不算失败） |

---

## P1 — 三处执行异常修复（commit 9c3284c）

> 代码 + 110 单测全绿（`uv run pytest core/tests/unit/test_live_engine.py`）。
> **2026-08-17 对账是平稳日，2 笔买入全正常成交，三条修复路径均未触发**——代码绿不等于真机生效，须自然触发验证。

### 验证 1 — 受理即丢弃即时检测 ⏳

**修复**：两桥 `_do_place` 加 `_confirm_order_arrival`（`iquant_bridge.py:228` / `iquant_bridge_live.py:228`，双桥手动同步）。passorder 返回 0 后 sleep 0.3s + 按 remark 回查 ORDER 表，无记录 → `ok:false` + `"no order record (dropped)"`，Core 走既有 `BridgeOrderRejected` 即时标 rejected。

**触发条件**：iQuant 侧再次丢单（passorder result=0 但 ORDER 表无记录）。不可控——14:xx 主循环异常/网络抖动时易发（2026-08-14 id36-39 即此模式）。

**观测点（触发到时逐项确认）**：

| # | 位置 | 应看到 | 命令/查法 |
|---|---|---|---|
| 1 | 桥日志 `Strategy_Log.md` | `ORDER <remark> ... ok result=0`（passorder 受理）后**无**对应成交 | Read `logs/iquant-list/YYYYMMDD/Strategy_Log.md` |
| 2 | iQuant `Order.csv` | 该 remark 的任务编号**缺失**（委托未生成） | Read `logs/iquant-list/YYYYMMDD/Order.csv`，grep remark |
| 3 | DB `live_orders` | `status=rejected`、`order_ref=NULL`、`error_message` 含 `no order record (dropped)` | `SELECT id,stock_code,status,order_ref,error_message FROM live_orders WHERE error_message LIKE '%no order record%'` |
| 4 | DB 时间差 | `updated_at - created_at` ≤ **~1s**（不再是 180s/440s） | `SELECT id,strftime('%s',updated_at)-strftime('%s',created_at) sec FROM live_orders WHERE error_message LIKE '%no order record%'` |
| 5 | SSE 事件 | order 事件 `status=rejected` + `error_message` 含 dropped 文案 | 前端实盘工作台事件日志 |

**通过判据**：#1+#2 证实丢单（iQuant 侧根因）+ #3 error 含 `no order record (dropped)` + #4 时间差 ≤1s。四项全中 = 即时检测生效。

**失败判据**：error 仍是 `order match timeout: no bridge order_ref within XXXs`（说明走了旧超时路径，`_confirm_order_arrival` 没生效或桥文件没更新）。

**难处**：丢单随机不可控，可能长期不触发。可主动做的是**桥冒烟确认正常路径不误伤**（见验证 1b）。

---

### 验证 1b — 桥到达确认正常路径不误伤 ⏳（可主动验）

**目的**：确认 `_confirm_order_arrival` 在正常下单（iQuant 正常生成委托）时 0.3s 回查能找到记录、返回 `ok:true`，不把正常单误判成 dropped。

**前置**：两桥文件已粘进 iQuant 策略，实盘交易模式启动（仿真桥 8790 / 实盘桥 8791）。

**步骤**：

```bash
# 仿真桥（8790）正常下单，期望 ok:true（到达确认通过）
curl -d '{"code":"159929.SZ","op":"sell","volume":100,"remark":"testconfirm0001"}' http://127.0.0.1:8790/order
```

**通过判据**：返回 JSON 含 `"ok": true`（且该单在 iQuant Order.csv 能找到任务编号）。

**失败判据**：返回 `"ok": false` + `"no order record (dropped)"`——说明 0.3s 回查太早（iQuant ORDER 落表慢于 0.3s），需调大 sleep 或重试次数（`iquant_bridge.py:252-261`）。

**注意**：这是**模拟/实盘交易模式**测试单，会真实报单。用小额（100 股）+ 非持仓影响方向，或收盘后用模拟盘。remark 用 `testconfirmXXXX` 前缀便于事后清理识别。

---

### 验证 2 — 收盘后不下单守卫 ⏳

**修复**：`_handle_bar` 信号推送**之后**、落单前加守卫（`live_engine.py:799`）。`order.bar_time >= 15:00`（深交所收盘）则跳过落单，信号事件**仍发**。用 `order.bar_time`（非墙钟，见 memory `live-engine-time-guard-use-bar-time`）。

**触发条件**：15:00 后产生信号（策略在收盘后仍触发 OPEN/REDUCE 等）。部分可控——若策略周期对 15:00 后的 bar 仍求值就会触发。

**观测点**：

| # | 位置 | 应看到 | 命令/查法 |
|---|---|---|---|
| 1 | Core 日志 | `skip after-close order <type> <code> <signal> bar=<bar_time> (bar_time >= 15:00)` | grep `skip after-close` 后端日志 |
| 2 | SSE 事件 | B5 信号事件**有**（signal 事件含该信号意图） | 前端实盘工作台事件日志 |
| 3 | DB `live_orders` | 该信号**无** LiveOrder 落库（15:00 后无新单） | `SELECT COUNT(*) FROM live_orders WHERE date(bar_time)=date('YYYY-MM-DD') AND bar_time >= '15:00'` 应 = 0 |
| 4 | iQuant `Order.csv` | 无 15:00 后的委托记录 | Read `Order.csv`，看委托时间列 |

**通过判据**：#1 日志有 skip + #2 信号事件有 + #3 DB 无 15:00 后单 + #4 iQuant 无 15:00 后委托。四项全中 = 守卫生效（信号透传、落单被拦）。

**失败判据**：DB/iQuant 出现 15:00 后的委托（守卫没拦住）。

**对照历史**：2026-08-14 id40-41 是 15:02:42 下单成待报（守卫修复前）。修复后应不再出现此类。

---

### 验证 3 — 超时检查移主循环 ⏳

**修复**：抽 `_expire_stale_orders`（`live_engine.py:1518`），`_tick_main` 每轮（60s 节拍）调（`:523`，核心，解决 deals 循环被 60s 主循环饿死），`_poll_deals` 保留调用（`:1493`，兜底幂等）。order_ref=NULL 且 age≥180s 的 submitted/partial 单 → rejected。

**触发条件**：出现"桥未丢但 order_ref 迟迟匹配不上"的边缘单（桥受理、ORDER 有记录、但 Core `/orders` 轮询匹配不上）。比 #1 难触发但更可控——若想主动验可造场景。

**观测点**：

| # | 位置 | 应看到 | 命令/查法 |
|---|---|---|---|
| 1 | DB `live_orders` | 该单 `status=rejected`、`order_ref=NULL`、`error_message` 含 `order match timeout: no bridge order_ref within XXXs` | `SELECT id,stock_code,status,order_ref,error_message FROM live_orders WHERE error_message LIKE '%order match timeout%'` |
| 2 | DB 时间差 | `updated_at - created_at` 在 **180s~240s** 之间（180s 阈值 + 主循环 60s 节拍，最坏 240s） | `SELECT id,strftime('%s',updated_at)-strftime('%s',created_at) sec FROM live_orders WHERE error_message LIKE '%order match timeout%'` |
| 3 | SSE 事件 | order 事件 `status=rejected` + error 含 timeout 文案 | 前端实盘工作台事件日志 |

**通过判据**：#1 走的是 timeout 路径（非 dropped，区别于验证 1）+ #2 时间差在 180~240s（**不再是 440s**）。两项全中 = 主循环超时生效。

**失败判据**：时间差 ≥400s（说明仍被 deals 循环饿死，`_tick_main` 调用没生效）。

**主动验证法（可选）**：交易日中，若某单 order_ref 迟迟未匹配上（DB status=submitted 且 order_ref=NULL 持续），观察其何时被标 rejected——记下 created_at 到 rejected 的秒数，应 ≤240s。

**与验证 1 的区别**：
- 验证 1（受理即丢弃）：桥侧回查**即时**发现无委托 → error 含 `no order record (dropped)`，~1s。
- 验证 3（超时兜底）：桥未丢但 Core 匹配不上 order_ref → 180s 超时 → error 含 `order match timeout`，180~240s。
- 两者 error_message 文案不同，可据此区分走的哪条路径。

---

## P3 — 既有挂起项（非本次修复，长期待验）

### 验证 4 — F9 印花税字段 ⏳

**现状**：`live_trades.stamp_duty` 全 0。清单 F9 标注"DEAL 印花税字段待真机验证"。

**待验**：iQuant 桥 `/deals` 返回的 DEAL 对象上，印花税字段叫什么、有没有值。08-10 真机摸字段时可能没专门看这个。

**验证法**：

```bash
# 桥 /deals 返回原始 DEAL 对象字段（需在桥端加临时日志打印 DEAL 全字段，或看 query_deals 实现）
curl http://127.0.0.1:8790/deals
# 看返回里有无 stamp_duty / m_dStampDuty / 印花税 类字段及值
```

**通过判据**：找到印花税字段且有非 0 值 → 接入 `query_deals` → DB stamp_duty 不再全 0。

**注意**：买入 ETF 通常无印花税（印花税仅卖出股票收），ETF 双向可能都 0 是正常的。需用**股票卖出**成交验证才有非 0 值。当前池全是 ETF，可能本就无印花税——先确认字段存在，值是否非 0 取决于标的。

---

### 验证 5 — D3 对账自动校准放开 ⏳（长期）

**现状**：D3 对账"仅告警不修正"已实现，`_reconcile_mismatches` 记差异 + 告警日志，不自动改账。"自动校准留待真机跑顺再放开"（清单 D3）。

**前提**：live 稳定跑多个交易日、对账持续闭环干净后，再考虑放开自动修正。2026-08-17 只 2 笔单，样本太小。

**验证法**：连续 N 个交易日对账报告 Layer 4 全 ✅、无未知差异 → 可考虑放开。**当前不动**。

---

### 验证 6 — id40-41 陈旧 submitted（已知缺口，非验证项）⏳

**现状**：DB id40-41 仍 `submitted`（用户已手动撤单，iQuant 冻结归 0 证实）。潜在堵 159929/159936 卖出（在途门 F7，未触发）。详见 memory `id40-41-stale-submitted-blocks-sell`。

**根治**：给桥加撤单端点（当前桥无撤单，只有 ping/order/positions/account/orders/deals/quote），让 Core 能感知 iQuant 客户端外部撤单。

**短期**：手动 `UPDATE live_orders SET status='rejected' WHERE id IN (40,41)`（备份在 `main/data/dev.db.bak-20260817-reconcile`）。用户决定先不改。

**此项无需真机验证**，是待处理的已知缺口，列此备查。

---

### 验证 7 — T+0 ETF 当日可卖规则 ⏳

**背景**：2026-08-17 在 etf 股票池加入部分 T+0 ETF（跨境/债券/货币等当日买当日可卖）。原池全是 T+1 ETF，T+0 标的的交收行为**尚未真机验过**——F5 真机验证时只确认了 T+1 ETF 的 `m_nCanUseVolume=0`（买入当日不可用）语义，T+0 标的该字段取值是缺口。

**为何应该自动对（无需改代码）**：实盘 T+1 检查用 `LiveT1Checker`（`execution_engine.py:79`），**不自己判断哪只 T+0/T+1**，直接读桥 `/positions` 返回的 `m_nCanUseVolume`（可用数量）作 SELL 上限：

```python
avail = self._available_map.get(position.stock_code)  # 桥 m_nCanUseVolume
return min(position.quantity, avail)
```

iQuant 客户端按标的自身交收规则设 `m_nCanUseVolume`：T+1 ETF 买入当日 = 0；T+0 ETF 买入当日 = 买入量。每 bar 刷一次（`live_engine.py:765` `_refresh_available_map` → `live_engine.py:1033`），实时取数。→ Core 对 T+0 当日可卖、T+1 当日不可卖**走同一条路自动覆盖**，券商端兜底。

**回测不覆盖 T+0**（已知，不影响实盘）：回测用 `SimulatedT1Checker`（`execution_engine.py:73`）走 `position.available_shares_on`（`position.py:94`），硬编码"query_date 之前（不含当日）买入的份额"——所有标的一律 T+1。回测含 T+0 ETF 的策略时当日回转会被错误压掉，回测失真。**本验证只管实盘**，回测 T+0 另行处理（若需回测 T+0 ETF 策略再单列）。

**待验**：iQuant 对 T+0 ETF 买入当日，`/positions` 的 `m_nCanUseVolume` 是否返回买入量（非 0）——这是整条链路的取数源头。

**验证法**：

```bash
# 当日买入 T+0 ETF 后，拉桥 /positions 看可用数量字段
curl http://127.0.0.1:8790/positions
# 找刚买入的 T+0 ETF，看 available(m_nCanUseVolume) 是否 = 买入量（非 0）
```

或对账时读 iQuant `Position.csv` 的 `可用数量` 列。

**观测点**：

| # | 位置 | 应看到（T+0 正确） | 查法 |
|---|---|---|---|
| 1 | 桥 `/positions` | 当日买入的 T+0 ETF `available` = 买入量（非 0） | `curl .../positions` 找该 code |
| 2 | iQuant `Position.csv` | 该 T+0 ETF `可用数量` = 买入量；对照同日 T+1 ETF `可用数量` = 0 | Read `Position.csv` |
| 3 | DB `live_orders` | 当日对该 T+0 ETF 出 SELL 信号 → 落单 quantity 为全额可卖量（不被压到 0） | `SELECT id,stock_code,trade_type,quantity,status FROM live_orders WHERE stock_code='<T0代码>' AND date(bar_time)=date('YYYY-MM-DD')` |
| 4 | DB `live_trades` | 当日买入后当日卖出该 ETF → 有 sell 成交、持仓归 0 | `SELECT stock_code,trade_type,quantity FROM live_trades WHERE stock_code='<T0代码>'` |

**通过判据**：#1 桥 `available` 非 0 + #3 当日 SELL 不被压 + #4 当日回转成交记录。三项（或 #1+#3，当日没卖出信号时）满足 = T+0 规则在实盘正确生效。

**失败判据**：
- #1 桥 `available` = 0 → iQuant 把该 T+0 ETF 当 T+1 了（标的分类/客户端配置问题），Core 跟着压掉当日卖——这是 bug 信号，需查 iQuant 端该标的交收规则设置。
- #3 SELL 被 `cap_quantity` 压到 0 但 #1 非 0 → `_refresh_available_map` 聚合没拿到该 code（查 `instrument.exchange` 拼接是否与 `stock_code` 一致）。

**与验证 1b 同类（可主动验）**：交易日盘中，买入 100 股某 T+0 ETF 后立即 `curl /positions` 看 `available`，确认非 0 即可（不必等卖出信号）。用小额 + 收盘后模拟盘更稳。

---

## 验证总览表

| # | 项 | 优先级 | 触发条件 | 可主动验 | 状态 |
|---|---|---|---|---|---|
| 1 | 受理即丢弃即时检测 | P1 | iQuant 侧丢单（不可控） | 否（靠自然触发） | ⏳ |
| 1b | 桥到达确认正常不误伤 | P1 | — | **是**（curl 冒烟） | ⏳ |
| 2 | 收盘后不下单守卫 | P1 | 15:00 后信号 | 半（看策略） | ⏳ |
| 3 | 超时检查移主循环 | P1 | order_ref 匹配不上的边缘单 | 半（可观察） | ⏳ |
| 4 | F9 印花税字段 | P3 | 有卖出成交时看 /deals | 是 | ⏳ |
| 5 | D3 对账自动校准放开 | P3 | 长期跑顺后 | 否 | ⏳（暂不动） |
| 6 | id40-41 陈旧 submitted | — | — | — | 已知缺口 |
| 7 | T+0 ETF 当日可卖规则 | P3 | 当日买入 T+0 ETF | 是（curl /positions） | ⏳ |

## 对账覆盖边界（7 项哪些靠每日对账、哪些不能）

并非 7 项全靠每日对账。分四类：

| 类别 | 验证项 | 说明 |
|---|---|---|
| **完全靠对账** | 1、1b、3、7（5 算） | 1/1b/3 查 DB rejected `error_message` + 时间差、7 查 Position.csv `可用数量`，全在对账报告范围内 |
| **核心能、漏侧面** | 2 | "DB/iQuant 无 15:00 后单"对账能确认（守卫拦住没的核心）；但"信号事件仍发"对账看不到——这是 **Core 侧**的 B5 SSE 信号推送（`live_engine.py:784` `_emit("signal")`，在下单前发），**SSE 不持久化**（`live_engine.py:262` `_emit` 只投订阅队列、积压丢弃，不写日志/不落库），盘中前端没连着看则事后无法回溯。与 iQuant 无关——iQuant 信号是桥下单时记的，守卫拦住没下单则 iQuant `Strategy_Signal.csv` 本就无该条。**但验证 2 不依赖 SSE 回溯**：守卫拦截证据 `skip after-close order` 日志**落盘到 `logs/core.log`**（`logging_config.py:8`，RotatingFileHandler），盘后 `grep "skip after-close" logs/core.log` 可查；该日志隐含信号已到该分支（否则不进守卫判断），足以佐证"信号透传、落单被拦" |
| **对账不覆盖** | 4 | 印花税字段清单数据陷阱明列"不对账此字段"（`stamp_duty` 全 0）。必须手动 `curl http://127.0.0.1:8790/deals` 摸 DEAL 原始字段名——这是验证 4 本身要干的事，加日志/对账都替代不了 |
| **非验证项** | 6 | 文档明确"无需真机验证，是待处理的已知缺口"。对账能**监测**它没恶化（id40-41 仍 submitted、未实际堵卖出），但它是已知 bug 不是待验证行为 |

**准确结论**：真正需要脱离对账、手动做的只有**验证 4**（摸印花税字段名）。验证 2 的"信号仍发"要顺便看前端 SSE 事件日志。其余 1/1b/3/7 对账都能闭环。

## 建议验证顺序

1. **验证 1b（可主动）**：下次开盘前/中，两桥粘进 iQuant 实盘模式后 curl 冒烟，确认正常路径不误伤。这是唯一不靠自然触发的 P1 验证。
2. **验证 3（易观察）**：交易日中留意 DB 是否有 order_ref=NULL 的 submitted 单，观察其 rejected 时效（应 ≤240s 非 440s）。
3. **验证 1 + 2（靠自然）**：每日对账时对照——出现 15:00 后信号看 #2、出现 DB rejected 看是 dropped 还是 timeout 文案判 #1/#3。
4. **验证 4（附带）**：有卖出成交时顺带看 /deals 印花税字段。
5. **验证 7（可主动）**：盘中买入 T+0 ETF 后 `curl /positions` 看 `available` 非 0——确认 iQuant 对 T+0 标的当日可用量语义，整条当日可卖链路的取数源头。
6. **验证 5/6**：长期项，暂不动。

## 每日对账时快速对照

跑完 `iquant-reconcile` skill 后，看当日 `reconcile-report.md`，对照本清单：

- 出现 `no order record (dropped)` 的 rejected 单 → **验证 1 触发**，查时间差是否 ≤1s。
- 出现 15:00 后信号但无委托 → **验证 2 触发**，确认信号事件有、DB 无单。
- 出现 `order match timeout` 的 rejected 单 → **验证 3 触发**，查时间差是否 ≤240s。
- 当日买入 T+0 ETF → **验证 7 对照**，看 Position.csv 该 code `可用数量` 是否非 0（T+0 正确）；同日 T+1 ETF `可用数量` 应 = 0 印证对照。
- 当日平稳无异常 → 三条均未触发（记 —），等下一日。
