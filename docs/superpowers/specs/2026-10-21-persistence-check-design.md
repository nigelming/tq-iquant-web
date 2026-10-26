# 持续性检验（阿尔法衰减 vs 风格钟摆）— 设计文档

日期：2026-10-21
状态：已批准（待实施）

## 一、背景与动机

回测详情页的「各年年收益柱状图」容易让用户把「逐年收益递减」直接判定为策略能力衰减
（因子失效/容量拥挤）。但逐年收益下滑的首要变量往往是**市场环境、风格贝塔、市场温度**，
不是策略本身退化。需要把「贝塔回落（风格钟摆/市场温度周期）」与「阿尔法衰减（超额持续
收敛 + 风险同步恶化 + 交易结构退化）」区分开，作为可检验的判定。

成长精选仅为示例——本模块**适配所有回测**，通过多指数对照判断策略收益下坠是随风格还是独立。

### 判定框架（来自需求）
- 只有【超额持续收敛 + 风险指标（回撤/夏普/盈亏比）同步恶化 + 交易结构退化】才是真正的阿尔法衰减；
- 若只是总收益随风格指数一起回落、超额稳定、风险稳定 → 正常的风格钟摆（市场温度周期）。

## 二、目标与非目标

### 目标
1. 分年度超额收益（对比组合自身 benchmark + 6 只宽基指数）
2. 分年度最大回撤 / 夏普 / 卡玛
3. 分年度胜率 / 盈亏比 / 换手 / 成本占比
4. 滚动 6 月 / 12 月年化收益曲线（策略）+ 组合自身基准 12 月滚动年化（对照）
5. 规则化自动判定（alpha_decay / style_pendulum / stable / insufficient）+「供参考非自动判定」标注

### 非目标
- 不改变回测引擎/交易逻辑（纯分析模块）
- 不做自动采纳/拦截（仅展示与提示）
- 不引入新的指数取数来源（复用现有 TQ 指数拉取）

## 三、架构与数据流

### 3.1 新表 `backtest_persistence_analyses`
与危机Alpha/稳健性同款：
- `record_id`：唯一 FK → `backtest_records.id`（一个回测一条）
- `status`：`completed | failed | insufficient`
- `error_message`：失败原因（文本）
- `computed_at`：UTC 时间戳
- `payload_json`：计算结果（见第四节 schema）
- `skipped_benchmarks_json`：无数据被跳过的指数名列表

需新增 Alembic 迁移。

### 3.2 引擎文件 `core/engine/persistence_alpha.py`
纯计算模块（可单测，不依赖 DB 之外的 I/O）：
- `compute_persistence(nav_series, trades, benchmark_closes, benchmark_code, benchmark_name) -> dict`
  - `nav_series`：`{date: Decimal}` 组合日净值（来自 `backtest_daily_snapshots.total_value`）
  - `trades`：`[{date, trade_type, amount, commission, stamp_duty, pnl}]`（来自 `backtest_trades`）
  - `benchmark_closes`：`{code: {date: Decimal}}` 各指数日收盘（含自身 benchmark）
  - 返回完整 payload（见第四节）

### 3.3 服务层 `core/services/backtest_service.py`
新增 `recompute_persistence(db, record_id)`：
- 校验回测存在 + 有组合日快照（无 → `failed` + 可读错误）
- 读 `params_snapshot`/组合取自身 `benchmark_index`
- `build_index_close_series([benchmark] + 6 宽基 codes, start, end)` 批量拉指数日线
- 汇总 `skipped_benchmarks`（无数据的指数）
- 调 `persistence_alpha.compute_persistence` 计算 → 落库 `_upsert_persistence`
- 7 天 TTL：`computed_at` 距今 < 7 天直接返回缓存（沿用 crisis-alpha `RECOMPUTE_TTL` 模式）

### 3.4 路由 `core/api/backtest.py`
- `POST /api/backtest/records/{record_id}/persistence/recompute` → `recompute_persistence`
  - 与 `/records/{id}/crisis-alpha/recompute` 同款：404 记录不存在；返回 `ok(result)`（含 status/payload/skipped）
- 详情接口 `get_record_detail` 返回 `persistence` 字段（读表，未计算为 null）

