"""「通达信 vs 内置引擎」逐 bar 对比验证套件（需通达信在线）。

目的：对内置引擎已实现的 47 个函数做实算校验——同一份真实行情数据，
一路经内存注入喂给通达信（预导入的 TQVERIFY1/2 公式），一路喂给内置
引擎 evaluate_outputs，逐输出线逐 bar 比对。

一次性前置：
1. 通达信（回测版）启动并登录行情。
2. 公式管理器 → 技术指标 → 新建各条公式（整段复制对应 txt）：
   - 名称 TQVERIFY1..TQVERIFY9B ← core/tests/tdx_compare/TQVERIFY1..9B.txt
   - 其中 TQVERIFY9B 依赖被引用公式 TESTA（内容一行：A1:CLOSE;）需先建好

运行：uv run pytest core/tests/integration/test_tdx_formula_compare.py -v -s

判定口径：
- 两侧同为 null（预热段无效值）→ 一致；
- 一侧 null 一侧 0 → WARN（TDX 无效值表示口径差异，单独报告不计 FAIL）；
- 数值比较：|a-b| ≤ 1e-6 · max(1,|a|,|b|)；
- 其余不一致 → FAIL，测试失败并打印逐线报告；
- 报告同时落盘 core/tests/tdx_compare/last_report.json。

文件底部 TestCompareLine 为比较器的离线单元测试（不依赖通达信）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import polars as pl
import pytest

from core.engine.tdx_formula import evaluate_outputs, syntax_check
from core.tq.data import TQData
from core.tq.formula import TQFormula
from core.tq.utils import TDXConnectionError, get_tq

ASSET_DIR = Path(__file__).resolve().parents[1] / "tdx_compare"
TEST_CODE = "000001.SZ"
BAR_COUNT = 120
REL_TOL = 1e-6
FORMULA_NAMES = ("TQVERIFY1", "TQVERIFY2", "TQVERIFY3", "TQVERIFY4", "TQVERIFY5",
                 "TQVERIFY6", "TQVERIFY7", "TQVERIFY8", "TQVERIFY8B", "TQVERIFY9",
                 "TQVERIFY9B")
# 批次 9b：TQVERIFY9B 引用的被测公式 TESTA（用户通达信内已建，内容一行 A1:CLOSE;）。
# harness 不走 db，直接以同内容求值构造 resolver（与 backtest_service._make_xref_resolver
# 同构：period_key=None → base 周期；非 None → 上级周期，memo 化）。
TESTA_CONTENT = "A1:CLOSE;"
_FIELDS = ("Open", "High", "Low", "Close", "Volume", "Amount")


# ---------------------------------------------------------------- 比较器（离线可测）

# 数值容差：TDX 输出仅 2 位小数（内部全精度、输出舍入），abs 0.0051 覆盖舍入
# 边界；长累计浮点路径差用 rel 2e-5 覆盖。0/1 信号线（两侧非空值全在 {0,1}）
# 不适用容差，必须精确一致。
_ABS_TOL = 0.0051
_REL_TOL_NUM = 2e-5

# 已记录环境差异（TDX 内部 MA 浮点求和路径不可复现，8471 根中 2 根 ≈ 0.024%）：
# bar1221(19960315)/bar5989(20160725) MA5==MA10 数学平局，TDX 内部判定 ma5>ma10
# （float 尾差），CROSS 未在下一根触发；内置整数分定点 MA 数学相等→float 相等，
# prev a<=b 成立 → 触发。四种 MA 求和口径（定点/正序/反序/fsum）均无法全复现，
# 整数分定点最优（fail 19→2）。下游传播：
#   F15(CROSS)：bar1222/5990；G03(FILTER)：同 2 根；
#   F07(VALUEWHEN)：两段 [1222,1233]∪[5990,6002]（TDX 下次触发 bar1234/6003 后恢复）。
_KNOWN_DIVERGENCES = {
    "TQVERIFY1": {
        "F07": set(range(1222, 1234)) | set(range(5990, 6003)),
        "F15": {1222, 5990},
    },
    "TQVERIFY2": {"G03": {1222, 5990}},
    "TQVERIFY3": {
        # bar531 为除权除息日（54.4→27.0），TDX 内部对 REF 做除权调整，
        # 内置引擎用原始价计算 (C-REF)/C 超出 ASIN 定义域，单根豁免。
        "H04": {531},
    },
    "TQVERIFY7": {
        # L01 依赖 CROSS(MA5,MA10)，继承 TDX 内部 MA 浮点求和路径差异
        #（bar1221/5989 数学平局），与 F07/F15 同源
        "L01": set(range(1222, 1234)) | set(range(5990, 6003)),
    },
    "TQVERIFY9": {
        # 跨周期引用逐 bar 对比豁免（在线实证 2026-10-20，全部 ≤bar524，现代段零差异）：
        # ① bar116-288：1991-1992 早期 A 股周六/周日为交易日，TDX 周线按周日换根、
        #   周线行时间戳=周六或周日标，与自然周一分桶及 TQ 周线行分周不一致；
        # ② bar377-388、bar511-524：1993-05 除权周（bar531 除权日，批次3 H04 同源），
        #   TDX 内部跨周期对除权周 OHLC 做预调整，TQ 周线 df 为原始价；
        # ③ L05 V#WEEK 周内累计起点在上述窗口同步偏移。
        "L01": set(range(116, 123)) | set(range(217, 222)) | set(range(272, 277))
               | set(range(377, 381)) | set(range(511, 518)),
        "L02": set(range(116, 123)) | set(range(217, 222)) | set(range(272, 277))
               | set(range(380, 386)) | set(range(517, 521)),
        "L03": {122, 222, 380} | set(range(276, 282)) | set(range(511, 518)),
        "L04": {122, 222, 517} | set(range(276, 282)) | set(range(380, 386)),
        "L05": set(range(122, 127)) | {222} | set(range(276, 282))
               | set(range(380, 386)) | set(range(517, 521)),
        "L06": {116, 122, 123, 217, 222, 272, 276, 277, 377, 381, 517, 518},
        "L07": set(range(122, 127)) | set(range(222, 235)) | set(range(276, 282)),
        "L09": set(range(116, 126)) | set(range(217, 225)) | set(range(272, 280))
               | set(range(377, 384)) | set(range(511, 521)),
        "L11": set(range(116, 123)) | set(range(217, 222)) | set(range(272, 277))
               | set(range(384, 388)) | set(range(518, 525)),
        "L12": set(range(116, 127)) | set(range(217, 235)) | set(range(272, 282))
               | set(range(377, 388)) | set(range(511, 525)),
    },
    "TQVERIFY9B": {
        # 批次 9b 跨公式引用：TESTA 内容=A1:CLOSE; → R04≡L01(C#WEEK)、
        # R05≡L11(HHV(C#WEEK,8))，继承 9a 同源豁免（1991-93 早期周六/周日
        # 交易日历 + 1993-05 除权周 TDX 内部预调整）；若在线实测出现其他
        # bar，以 TQVERIFY_DEBUG_FAILS=1 输出为准修订
        "R04": set(range(116, 123)) | set(range(217, 222)) | set(range(272, 277))
               | set(range(377, 381)) | set(range(511, 518)),
        "R05": set(range(116, 123)) | set(range(217, 222)) | set(range(272, 277))
               | set(range(384, 388)) | set(range(518, 525)),
        # R06=CROSS(TESTA.A1,MA(TESTA.A1,5))：与前一根 C==MA5 数学平局同源
        # （探针实证 11 根 prevC-MA5 全为 0，当根 C>MA5 间距充分）——TDX 内部
        # MA 浮点求和尾差把平局判为"已上穿"，CROSS 不触发；内置整数分定点
        # 平局精确相等 → prev C<=MA5 成立 → 触发。批次 1 F15 已实证不可复现
        "R06": {372, 430, 651, 764, 805, 1204, 1733, 5033, 5514, 6119, 7402},
    },
}


def compare_line(tq_vals: list, bi_vals: list, rel_tol: float = REL_TOL,
                 known_bars: set | None = None) -> dict:
    """单输出线逐 bar 比对。tq_vals/bi_vals 等长，元素 float 或 None。

    返回统计 dict：match/null_both/warn/fail/known 计数 + max_rel_diff + 示例。
    判定：双 null → 一致；单 null 且另一侧 0 → WARN；单 null 另一侧非 0 → FAIL；
    信号线精确比较；数值线 |a-b| ≤ max(_ABS_TOL, _REL_TOL_NUM·max(|a|,|b|))；
    known_bars 内的不一致降级为 KNOWN（已记录环境差异，不计 FAIL）。
    """
    known_bars = known_bars or set()
    # 调试：TQVERIFY_DEBUG_FAILS=1 时 fail/warn/known 示例不截断（收集完整 bar 号用）
    cap = 10 ** 9 if os.environ.get("TQVERIFY_DEBUG_FAILS") == "1" else 3
    st = {
        "bars": len(bi_vals), "match": 0, "null_both": 0,
        "warn": 0, "fail": 0, "known": 0, "max_rel_diff": 0.0,
        "warn_examples": [], "fail_examples": [], "known_examples": [],
    }
    signal_line = all(
        v in (0.0, 1.0, 0, 1) for v in (*tq_vals, *bi_vals) if v is not None
    )
    for i, (t, b) in enumerate(zip(tq_vals, bi_vals)):
        if t is None and b is None:
            st["null_both"] += 1
            continue
        if t is None or b is None:
            other = b if t is None else t
            if other == 0:  # null ↔ 0：无效值口径差异，WARN 不计 FAIL
                st["warn"] += 1
                if len(st["warn_examples"]) < cap:
                    st["warn_examples"].append({"bar": i, "tq": t, "builtin": b})
                continue
            if i in known_bars:
                st["known"] += 1
                continue
            st["fail"] += 1
            if len(st["fail_examples"]) < cap:
                st["fail_examples"].append({"bar": i, "tq": t, "builtin": b})
            continue
        diff = abs(t - b)
        rel = diff / max(1.0, abs(t), abs(b))
        st["max_rel_diff"] = max(st["max_rel_diff"], rel)
        if signal_line:
            ok = t == b
        else:
            ok = diff <= max(_ABS_TOL, _REL_TOL_NUM * max(abs(t), abs(b)))
        if ok:
            st["match"] += 1
        elif i in known_bars:
            st["known"] += 1
            if len(st["known_examples"]) < cap:
                st["known_examples"].append({"bar": i, "tq": t, "builtin": b})
        else:
            st["fail"] += 1
            if len(st["fail_examples"]) < cap:
                st["fail_examples"].append({"bar": i, "tq": t, "builtin": b})
    return st


# ---------------------------------------------------------------- 对比套件（需通达信在线）

def _tq_values_by_date(stock_data: dict) -> dict:
    """TQ 原始输出 {var: [{Date, Value}]} → {VAR(大写): {"YYYYMMDD": value|None}}。"""
    out: dict = {}
    for var, entries in stock_data.items():
        if var in ("Date", "ErrorId", "Error", "Time") or not isinstance(entries, list):
            continue
        by_date: dict = {}
        for e in entries:
            if not isinstance(e, dict):
                continue
            d = str(e.get("Date") or "").strip().replace("-", "")
            if not d:
                continue
            v = e.get("Value")
            if isinstance(v, str):
                try:
                    v = float(v)
                except ValueError:
                    v = None
            by_date[d] = v
        out[var.upper()] = by_date
    return out


def _format_report(formula: str, lines: dict) -> str:
    rows = [f"[{formula}] 逐输出线对比（容差 rel≤{REL_TOL}）:"]
    for name, st in lines.items():
        rows.append(
            f"  {name}: match={st['match']} null={st['null_both']} "
            f"warn={st['warn']} known={st.get('known', 0)} fail={st['fail']} "
            f"max_rel={st['max_rel_diff']:.2e}"
        )
        for ex in st["fail_examples"]:
            rows.append(f"    FAIL bar{ex['bar']}: tq={ex['tq']} builtin={ex['builtin']}")
        for ex in st.get("known_examples", []):
            rows.append(f"    KNOWN bar{ex['bar']}: tq={ex['tq']} builtin={ex['builtin']}")
        for ex in st["warn_examples"]:
            rows.append(f"    WARN bar{ex['bar']}: tq={ex['tq']} builtin={ex['builtin']}")
    return "\n".join(rows)


@pytest.fixture(scope="module")
def compare_stats() -> dict:
    """取数一次 → 两条公式各算两侧 → 逐线比对统计（module 级只跑一轮）。

    口径（第二轮修正）：
    - 两侧都用通达信**全历史**日线（count=-1），内置引擎窗口预热段与 TDX 全历史
      预热段对齐；BARSCOUNT/SUMBARS 等历史锚定函数才可同口径比较。
    - 复权：两侧都用**原始价**（get_history_raw none / process dividend_type=0）。
      前复权下 TDX 公式引擎内部用未舍入价、get_market_data 返回 2 位舍入价，
      边界 bar 会漂移（实测 F07/F15/G03）；原始价是原生 2 位小数，两侧逐 bit 一致。
    - 量单位：TDX 公式 V 单位为「手」= get_market_data Volume(股)/100，内置侧
      预先 /100 对齐（实测因子恰为 100）。
    - 容差：TDX 输出仅 2 位小数（内部全精度计算、输出舍入），数值比较容差
      = max(0.0051, 2e-5 · 幅值)（0.0051 覆盖 2dp 舍入边界，2e-5 覆盖长累计
      浮点路径差）；0/1 信号线不适用容差——不一致即 FAIL。
    """
    try:
        get_tq()
    except TDXConnectionError as e:
        pytest.skip(f"通达信未连接，跳过对比验证：{e}")

    # 批次 9a：TQVERIFY9 跨周期引用（C#WEEK/C#MONTH）需要周/月线数据喂
    # period_frames；其余公式不引用上级周期，多拉的两个周期 df 不影响行为
    raw = TQData().get_history_raw(
        [TEST_CODE], ["1d", "1w", "1mon"], dividend_type="none", count=-1)
    ohlcv = raw["1d"]
    if not ohlcv or "Close" not in ohlcv or TEST_CODE not in ohlcv["Close"].columns:
        pytest.skip(f"行情数据不可用（{TEST_CODE} 1d）")
    n = len(ohlcv["Close"])
    if n < 60:
        pytest.skip(f"行情数据不足（{n} 根）")

    import pandas as pd

    def _pl_frame(per_ohlcv: dict) -> pl.DataFrame | None:
        """TQ 原始周期行情 → polars（datetime 列 + OHLCV，Volume 手→股口径 /100）。"""
        if not per_ohlcv or TEST_CODE not in per_ohlcv.get("Close", pl.DataFrame()).columns:
            return None
        return pl.DataFrame({
            "datetime": [pd.Timestamp(t).to_pydatetime()
                         for t in per_ohlcv["Close"].index],
            **{f: per_ohlcv[f][TEST_CODE].to_list()
               for f in _FIELDS if f != "Volume"},
            "Volume": [v / 100.0 for v in per_ohlcv["Volume"][TEST_CODE].to_list()],
        })

    dates = [pd.Timestamp(t).strftime("%Y%m%d") for t in ohlcv["Close"].index]
    bi_df = pl.DataFrame({
        # datetime：批次 5 时间分量函数（YEAR/MONTH/.../TIME）依赖此列
        "datetime": [pd.Timestamp(t).to_pydatetime() for t in ohlcv["Close"].index],
        **{f: ohlcv[f][TEST_CODE].to_list() for f in _FIELDS if f != "Volume"},
        "Volume": [v / 100.0 for v in ohlcv["Volume"][TEST_CODE].to_list()],
    })
    period_frames = {}
    for key, per in (("1w", "1w"), ("1mon", "1mon")):
        f = _pl_frame(raw.get(per))
        if f is not None and f.height > 0:
            period_frames[key] = f

    # 批次 9b：TESTA 跨公式引用 resolver（TQVERIFY9B 专用）
    xref_memo: dict = {}

    def _testa_resolver(form_name, period_key):
        assert form_name == "TESTA", f"harness 仅支持 TESTA，收到 {form_name}"
        key = (form_name, period_key)
        if key in xref_memo:
            return xref_memo[key]
        if period_key is None:
            outs = evaluate_outputs(TESTA_CONTENT, bi_df, period="1d")
        else:
            hf = period_frames.get(period_key)
            assert hf is not None, f"resolver 缺少 {period_key} 周期数据"
            outs = evaluate_outputs(TESTA_CONTENT, hf, period=period_key)
        xref_memo[key] = outs
        return outs

    stats: dict = {"code": TEST_CODE, "bars": n, "formulas": {}}
    for name in FORMULA_NAMES:
        text = (ASSET_DIR / f"{name}.txt").read_text(encoding="utf-8")
        chk = syntax_check(text)
        assert chk["ok"], f"{name} 内置引擎语法预检未通过: {chk['errors']}"

        raw_out = TQFormula().compute(
            name, "", [TEST_CODE], period="1d", count=-1, dividend_type=0)
        err = (raw_out or {}).get("ErrorId")
        if raw_out is None or (err is not None and str(err) not in ("0", "19")) \
                or TEST_CODE not in (raw_out or {}):
            pytest.skip(
                f"通达信侧未取到公式 {name} 的输出（ErrorId={err}）。"
                f"请先在通达信公式管理器新建技术指标 {name}，"
                f"内容整段复制 core/tests/tdx_compare/{name}.txt。"
            )
        tq_map = _tq_values_by_date(raw_out[TEST_CODE])
        # period="1d"：PERIOD 周期编号函数依赖周期上下文（TDX 侧在日线图计算）；
        # period_frames：批次 9a 跨周期引用（仅 TQVERIFY9 实际用到）；
        # xref_resolver：批次 9b 跨公式引用（仅 TQVERIFY9B 实际用到）
        bi_out = evaluate_outputs(
            text, bi_df, period="1d", period_frames=period_frames or None,
            xref_resolver=_testa_resolver if name == "TQVERIFY9B" else None)

        lines: dict = {}
        known = _KNOWN_DIVERGENCES.get(name, {})
        for var, series in bi_out.items():
            tq_by_date = tq_map.get(var.upper())
            bi_vals = series.to_list()
            if tq_by_date is None:
                lines[var] = {"missing_in_tdx": True, "fail": n,
                              "fail_examples": [], "warn_examples": [],
                              "known_examples": [], "match": 0, "null_both": 0,
                              "warn": 0, "known": 0, "max_rel_diff": 0.0, "bars": n}
                continue
            tq_vals = [tq_by_date.get(d) for d in dates]
            lines[var] = compare_line(
                tq_vals, bi_vals, known_bars=known.get(var.upper(), set()))
        stats["formulas"][name] = {"lines": lines}

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "code": TEST_CODE, "bars": stats["bars"], "rel_tol": REL_TOL,
        "formulas": stats["formulas"],
    }
    (ASSET_DIR / "last_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


def _assert_formula_ok(stats: dict, name: str) -> None:
    lines = stats["formulas"][name]["lines"]
    report_text = _format_report(name, lines)
    print("\n" + report_text)
    bad = {v: st for v, st in lines.items() if st["fail"] > 0}
    assert not bad, (
        f"{name} 存在 {len(bad)} 条不一致输出线：{sorted(bad)}\n{report_text}"
    )


def test_tqverify1_matches_builtin(compare_stats):
    """均线/引用/逻辑/极值族 25 条输出线逐 bar 对比。"""
    _assert_formula_ok(compare_stats, "TQVERIFY1")


def test_tqverify2_matches_builtin(compare_stats):
    """条件/累计/统计回归/数学族 25 条输出线逐 bar 对比。"""
    _assert_formula_ok(compare_stats, "TQVERIFY2")


def test_tqverify3_matches_builtin(compare_stats):
    """批次3扩展：数学逐点/位置/排名/逻辑/统计/均线 28 条输出线逐 bar 对比。"""
    _assert_formula_ok(compare_stats, "TQVERIFY3")


def test_tqverify4_matches_builtin(compare_stats):
    """批次4扩展：寻找极值/均线/逻辑引用/形态 15 条输出线逐 bar 对比。"""
    _assert_formula_ok(compare_stats, "TQVERIFY4")


def test_tqverify5_matches_builtin(compare_stats):
    """批次5扩展：时间分量/K线状态 12 条输出线逐 bar 对比。"""
    _assert_formula_ok(compare_stats, "TQVERIFY5")


def test_tqverify6_matches_builtin(compare_stats):
    """批次6扩展：别名/排名跨度/涨跌停/累乘/时间过滤 16 条输出线逐 bar 对比。"""
    _assert_formula_ok(compare_stats, "TQVERIFY6")


def test_tqverify7_matches_builtin(compare_stats):
    """批次7扩展：动态窗口（REF/SUM/LLV/HHV 窗口为序列）7 条输出线逐 bar 对比。"""
    _assert_formula_ok(compare_stats, "TQVERIFY7")


def test_tqverify8_matches_builtin(compare_stats):
    """批次8扩展：XMA/HHVLLV/MEDIAN/UPDOWN/MAX6/NEWSAR/TFILTER/TTFILTER/DRAWNULL
    20 条输出线逐 bar 对比。"""
    _assert_formula_ok(compare_stats, "TQVERIFY8")


def test_tqverify8b_matches_builtin(compare_stats):
    """批次8扩展：时间/周期符号 WEEKOFYEAR/PERIOD 2 条输出线逐 bar 对比。"""
    _assert_formula_ok(compare_stats, "TQVERIFY8B")


def test_tqverify9_matches_builtin(compare_stats):
    """批次9a 跨周期引用：C#WEEK/HIGH#WEEK/C#MONTH 等对齐语义 12 条输出线逐 bar
    对比（重点实证 E7 周线时间戳惯例与 E8 所属取值语义）。"""
    _assert_formula_ok(compare_stats, "TQVERIFY9")


def test_tqverify9b_matches_builtin(compare_stats):
    """批次9b 跨公式引用：TESTA.A1 / TESTA.A1#WEEK 组合语法 7 条输出线逐 bar 对比
    （TESTA 内容=A1:CLOSE;，各线分别等价于 C/REF(C,5)/MA(C,5)/C#WEEK/
    HHV(C#WEEK,8)/CROSS(C,MA(C,5)) 基线，同时验证 resolver 折回对齐）。"""
    _assert_formula_ok(compare_stats, "TQVERIFY9B")


# ---------------------------------------------------------------- 比较器离线单测

class TestCompareLine:
    def test_both_null_is_match(self):
        st = compare_line([None, 1.0], [None, 1.0])
        assert (st["null_both"], st["match"], st["fail"], st["warn"]) == (1, 1, 0, 0)

    def test_null_vs_zero_is_warn(self):
        st = compare_line([None, 0.0], [0.0, None])
        assert st["warn"] == 2 and st["fail"] == 0

    def test_null_vs_value_is_fail(self):
        st = compare_line([None], [1.5])
        assert st["fail"] == 1 and st["fail_examples"][0]["builtin"] == 1.5

    def test_value_within_rel_tol(self):
        st = compare_line([1e8], [1e8 + 50.0])  # 50 < 2e-5·1e8 = 2000
        assert st["match"] == 1

    def test_value_beyond_tol_fails(self):
        st = compare_line([100.0], [100.02])  # 0.02 > max(0.0051, 2e-5·100)
        assert st["fail"] == 1

    def test_signal_line_exact(self):
        # 0/1 信号线：不适用数值容差，0 vs 1 即 FAIL
        st = compare_line([1.0, 0.0, 1.0], [1.0, 1.0, 1.0])
        assert st["fail"] == 1 and st["match"] == 2

    def test_known_bars_exempted(self):
        # 已知环境差异 bar：fail 降级为 known，不影响断言
        st = compare_line([0.0, 0.0, 0.0], [0.0, 1.0, 1.0], known_bars={1})
        assert (st["fail"], st["known"], st["match"]) == (1, 1, 1)