### 3.5 基准集合（复用）
`core/engine/crisis_alpha.py:CRISIS_BENCHMARKS`（6 只宽基）：
中证全指 `000985.CSI`、沪深300 `000300.SH`、中证500 `000905.SH`、中证1000 `000852.SH`、
创业板指 `399006.SZ`、科创50 `000688.SH`。
超额对照集合 = `[自身 benchmark] + 上述 6 只`，去重。

## 四、Payload schema（`payload_json`）

```jsonc
{
  "years": [
    {
      "year": 2024,
      "is_partial": false,              // 当年快照交易日 < 60 → true（不参与趋势判定）
      "nav_return": 0.9557,             // 年内净值收益
      "max_drawdown": 0.12,             // 年内最大回撤（正数）
      "sharpe": 1.8,                    // 年内日收益年化夏普（<20 交易日 → null）
      "calmar": 5.2,                    // 年内收益 / 年内最大回撤（回撤 0 → null）
      "win_rate": 0.55,                 // 复用 Evaluator 定义
      "profit_factor": 1.6,
      "turnover": 3.2,                  // 年成交额 / 年均净值
      "cost_ratio": 0.0012,             // 年总费用 / 年成交额
      "excess": { "000300.SH": 0.30, "000905.SH": 0.25, ... },  // nav_return − 该指数当年收益（指数当年无数据 → 缺该键）
      "bench_returns": { "000300.SH": 0.65, ... }               // 各指数当年收益
    }
  ],
  "rolling": {
    "dates": ["2024-06-01", ...],
    "strat_6m": [0.12, ...],            // 6 月滚动年化（<183 日历史 → null）
    "strat_12m": [0.08, ...],           // 12 月滚动年化
    "bench_12m": [0.06, ...]            // 自身基准 12 月滚动年化
  },
  "benchmarks": [
    { "code": "000300.SH", "name": "沪深300", "is_main": true, "available": true }
  ],
  "verdict": {
    "level": "alpha_decay" | "style_pendulum" | "stable" | "insufficient",
    "reasons": ["近 2 年超额逐年收敛", "夏普/卡玛逐年下行且回撤走高", "胜率走低且换手上升", ...],
    "summary": "一句话结论",
    "caveat": "结论供参考，非自动判定：仅当超额持续收敛 + 风险指标同步恶化 + 交易结构退化同时成立才判为疑似衰减；若总收益随风格指数回落而超额稳定，则为风格钟摆（市场温度周期）。"
  }
}
```

## 五、计算口径

### 5.1 分年度指标
- **满年**：该自然年快照交易日 ≥ 60 才算满年；否则 `is_partial=true` 且不参与趋势判定。
- **nav_return**：`nav_年末 / nav_年初 − 1`（年初=年内首个快照日）。
- **max_drawdown**：年内 NAV 峰值回撤峰值（正数）。
- **sharpe**：年内日收益 `mean/std * sqrt(252)`；样本 < 20 → null。
- **calmar**：`nav_return / max_drawdown`；max_drawdown ≤ 0 → null。
- **win_rate / profit_factor / turnover / cost_ratio**：**复用 Evaluator 的公式定义**（按年内成交过滤），实现时从 `core/engine/evaluator.py` 对齐。
- **excess[code]**：`nav_return − 指数当年收益`；指数当年无数据 → 该键缺省。

### 5.2 滚动年化
- 对每个快照日 t，取 `t − 窗口` 前最近的一个快照日 s（窗口 6M≈183 天 / 12M≈365 天）；
- `annualized = (nav_t / nav_s) ^ (365 / 实际间隔天数) − 1`；无足够历史（s 不存在）→ null。
- `bench_12m` 同法作用于基准日收盘序列。

### 5.3 判定规则（`persistence_alpha._decide`）
输入为按时间排序的**满年**序列（`n = len(full_years)`）：
- `excess_conv`（超额收敛）：`n ≥ 2 且 excess 逐年递减（最后满年 < 前一年）`。
- `risk_bad`（风险恶化）：`n ≥ 2 且（sharpe 与 calmar 最后一年均 < 前一年）且 max_drawdown 最后一年 > 前一年`。
- `structure_bad`（结构退化）：`n ≥ 2 且（win_rate 或 profit_factor 最后一年 < 前一年）且（turnover 或 cost_ratio 最后一年 > 前一年）`。
- `total_down`（总收益回落）：`最后满年 nav_return < 前一年`。

判定：
- `n < 2` → `insufficient`（只展示数据，不出结论）。
- `excess_conv and risk_bad and structure_bad` → `alpha_decay`（疑似阿尔法衰减）。
- `total_down and not excess_conv and not risk_bad` → `style_pendulum`（风格钟摆）。
- 其余 → `stable`（超额与风险平稳 / 混合）。

`reasons[]` 逐条列出各条件的达标/未达标情况；阈值（60 交易日、252、183/365 天）在常量区集中定义、便于调整。

## 六、前端（回测详情报告）

`web/src/views/Backtest.vue` 详情报告新增折叠区块，放在「稳健性体检（九）」之后：

- 区块标题：`十、持续性检验（阿尔法衰减 vs 风格钟摆）`（复用 `.section-title ca-header` 折叠样式）
- 「重算」按钮 → `recomputePersistence(id)`（复用 `.ca-recompute` 交互），状态 running→completed
- **判定横幅**（复用 `.ca-banner`）：level 徽章（alpha_decay=红 / style_pendulum=金 / stable=绿 / insufficient=灰）+ `reasons` 逐条 + `caveat`
- **表一 · 分年度指标矩阵**：行=年份，列=`收益 / 最大回撤 / 夏普 / 卡玛 / 胜率 / 盈亏比 / 换手 / 成本占比`（`is_partial` 年份加 `*` 标注）
- **表二 · 分年度超额矩阵**：行=年份，列=各基准（自身 + 6 宽基），值为超额%；无数据基准在表头标注「无数据」
- **ECharts 滚动年化曲线**：`dates` x 轴，三条线 `策略 6M / 策略 12M / 基准 12M`（复用 `cssVar` 主题色；坐标 `%`）

前端 `api/index.ts` 新增：`recomputePersistence(id)`、`PersistenceAnalysis` 类型、`get_record_detail` 返回类型加 `persistence` 字段。

## 七、测试

### 后端单测 `core/tests/unit/test_persistence_alpha.py`
- 分年度指标：nav_return / max_drawdown / sharpe / calmar / win_rate / profit_factor / turnover / cost_ratio（含 Evaluator 公式对齐）
- 超额：`nav_return − 指数收益`；指数缺某年 → 该键缺省
- 滚动年化：6M/12M 正确年化；历史不足 → null
- 部分年份：`is_partial=true` 且不参与判定
- 样本不足：`n<2` → insufficient
- 判定三条路径：alpha_decay（三条件齐）、style_pendulum（回落但超额/风险稳定）、stable
- 指数无数据：`skipped_benchmarks` 汇总

### 后端集成 `core/tests/integration/test_backtest_api.py`
- `POST /records/{id}/persistence/recompute` → 200 + 落库 + 404（记录不存在）
- TTL 缓存：7 天内不重算

### 前端
- `Backtest.test.ts`：模块渲染、判定横幅 level、两张表列、重算按钮交互

## 八、错误处理

- 回测记录不存在 → 404
- 无组合日快照 / 组合已删除 → `failed` + 可读错误（沿用 crisis-alpha 文案风格）
- 自身基准指数无数据 → `failed`（主基准是判定核心，必须有）；6 宽基缺某只 → 仅跳过该只（`skipped_benchmarks`）
- 计算异常 → `failed` + 日志，不阻塞详情页（`persistence` 返回 null/错误）

## 九、验收标准

1. 任一已完成的回测，详情页「持续性检验」可重算并展示：判定横幅 + 两张表 + 滚动曲线
2. 分年度超额对「自身基准 + 全部可用宽基」逐只给出
3. 判定三条件可被单测触发/拦截，`caveat` 恒显示
4. 后端 1458+ 测试全绿、前端 288+ 测试全绿、`npm run build` 通过
5. 重打包后可离线使用（TQ 拉指数数据需通达信运行）
