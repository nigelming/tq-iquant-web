"""tdx_formula 求值层单元测试。

用已知数值的小型 OHLCV DataFrame 验证表达式求值与 8 个核心函数的
时序语义（MA/EMA/REF/CROSS/HHV/LLV/IF/BARSLAST）。
"""

from datetime import datetime

import polars as pl
import pytest

from core.engine.tdx_formula.evaluator import FormulaEvalError, evaluate_outputs


@pytest.fixture()
def df() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "Open": [1.0, 1.0, 2.0, 3.0, 3.0],
            "High": [1.5, 2.5, 3.0, 4.0, 4.0],
            "Low": [0.5, 1.0, 1.5, 2.5, 2.5],
            "Close": [1.0, 2.0, 3.0, 4.0, 5.0],
            "Volume": [10.0, 20.0, 30.0, 40.0, 50.0],
            "Amount": [100.0, 200.0, 300.0, 400.0, 500.0],
        }
    )


def _vals(out: dict, name: str) -> list:
    return out[name].to_list()


# ---------------------------------------------------------------- 函数语义

class TestFunctions:
    def test_ma(self, df):
        out = evaluate_outputs("X:MA(C,2);", df)
        assert _vals(out, "X") == [None, 1.5, 2.5, 3.5, 4.5]

    def test_ma_window_full(self, df):
        out = evaluate_outputs("X:MA(C,5);", df)
        assert _vals(out, "X")[4] == 3.0

    def test_ema(self, df):
        # TDX EMA: alpha = 2/(N+1)，首值=首观测
        out = evaluate_outputs("X:EMA(C,3);", df)
        assert _vals(out, "X") == [1.0, 1.5, 2.25, 3.125, 4.0625]

    def test_ref(self, df):
        out = evaluate_outputs("X:REF(C,1);", df)
        assert _vals(out, "X") == [None, 1.0, 2.0, 3.0, 4.0]

    def test_cross(self, df):
        # C=[1..5] 上穿常数 2.5 发生在第 3 根 bar
        out = evaluate_outputs("X:CROSS(C,2.5);", df)
        assert _vals(out, "X") == [0.0, 0.0, 1.0, 0.0, 0.0]

    def test_hhv_llv(self, df):
        # TDX 部分窗口：窗口未满用已有数据（F21/F23 实证）
        out = evaluate_outputs("H3:HHV(C,3);L3:LLV(C,3);", df)
        assert _vals(out, "H3") == [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _vals(out, "L3") == [1.0, 1.0, 1.0, 2.0, 3.0]

    def test_hhv_n0_expanding(self, df):
        out = evaluate_outputs("X:HHV(C,0);", df)
        assert _vals(out, "X") == [1.0, 2.0, 3.0, 4.0, 5.0]

    def test_if(self, df):
        # C=[1..5], O=[1,1,2,3,3] → C>O 从第 2 根起为真
        out = evaluate_outputs("X:IF(C>O,1,0);", df)
        assert _vals(out, "X") == [0.0, 1.0, 1.0, 1.0, 1.0]

    def test_barslast(self, df):
        # C==3 在第 3 根 bar 成立；TDX 实证：首次成立之前 → null（非 0）
        out = evaluate_outputs("X:BARSLAST(C=3);", df)
        assert _vals(out, "X") == [None, None, 0.0, 1.0, 2.0]


# ---------------------------------------------------------------- 运算符

class TestOperators:
    def test_arithmetic(self, df):
        out = evaluate_outputs("X:(C-O)*2+1;", df)
        assert _vals(out, "X") == [1.0, 3.0, 3.0, 3.0, 5.0]

    def test_comparison_and_logic(self, df):
        out = evaluate_outputs("A:C>2 AND C<5;B:C<1 OR C>4;D:NOT(C>2);", df)
        assert _vals(out, "A") == [0.0, 0.0, 1.0, 1.0, 0.0]
        assert _vals(out, "B") == [0.0, 0.0, 0.0, 0.0, 1.0]
        assert _vals(out, "D") == [1.0, 1.0, 0.0, 0.0, 0.0]

    def test_short_circuit_forms(self, df):
        out = evaluate_outputs("A:C>2 && C<5;B:!(C>2);", df)
        assert _vals(out, "A") == [0.0, 0.0, 1.0, 1.0, 0.0]
        assert _vals(out, "B") == [1.0, 1.0, 0.0, 0.0, 0.0]

    def test_field_aliases(self, df):
        out = evaluate_outputs("X:O+H+L+C+V+AMO;", df)
        assert _vals(out, "X")[0] == 1.0 + 1.5 + 0.5 + 1.0 + 10.0 + 100.0

    def test_null_propagates_in_arithmetic(self, df):
        out = evaluate_outputs("X:REF(C,1)+1;", df)
        assert _vals(out, "X") == [None, 2.0, 3.0, 4.0, 5.0]


# ---------------------------------------------------------------- 定义链

class TestDefinitionChain:
    def test_param_and_reference(self, df):
        out = evaluate_outputs("N:=5;MA5:MA(C,N);", df)
        assert list(out.keys()) == ["MA5"]
        assert _vals(out, "MA5")[4] == 3.0

    def test_var_chain_and_output_reuse(self, df):
        out = evaluate_outputs(
            "MA3:=MA(C,3);X:CROSS(MA3,MA3);Y:MA3*2;", df
        )
        assert set(out.keys()) == {"X", "Y"}
        assert _vals(out, "X") == [0.0] * 5  # 自己与自己不交叉
        assert _vals(out, "Y") == [None, None, 4.0, 6.0, 8.0]

    def test_negative_param(self, df):
        out = evaluate_outputs("LOWL:=-5;X:0-LOWL;", df)
        assert _vals(out, "X") == [5.0] * 5

    def test_chinese_identifiers(self, df):
        out = evaluate_outputs("条件:=C>O;共振:IF(条件,1,0);", df)
        assert _vals(out, "共振") == [0.0, 1.0, 1.0, 1.0, 1.0]

    def test_statements_separated_by_comments(self, df):
        out = evaluate_outputs("A:=C;{注释}X:A*1;", df)
        assert _vals(out, "X") == _vals(evaluate_outputs("X:C;", df), "X")


# ---------------------------------------------------------------- 错误路径

class TestErrors:
    def test_unknown_variable(self, df):
        with pytest.raises(FormulaEvalError, match="FOO"):
            evaluate_outputs("X:FOO;", df)

    def test_unknown_function(self, df):
        with pytest.raises(FormulaEvalError, match="FOO"):
            evaluate_outputs("X:FOO(C,5);", df)

    def test_cross_period_rejected(self, df):
        with pytest.raises(FormulaEvalError, match="跨周期"):
            evaluate_outputs("X:REF(HIGH#DAY,1);", df)

    def test_window_must_be_constant(self, df):
        with pytest.raises(FormulaEvalError):
            evaluate_outputs("X:MA(C,C);", df)

    def test_syntax_error(self, df):
        with pytest.raises(FormulaEvalError):
            evaluate_outputs("X:MA(C,5;", df)

    def test_future_function_not_implemented_yet(self, df):
        # 词典闸门在 precheck；求值层遇到未实现函数应报错而非静默
        with pytest.raises(FormulaEvalError, match="TDXW"):
            evaluate_outputs("X:TDXW(C,5);", df)

    def test_funcs_not_in_runtime(self, df):
        # DAYOFWEEK/TOTALBARCOUNT 不在 iQuant 运行时函数表（TQVERIFY6 导入
        # 实测报未知字符串），为保持回测/实盘词表一致已移除，求值层必须显式报错
        with pytest.raises(FormulaEvalError, match="DAYOFWEEK"):
            evaluate_outputs("X:DAYOFWEEK;", df)
        with pytest.raises(FormulaEvalError, match="TOTALBARCOUNT"):
            evaluate_outputs("X:TOTALBARCOUNT;", df)


# ---------------------------------------------------------------- 扩展函数

class TestAccumulate:
    def test_sum_window(self, df):
        # TDX 部分窗口（F04 实证）：窗口未满用已有数据
        out = evaluate_outputs("X:SUM(C,2);", df)
        assert _vals(out, "X") == [1.0, 3.0, 5.0, 7.0, 9.0]

    def test_sum_expanding(self, df):
        out = evaluate_outputs("X:SUM(C,0);", df)
        assert _vals(out, "X") == [1.0, 3.0, 6.0, 10.0, 15.0]

    def test_count(self, df):
        # TDX 部分窗口：bar0 即出值（C>2 全假也是 0）
        out = evaluate_outputs("X:COUNT(C>2,2);", df)
        assert _vals(out, "X") == [0.0, 0.0, 1.0, 2.0, 2.0]

    def test_every(self, df):
        # TDX 部分窗口：bar0 窗口 [F] → 0
        out = evaluate_outputs("X:EVERY(C>1,2);", df)
        assert _vals(out, "X") == [0.0, 0.0, 1.0, 1.0, 1.0]

    def test_last(self, df):
        # LAST(X,A,B)：窗口 [i-A .. i-B] 共 A-B+1 根；空窗（i<B）真空真=1
        # LAST(C>1,3,1)：bar0 空窗→1；bar1 [F]→0；bar2 [F,T]→0；bar3 [F,T,T]→0；bar4 [T,T,T]→1
        out = evaluate_outputs("X:LAST(C>1,3,1);", df)
        assert _vals(out, "X") == [1.0, 0.0, 0.0, 0.0, 1.0]

    def test_barslastcount(self, df):
        out = evaluate_outputs("X:BARSLASTCOUNT(C>1);", df)
        assert _vals(out, "X") == [0.0, 1.0, 2.0, 3.0, 4.0]

    def test_filter(self, df):
        # 成立当根出信号，随后 N 根屏蔽
        out = evaluate_outputs("X:FILTER(C>2,1);", df)
        assert _vals(out, "X") == [0.0, 0.0, 1.0, 0.0, 1.0]

    def test_sma(self, df):
        # TDX SMA: Y=(M*X+(N-M)*Y')/N，首值=首观测
        out = evaluate_outputs("X:SMA(C,3,1);", df)
        assert _vals(out, "X")[:3] == [1.0, pytest.approx(4 / 3), pytest.approx(17 / 9)]

    def test_dma_constant_alpha(self, df):
        # DMA(X,0.5) 等价于 EMA(X,3)（alpha 同为 0.5）
        a = evaluate_outputs("X:DMA(C,0.5);", df)
        b = evaluate_outputs("Y:EMA(C,3);", df)
        assert _vals(a, "X") == _vals(b, "Y")


class TestReference:
    def test_hhvbars(self):
        # TDX 部分窗口：bar0 [3]→0；bar1 [3,1]→1；bar2 [3,1,2]→2；bar3 [1,2,5]→0；bar4 [2,5,4]→1
        d2 = pl.DataFrame({
            "Open": [3.0, 1.0, 2.0, 5.0, 4.0], "High": [3.0, 1.0, 2.0, 5.0, 4.0],
            "Low": [3.0, 1.0, 2.0, 5.0, 4.0], "Close": [3.0, 1.0, 2.0, 5.0, 4.0],
            "Volume": [1.0] * 5, "Amount": [1.0] * 5,
        })
        out = evaluate_outputs("X:HHVBARS(C,3);", d2)
        assert _vals(out, "X") == [0.0, 1.0, 2.0, 0.0, 1.0]

    def test_llvbars(self):
        # TDX 部分窗口：bar0 [3]→0；bar1 [3,1]→0；bar2 [3,1,2]→1；bar3 [1,2,5]→2；bar4 [2,5,4]→2
        d2 = pl.DataFrame({
            "Open": [3.0, 1.0, 2.0, 5.0, 4.0], "High": [3.0, 1.0, 2.0, 5.0, 4.0],
            "Low": [3.0, 1.0, 2.0, 5.0, 4.0], "Close": [3.0, 1.0, 2.0, 5.0, 4.0],
            "Volume": [1.0] * 5, "Amount": [1.0] * 5,
        })
        out = evaluate_outputs("X:LLVBARS(C,3);", d2)
        assert _vals(out, "X") == [0.0, 0.0, 1.0, 2.0, 2.0]

    def test_valuewhen(self, df):
        out = evaluate_outputs("X:VALUEWHEN(C=3,C);", df)
        assert _vals(out, "X") == [None, None, 3.0, 3.0, 3.0]

    def test_const(self, df):
        out = evaluate_outputs("X:CONST(C);", df)
        assert _vals(out, "X") == [5.0] * 5


class TestMath:
    def test_abs_sequence(self, df):
        out = evaluate_outputs("X:ABS(0-C);", df)
        assert _vals(out, "X") == [1.0, 2.0, 3.0, 4.0, 5.0]

    def test_max_min_elementwise(self, df):
        out = evaluate_outputs("A:MAX(C,O);B:MIN(C,O);", df)
        assert _vals(out, "A") == [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _vals(out, "B") == [1.0, 1.0, 2.0, 3.0, 3.0]

    def test_sqrt(self, df):
        out = evaluate_outputs("X:SQRT(C*0+9);", df)
        assert _vals(out, "X") == [3.0] * 5

    def test_iff_alias(self, df):
        out = evaluate_outputs("X:IFF(C>O,1,0);", df)
        assert _vals(out, "X") == [0.0, 1.0, 1.0, 1.0, 1.0]


# ------------------------------------------------- 扩展批次 2：统计回归族

class TestRollingStats:
    def test_std(self, df):
        # 样本标准差（÷N-1）：[1,2,3] 均值 2，偏差平方和 2，/2 = 1
        out = evaluate_outputs("X:STD(C,3);", df)
        assert _vals(out, "X") == [None, None, 1.0, 1.0, 1.0]

    def test_std_n1_null(self, df):
        # N=1 分母为 0 → null（词典注记的保护点）
        out = evaluate_outputs("X:STD(C,1);", df)
        assert _vals(out, "X") == [None] * 5

    def test_var_varp(self, df):
        # VAR 样本方差（÷N-1）；VARP 总体方差（÷N）：[1,2,3] 偏差平方和 2
        # VARP 前 w 根 null（TDX 怪癖：TDX 输出 0.0，内置置 null 仅 WARN）
        out = evaluate_outputs("V1:VAR(C,3);V2:VARP(C,3);", df)
        assert _vals(out, "V1") == [None, None, 1.0, 1.0, 1.0]
        assert _vals(out, "V2") == [None, None, None, 2 / 3, 2 / 3]

    def test_avedev(self, df):
        # 平均绝对偏差：[1,2,3] → (1+0+1)/3 = 2/3
        out = evaluate_outputs("X:AVEDEV(C,3);", df)
        assert _vals(out, "X") == [None, None, 2 / 3, 2 / 3, 2 / 3]

    def test_devsq(self, df):
        # 偏差平方和：[1,2,3] → 1+0+1 = 2
        out = evaluate_outputs("X:DEVSQ(C,3);", df)
        assert _vals(out, "X") == [None, None, 2.0, 2.0, 2.0]

    def test_slope_forcast_arith(self, df):
        # 等差序列斜率恒 1（浮点舍入内 approx）；FORCAST = 窗口均值 + 斜率*(N-1)/2
        out = evaluate_outputs("S:SLOPE(C,3);F:FORCAST(C,3);", df)
        s, f = _vals(out, "S"), _vals(out, "F")
        assert s[:2] == [None, None]
        assert all(v == pytest.approx(1.0) for v in s[2:])
        assert f[:2] == [None, None]
        assert [round(v, 6) for v in f[2:]] == [3.0, 4.0, 5.0]

    def test_slope_forcast_general(self):
        df2 = pl.DataFrame({
            "Open": [1.0] * 5, "High": [5.0] * 5, "Low": [0.5] * 5,
            "Close": [1.0, 3.0, 2.0, 4.0, 5.0],
            "Volume": [10.0] * 5, "Amount": [100.0] * 5,
        })
        out = evaluate_outputs("S:SLOPE(C,3);F:FORCAST(C,3);", df2)
        s, f = _vals(out, "S"), _vals(out, "F")
        assert s[2] == pytest.approx(0.5)
        assert f[2] == pytest.approx(2.5)
        assert s[3] == pytest.approx(0.5)
        assert f[3] == pytest.approx(3.5)
        assert s[4] == pytest.approx(1.5)
        assert f[4] == pytest.approx(31 / 6)


# ------------------------------------------------- 扩展批次 2：逻辑引用族

class TestLogicRef:
    def test_exist_window(self, df):
        # TDX 部分窗口：bar0 窗口 [F]→0；bar2 起含 T→1
        out = evaluate_outputs("X:EXIST(C>2,3);", df)
        assert _vals(out, "X") == [0.0, 0.0, 1.0, 1.0, 1.0]

    def test_exist_expanding(self, df):
        out = evaluate_outputs("X:EXIST(C>2,0);", df)
        assert _vals(out, "X") == [0.0, 0.0, 1.0, 1.0, 1.0]

    def test_barssince(self, df):
        # 首次成立（idx2）距今：从未成立为 null
        out = evaluate_outputs("X:BARSSINCE(C>2);", df)
        assert _vals(out, "X") == [None, None, 0.0, 1.0, 2.0]

    def test_barssince_never(self, df):
        out = evaluate_outputs("X:BARSSINCE(C>9);", df)
        assert _vals(out, "X") == [None] * 5

    def test_barscount(self, df):
        # 首个有效值至今的周期数（TDX 实证：0 起，首个有效 bar 计 0）
        out = evaluate_outputs("B1:BARSCOUNT(C);B2:BARSCOUNT(MA(C,2));", df)
        assert _vals(out, "B1") == [0.0, 1.0, 2.0, 3.0, 4.0]
        assert _vals(out, "B2") == [None, 0.0, 1.0, 2.0, 3.0]

    def test_longcross(self, df):
        # C<3.5 连续 2 根（截至上根）后 C 上穿 3.5：C=[1,2,3,4,5]
        out = evaluate_outputs("X:LONGCROSS(C,3.5,2);", df)
        assert _vals(out, "X") == [0.0, 0.0, 0.0, 1.0, 0.0]

    def test_longcross_n1_is_cross(self, df):
        a = evaluate_outputs("X:LONGCROSS(C,3.5,1);", df)["X"].to_list()
        b = evaluate_outputs("Y:CROSS(C,3.5);", df)["Y"].to_list()
        assert a == b

    def test_nday(self, df):
        # 连续 N 根（含当根）X>Y：C>2.5 = [F,F,T,T,T]
        out = evaluate_outputs("X:NDAY(C,2.5,2);", df)
        assert _vals(out, "X") == [None, 0.0, 0.0, 1.0, 1.0]

    def test_updown_nday(self, df):
        # UPNDAY 连涨 N 根（含当根）；DOWNNDAY 连跌
        out = evaluate_outputs("U:UPNDAY(C,2);D:DOWNNDAY(C,2);", df)
        assert _vals(out, "U") == [None, 0.0, 1.0, 1.0, 1.0]
        assert _vals(out, "D") == [None, 0.0, 0.0, 0.0, 0.0]

    def test_sumbars(self, df):
        # TDX 实证：含当根 cnt，cnt==1 特判返 0
        # V=[10..50]：bar0:10<70→0；bar1:30<70→1；bar2:60<70→2；bar3:40+30=70 cnt2→2；bar4:90 cnt2→2
        out = evaluate_outputs("X:SUMBARS(V,70);", df)
        assert _vals(out, "X") == [0.0, 1.0, 2.0, 2.0, 2.0]

    def test_sumbars_never(self, df):
        # 全部历史不足：返回已累加根数 i
        out = evaluate_outputs("X:SUMBARS(V,1000);", df)
        assert _vals(out, "X") == [0.0, 1.0, 2.0, 3.0, 4.0]


# ------------------------------------------------- 扩展批次 2：数学/均线族

class TestMathExt:
    def test_log_ln_exp(self, df):
        out = evaluate_outputs("A:LOG(100);B:LN(EXP(1));E:EXP(0);", df)
        assert _vals(out, "A") == [2.0] * 5
        assert _vals(out, "B") == [1.0] * 5
        assert _vals(out, "E") == [1.0] * 5

    def test_pow(self, df):
        out = evaluate_outputs("X:POW(C,2);", df)
        assert _vals(out, "X") == [1.0, 4.0, 9.0, 16.0, 25.0]

    def test_pow_frac(self, df):
        out = evaluate_outputs("X:POW(4,0.5);", df)
        assert _vals(out, "X") == [2.0] * 5

    def test_sign(self, df):
        out = evaluate_outputs("X:SIGN(C-3);", df)
        assert _vals(out, "X") == [-1.0, -1.0, 0.0, 1.0, 1.0]

    def test_wma(self, df):
        # 线性加权（权重 1..3，最近最大）：[1,2,3] → (1+4+9)/6 = 14/6
        out = evaluate_outputs("X:WMA(C,3);", df)
        assert _vals(out, "X")[:2] == [None, None]
        assert _vals(out, "X")[2] == pytest.approx(14 / 6)
        assert _vals(out, "X")[3] == pytest.approx(20 / 6)
        assert _vals(out, "X")[4] == pytest.approx(26 / 6)


# ------------------------------------------------- 扩展批次 3：数学逐点族

class TestBatch3Math:
    def test_trig(self, df):
        out = evaluate_outputs("S:SIN(C-C);C1:COS(C-C);T:ATAN(C/C);", df)
        assert _vals(out, "S") == [0.0] * 5
        assert _vals(out, "C1") == [1.0] * 5
        assert _vals(out, "T") == [pytest.approx(0.7853981633974483)] * 5

    def test_asin_acos(self, df):
        out = evaluate_outputs("A:ASIN(C-C);B:ACOS(C/C);", df)
        assert _vals(out, "A") == [0.0] * 5
        assert _vals(out, "B") == [0.0] * 5

    def test_ceiling_floor_intpart_fracpart(self, df):
        # C/2 = [0.5, 1, 1.5, 2, 2.5]
        out = evaluate_outputs("A:CEILING(C/2);B:FLOOR(C/2);I:INTPART(C/2);F:FRACPART(C/2);", df)
        assert _vals(out, "A") == [1.0, 1.0, 2.0, 2.0, 3.0]
        assert _vals(out, "B") == [0.0, 1.0, 1.0, 2.0, 2.0]
        assert _vals(out, "I") == [0.0, 1.0, 1.0, 2.0, 2.0]
        assert _vals(out, "F") == [0.5, 0.0, 0.5, 0.0, 0.5]

    def test_intpart_trunc_toward_zero(self, df):
        # 负数向零取整（TDX INTPART），区别于 FLOOR
        out = evaluate_outputs("I:INTPART(0-C/2);F:FRACPART(0-C/2);", df)
        assert _vals(out, "I") == [0.0, -1.0, -1.0, -2.0, -2.0]
        assert _vals(out, "F") == [-0.5, 0.0, -0.5, 0.0, -0.5]

    def test_round_half_up(self, df):
        # TDX ROUND 四舍五入（half away from zero），区别于银行家舍入：
        # ROUND(0.5)=1 ROUND(1.5)=2 ROUND(2.5)=3；负数 ROUND(-0.5)=-1 ROUND(-2.5)=-3
        out = evaluate_outputs("A:ROUND(C/2);B:ROUND(0-C/2);", df)
        assert _vals(out, "A") == [1.0, 1.0, 2.0, 2.0, 3.0]
        assert _vals(out, "B") == [-1.0, -1.0, -2.0, -2.0, -3.0]

    def test_round2(self, df):
        # 保留 N 位小数四舍五入：C/4 = [0.25, 0.5, 0.75, 1, 1.25] → 1 位
        out = evaluate_outputs("X:ROUND2(C/4,1);", df)
        assert _vals(out, "X") == [pytest.approx(0.3), pytest.approx(0.5),
                                   pytest.approx(0.8), pytest.approx(1.0), pytest.approx(1.3)]

    def test_mod_fmod_semantics(self, df):
        # TDX MOD 为 fmod 语义（结果符号跟随被除数）：MOD(5,2)=1 MOD(-5,3)=-2
        out = evaluate_outputs("A:MOD(C,2);B:MOD(0-5,3);", df)
        assert _vals(out, "A") == [1.0, 0.0, 1.0, 0.0, 1.0]
        assert _vals(out, "B") == [-2.0] * 5

    def test_reverse(self, df):
        out = evaluate_outputs("X:REVERSE(C);", df)
        assert _vals(out, "X") == [-1.0, -2.0, -3.0, -4.0, -5.0]


# ------------------------------------------------- 扩展批次 3：位置/排名字幕

class TestBatch3Position:
    def test_bare_position_funcs(self, df):
        out = evaluate_outputs(
            "A:CURRBARSCOUNT;B:TOTALBARSCOUNT;C:ISLASTBAR;D:BARSTATUS;", df)
        assert _vals(out, "A") == [5.0, 4.0, 3.0, 2.0, 1.0]
        assert _vals(out, "B") == [5.0] * 5
        assert _vals(out, "C") == [0.0, 0.0, 0.0, 0.0, 1.0]
        assert _vals(out, "D") == [1.0, 0.0, 0.0, 0.0, 2.0]

    def test_hod_lod(self):
        # 窗口 [i-2..i] 部分窗口：HOD 第几大（最大=1）；LOD 第几小（最小=1）
        d2 = pl.DataFrame({
            "Open": [3.0, 1.0, 2.0, 5.0, 4.0], "High": [3.0, 1.0, 2.0, 5.0, 4.0],
            "Low": [3.0, 1.0, 2.0, 5.0, 4.0], "Close": [3.0, 1.0, 2.0, 5.0, 4.0],
            "Volume": [1.0] * 5, "Amount": [1.0] * 5,
        })
        out = evaluate_outputs("H:HOD(C,3);L:LOD(C,3);", d2)
        # bar0 [3]→1/1；bar1 [3,1]→2/1；bar2 [3,1,2]→2/2；bar3 [1,2,5]→1/3；bar4 [2,5,4]→2/2
        assert _vals(out, "H") == [1.0, 2.0, 2.0, 1.0, 2.0]
        assert _vals(out, "L") == [1.0, 1.0, 2.0, 3.0, 2.0]


# ------------------------------------------------- 扩展批次 3：逻辑族

class TestBatch3Logic:
    def test_between_closed(self, df):
        # BETWEEN 闭区间 B<=A<=C
        out = evaluate_outputs("X:BETWEEN(C,2,4);", df)
        assert _vals(out, "X") == [0.0, 1.0, 1.0, 1.0, 0.0]

    def test_range_open(self, df):
        # RANGE 开区间 B<A<C
        out = evaluate_outputs("X:RANGE(C,2,4);", df)
        assert _vals(out, "X") == [0.0, 0.0, 1.0, 0.0, 0.0]

    def test_not_func_form(self, df):
        out = evaluate_outputs("X:NOT(C>2);", df)
        assert _vals(out, "X") == [1.0, 1.0, 0.0, 0.0, 0.0]


# ------------------------------------------------- 扩展批次 3：统计/均线族

class TestBatch3StatsMa:
    def test_stdp(self, df):
        # 总体标准差（÷N）：相邻两数 [k,k+1] 方差 0.25 → std 0.5
        # TDX 怪癖：前 N 根 null（首个满窗口返 0，内置置 null 豁免）
        out = evaluate_outputs("X:STDP(C,2);", df)
        assert _vals(out, "X") == [None, None, 0.5, 0.5, 0.5]

    def test_covar(self, df):
        # 协方差（样本口径 ÷N-1 假设，对比验证校正）
        # C=[1,2,3,4,5], O=[1,1,2,3,3]
        # bar2 窗 C[2,3] O[1,2]: cov_smp = ((2-2.5)(1-1.5)+(3-2.5)(2-1.5))/1 = 0.5
        out = evaluate_outputs("X:COVAR(C,O,2);", df)
        assert _vals(out, "X") == [None, 0.0, 0.5, 0.5, 0.0]

    def test_relate(self, df):
        # 相关系数：bar2 C[2,3] O[1,2] 完全正相关 → 1.0；常数窗分母 0 → null
        out = evaluate_outputs("X:RELATE(C,O,2);", df)
        v = _vals(out, "X")
        assert v[0] is None and v[1] is None
        assert v[2] == pytest.approx(1.0)
        assert v[3] == pytest.approx(1.0)
        assert v[4] is None

    def test_mema(self, df):
        # MEMA(X,N): 前 N-1 根 null，Y[N-1]=前 N 根 SMA，之后 Y=(X+(N-1)Y')/N
        # C=[1,2,3,4,5], N=2: Y1=(1+2)/2=1.5, Y2=(3+1.5)/2=2.25, Y3=3.125, Y4=4.0625
        out = evaluate_outputs("X:MEMA(C,2);", df)
        assert _vals(out, "X") == [None, 1.5, 2.25, 3.125, 4.0625]

    def test_expmema(self, df):
        # EXPMEMA(X,N): 前 N-1 根 null，Y[N-1]=前 N 根 SMA，之后 Y=(2X+(N-1)Y')/(N+1)
        # C=[1,2,3,4,5], N=3: Y2=(1+2+3)/3=2.0, Y3=(8+4)/4=3.0, Y4=(10+6)/4=4.0
        out = evaluate_outputs("X:EXPMEMA(C,3);", df)
        assert _vals(out, "X") == [None, None, 2.0, 3.0, 4.0]


# ------------------------------------------------- 扩展批次 4：寻找极值/均线/逻辑引用

class TestBatch4Find:
    """FINDHIGH/FINDLOW/FINDHIGHBARS/FINDLOWBARS 寻找极值族。"""

    def test_findhigh(self, df):
        # FINDHIGH(C,2,3,1): 2周期前的3周期内第1大值
        # C=[1,2,3,4,5]
        # bar0: 窗口越界 → null
        # bar1: 窗口越界 → null
        # bar2: REF(C,2)=1, 窗口 [1] → 1.0
        # bar3: REF(C,2)=2, 窗口 [1,2] → 2.0
        # bar4: REF(C,2)=3, 窗口 [1,2,3] → 3.0
        out = evaluate_outputs("X:FINDHIGH(C,2,3,1);", df)
        assert _vals(out, "X") == [None, None, 1.0, 2.0, 3.0]

    def test_findlow(self, df):
        out = evaluate_outputs("X:FINDLOW(C,2,3,1);", df)
        assert _vals(out, "X") == [None, None, 1.0, 1.0, 1.0]

    def test_findhighbars(self, df):
        # FINDHIGHBARS(C,2,3,1): 2周期前的3周期内第1大值距今周期数
        out = evaluate_outputs("X:FINDHIGHBARS(C,2,3,1);", df)
        assert _vals(out, "X") == [None, None, 2.0, 2.0, 2.0]

    def test_findlowbars(self, df):
        out = evaluate_outputs("X:FINDLOWBARS(C,2,3,1);", df)
        assert _vals(out, "X") == [None, None, 2.0, 3.0, 4.0]


class TestBatch4Ma:
    """TMA/AMA 均线族。"""

    def test_tma(self, df):
        # TMA(X,A,B): Y=(A*Y'+B*X), 初值=X0
        # C=[1,2,3,4,5], A=0.5, B=0.5
        # Y0=1, Y1=0.5*1+0.5*2=1.5, Y2=0.5*1.5+0.5*3=2.25, Y3=3.125, Y4=4.0625
        out = evaluate_outputs("X:TMA(C,0.5,0.5);", df)
        assert _vals(out, "X") == [1.0, 1.5, 2.25, 3.125, 4.0625]

    def test_ama(self, df):
        # AMA(X,A): Y=Y'+A*(X-Y'), 初值=X0
        # C=[1,2,3,4,5], A=0.5
        # Y0=1, Y1=1+0.5*(2-1)=1.5, Y2=1.5+0.5*(3-1.5)=2.25, Y3=3.125, Y4=4.0625
        out = evaluate_outputs("X:AMA(C,0.5);", df)
        assert _vals(out, "X") == [1.0, 1.5, 2.25, 3.125, 4.0625]


class TestBatch4LogicRef:
    """BARSLASTS/BARSSINCEN/EXISTR/REFV/BACKSET/FILTERX/SUMBARSX。"""

    def test_barslasts(self, df):
        # BARSLASTS(X,N): 倒数第N次成立距今
        # C>O: [0,1,1,1,1], N=2
        # bar0: 无 → null
        # bar1: 第1次 → null（倒数第2次不存在）
        # bar2: 倒数第2次=bar1 → 1
        # bar3: 倒数第2次=bar2 → 1
        # bar4: 倒数第2次=bar3 → 1
        out = evaluate_outputs("X:BARSLASTS(C>O,2);", df)
        assert _vals(out, "X") == [None, None, 1.0, 1.0, 1.0]

    def test_barssincen(self, df):
        # BARSSINCEN(X,N): N周期内第一次X非0距今
        # C>O: [0,1,1,1,1], N=3
        # bar0: 窗口越界 → null
        # bar1: 窗口越界 → null
        # bar2: 窗口 [0,1,1], 首次=bar1 → 1
        # bar3: 窗口 [1,1,1], 首次=bar1 → 2
        # bar4: 窗口 [1,1,1], 首次=bar2 → 2（窗口内最早）
        out = evaluate_outputs("X:BARSSINCEN(C>O,3);", df)
        assert _vals(out, "X") == [None, None, 1.0, 2.0, 2.0]

    def test_existr(self, df):
        # EXISTR(X,A,B): 前A日到前B日间是否存在X非0
        # C>O: [0,1,1,1,1]
        # EXISTR(C>O,2,1): 前2日到前1日（即 bar[i-2] 到 bar[i-1]）
        # bar0: 窗口越界 → 0
        # bar1: 窗口越界 → 0
        # bar2: [0,1] → 1
        # bar3: [1,1] → 1
        # bar4: [1,1] → 1
        out = evaluate_outputs("X:EXISTR(C>O,2,1);", df)
        assert _vals(out, "X") == [0.0, 0.0, 1.0, 1.0, 1.0]

    def test_refv(self, df):
        # REFV(X,A): 引用A周期前（平滑处理），A=2
        # C=[1,2,3,4,5]
        # bar0/bar1: 越界 → 回填首个有效值 C0=1.0（I10 实证：TDX 前段填充）
        # bar2: C0=1.0
        # bar3: C1=2.0
        # bar4: C2=3.0
        out = evaluate_outputs("X:REFV(C,2);", df)
        assert _vals(out, "X") == [1.0, 1.0, 1.0, 2.0, 3.0]

    def test_backset(self, df):
        # BACKSET(X,N): 若X非0，将当前到前N周期设为1
        # C>O: [0,1,1,1,1], N=2
        # bar0: 0 → 0
        # bar1: 1 → 设 bar0,bar1 为 1
        # bar2: 1 → 设 bar1,bar2 为 1
        # bar3: 1 → 设 bar2,bar3 为 1
        # bar4: 1 → 设 bar3,bar4 为 1
        # 结果: [1,1,1,1,1]
        out = evaluate_outputs("X:BACKSET(C>O,2);", df)
        assert _vals(out, "X") == [1.0, 1.0, 1.0, 1.0, 1.0]

    def test_filterx(self, df):
        # FILTERX(X,N): 反向过滤，X满足后前N周期内置0
        # C>O: [0,1,1,1,1], N=2
        # bar0: 0 → 0
        # bar1: 1 → 保留，但 bar0 已在 N 内？FILTERX 是反向，从后往前
        # 实际：bar1 成立，检查 bar0 是否成立（在 N=2 内）→ bar0=0，保留 bar1
        # bar2 成立，检查 bar0,bar1 → bar1 成立，在 N=2 内 → bar2 置 0
        # bar3 成立，检查 bar1,bar2 → bar2=0, bar1=1 但 bar1 距 bar3 为 2 > N-1=1？需实证
        # 简化理解：FILTERX 保留每个成立点，但消灭其后 N 周期内的其他成立点
        # 从后往前：bar4 保留，bar3 在 bar4 前 N=2 内 → 置 0
        # bar2 在 bar4 前 N=2 内 → 置 0；bar1 保留
        # 结果: [0,1,0,0,1]
        out = evaluate_outputs("X:FILTERX(C>O,2);", df)
        assert _vals(out, "X") == [0.0, 1.0, 0.0, 0.0, 1.0]

    def test_sumbarsx(self, df):
        # SUMBARSX(X,A): 由近及远累加到 ≥A。TDX 实证：cnt==1（当根即够）→ -1；
        # cnt>=2 → cnt-1；不足 → null（I13：全历史值域 {-1,1,...,225}，无 0）
        # V=[10,20,30,40,50], A=100
        # bar0: 10<100 → null；bar1: 30 → null；bar2: 60 → null
        # bar3: 40+30+20+10=100，cnt=4 → 3
        # bar4: 50+40=90... +30=120，cnt=3 → 2
        out = evaluate_outputs("X:SUMBARSX(V,100);", df)
        assert _vals(out, "X") == [None, None, None, 3.0, 2.0]
        # 当根即 ≥A → -1
        out2 = evaluate_outputs("X:SUMBARSX(V,10);", df)
        assert _vals(out2, "X") == [-1.0, -1.0, -1.0, -1.0, -1.0]


class TestBatch4Sar:
    """SAR/SARTURN 抛物转向族（TDX 实证规则，TQVERIFY4 全历史零差异）。

    TDX 口径要点：前 N-1 根 null；第 N-1 根恒多初始化 SAR=LLV(L,N)、
    EP=HHV(H,N)；反转判定用上一根输出的 SAR；继续根钳制窗口含当根 [i,i-1]；
    转多 SAR=min(L[i-1],L[i])，转空 SAR=max(EP-step*(EP-L[i]),H[i],H[i-1])；
    SARTURN 由 SAR 与 Close 穿越派生，初始化根输出 SAR 值。
    """

    @pytest.fixture()
    def trend_df(self):
        # 先涨 5 根（多方向加速），再跌 5 根（触发反转），便于手算验证
        return pl.DataFrame({
            "Open":   [10.0, 11.0, 12.0, 13.0, 14.0, 14.8, 13.5, 12.5, 11.5, 10.5],
            "High":   [10.5, 11.5, 12.5, 13.5, 14.5, 15.0, 13.8, 12.8, 11.8, 10.8],
            "Low":    [9.8, 10.8, 11.8, 12.8, 13.8, 14.2, 12.9, 11.9, 10.9, 9.9],
            "Close":  [10.2, 11.2, 12.2, 13.2, 14.2, 14.6, 13.2, 12.2, 11.2, 10.2],
            "Volume": [1.0] * 10,
            "Amount": [1.0] * 10,
        })

    def test_sar_init_and_continue(self, trend_df):
        # N=3：bar0-1 null；bar2 初始化 SAR=LLV(L,3)=9.8，EP=HHV(H,3)=12.5，af=0.02
        out = evaluate_outputs("X:SAR(3,2,20);", trend_df)
        v = _vals(out, "X")
        assert v[0] is None and v[1] is None
        assert v[2] == pytest.approx(9.8)
        # bar3 多头继续：H=13.5>EP → EP=13.5, af=0.04；cand=9.8+0.04*(13.5-9.8)=9.948
        # 钳制 min(9.948, L3=12.8, L2=11.8)=9.948
        assert v[3] == pytest.approx(9.948)
        # bar4：EP=14.5, af=0.06；cand=9.948+0.06*(14.5-9.948)=10.22112；钳制不触发
        assert v[4] == pytest.approx(10.22112)

    def test_sar_reversal_rules(self, trend_df):
        # bar5：EP=15.0, af=0.08；SAR4=10.22112；L5=14.2 > SAR4 → 不反转
        # cand=10.22112+0.08*(15.0-10.22112)=10.6034304；钳制 min(...,14.2,13.8)=10.6034304
        out = evaluate_outputs("X:SAR(3,2,20);", trend_df)
        v = _vals(out, "X")
        assert v[5] == pytest.approx(10.6034304)
        # bar6：EP 不变 15.0（H6=13.8<15.0）；SAR5=10.6034304；L6=12.9 > SAR5 → 不反转
        # cand=10.6034304+0.08*(15.0-10.6034304)=10.955155968
        assert v[6] == pytest.approx(10.955155968)
        # bar7-9 同样 L > SAR（L7=11.9>10.955...，L8=10.9>..., L9=9.9 < ...?）
        # bar7：cand=10.955155968+0.08*(15.0-10.955155968)=11.2787435；L7=11.9>11.278 → 续
        assert v[7] == pytest.approx(11.27874352544)
        # bar8：cand=11.2787435+0.08*(15.0-11.2787435)=11.5764440；L8=10.9<11.576?
        #   但反转判定用 SAR7=11.2787435：L8=10.9 < 11.2787 → 反转向空！
        #   SAR8=max(EP-step*(EP-L8), H8, H7)=max(15.0-0.02*4.1, 12.8, 13.8)=max(14.918,12.8,13.8)=14.918
        assert v[8] == pytest.approx(14.918)

    def test_sarturn_derived_from_close_cross(self, trend_df):
        # SARTURN：初始化根输出 SAR 值；此后由 SAR 与 Close 穿越派生 ±1/0
        out = evaluate_outputs("X:SARTURN(3,2,20);", trend_df)
        v = _vals(out, "X")
        assert v[0] is None and v[1] is None
        assert v[2] == pytest.approx(9.8)  # 初始化根怪癖：输出 SAR 值
        # bar8 SAR=14.918 > C8=11.2 且 bar7 SAR=11.2787 <= C7=12.2？否：11.2787<=12.2 成立
        # → s1>c1 且 s0<=c0 → -1（反转向空）
        assert v[8] == -1.0
        # 其余延续根为 0
        assert v[3] == 0.0 and v[4] == 0.0 and v[5] == 0.0


# ---------------------------------------------------------------- 扩展批次 5

class TestBatch5Time:
    """时间分量族：YEAR/MONTH/DAY/HOUR/MINUTE/WEEKDAY/DATE/TIME。

    均为裸标识符形式（TDX 无参数引用函数），依赖 df 的 datetime 列。
    DATE/TIME 的复合编码按 TDX 常规口径预置（(year-1900)*10000+m*100+d、
    h*10000+m*100+s），最终以 TQVERIFY5 在线对比实证为准。
    """

    @pytest.fixture()
    def dt_df(self):
        # 2026-01-05 为周一，5 根 5 分钟 bar（09:30 起）
        return pl.DataFrame({
            "datetime": [
                datetime(2026, 1, 5, 9, 30),
                datetime(2026, 1, 5, 9, 35),
                datetime(2026, 1, 5, 9, 40),
                datetime(2026, 1, 5, 9, 45),
                datetime(2026, 1, 5, 9, 50),
            ],
            "Open": [1.0, 1.0, 2.0, 3.0, 3.0],
            "High": [1.5, 2.5, 3.0, 4.0, 4.0],
            "Low": [0.5, 1.0, 1.5, 2.5, 2.5],
            "Close": [1.0, 2.0, 3.0, 4.0, 5.0],
            "Volume": [10.0, 20.0, 30.0, 40.0, 50.0],
            "Amount": [100.0, 200.0, 300.0, 400.0, 500.0],
        })

    def test_year_month_day(self, dt_df):
        out = evaluate_outputs("Y:YEAR;M:MONTH;D:DAY;", dt_df)
        assert _vals(out, "Y") == [2026.0] * 5
        assert _vals(out, "M") == [1.0] * 5
        assert _vals(out, "D") == [5.0] * 5

    def test_hour_minute(self, dt_df):
        out = evaluate_outputs("H:HOUR;MI:MINUTE;", dt_df)
        assert _vals(out, "H") == [9.0] * 5
        assert _vals(out, "MI") == [30.0, 35.0, 40.0, 45.0, 50.0]

    def test_weekday(self):
        # TDX 口径：周日=0，周一=1 … 周六=6（词典：有效值范围 (1,2,3,4,5,6,0)）
        # 2026-01-03 周六 / 01-04 周日 / 01-05 周一 / 01-06 周二 / 01-07 周三
        week_df = pl.DataFrame({
            "datetime": [
                datetime(2026, 1, 3), datetime(2026, 1, 4), datetime(2026, 1, 5),
                datetime(2026, 1, 6), datetime(2026, 1, 7),
            ],
            "Open": [1.0] * 5, "High": [1.0] * 5, "Low": [1.0] * 5,
            "Close": [1.0] * 5, "Volume": [1.0] * 5, "Amount": [1.0] * 5,
        })
        out = evaluate_outputs("X:WEEKDAY;", week_df)
        assert _vals(out, "X") == [6.0, 0.0, 1.0, 2.0, 3.0]

    def test_date(self, dt_df):
        # DATE = (year-1900)*10000 + month*100 + day → 2026-01-05 → 1260105
        out = evaluate_outputs("X:DATE;", dt_df)
        assert _vals(out, "X") == [1260105.0] * 5

    def test_time(self, dt_df):
        # TIME = h*10000 + m*100 + s → 09:30:00 → 93000
        out = evaluate_outputs("X:TIME;", dt_df)
        assert _vals(out, "X") == [93000.0, 93500.0, 94000.0, 94500.0, 95000.0]

    def test_paren_form_compat(self, dt_df):
        # 括号空参形式兼容（部分平台习惯写 YEAR()）
        assert _vals(evaluate_outputs("X:YEAR();", dt_df), "X") == [2026.0] * 5

    def test_time_funcs_require_datetime(self, df):
        # df 无 datetime 列 → 显式报错（不静默返回 0）
        with pytest.raises(FormulaEvalError, match="datetime"):
            evaluate_outputs("X:YEAR;", df)


# ---------------------------------------------------------------- 扩展批次 6

def _d2_df() -> pl.DataFrame:
    # 复用 HOD/LOD 的极值验证序列 [3,1,2,5,4]
    return pl.DataFrame({
        "Open": [3.0, 1.0, 2.0, 5.0, 4.0], "High": [3.0, 1.0, 2.0, 5.0, 4.0],
        "Low": [3.0, 1.0, 2.0, 5.0, 4.0], "Close": [3.0, 1.0, 2.0, 5.0, 4.0],
        "Volume": [1.0] * 5, "Amount": [1.0] * 5,
    })


class TestBatch6Alias:
    """EXPMA=EMA、SGN=SIGN（词典标注的别名函数）。"""

    def test_expma_is_ema(self, df):
        a = _vals(evaluate_outputs("X:EXPMA(C,3);", df), "X")
        b = _vals(evaluate_outputs("Y:EMA(C,3);", df), "Y")
        assert a == b == [1.0, 1.5, 2.25, 3.125, 4.0625]

    def test_sgn_is_sign(self, df):
        out = evaluate_outputs("X:SGN(C-3);", df)
        assert _vals(out, "X") == [-1.0, -1.0, 0.0, 1.0, 1.0]


class TestBatch6RangeRank:
    """TOPRANGE/LOWRANGE：当前值作为极值可向前追溯的前导 bar 数（不含当根）。

    即「最大窗口长度-1」：TOPRANGE = 连续前导 bar 值均 ≤ 当根的个数
    （LOWRANGE 反之 ≥）。TQVERIFY6 K03/K04 在线实证：递减序列 TOPRANGE
    恒 0、LOWRANGE=i。
    """

    def test_toprange(self):
        out = evaluate_outputs("X:TOPRANGE(C);", _d2_df())
        # C=[3,1,2,5,4] → bar0 无前导=0；bar1 前导 3>1 即断=0；bar2 仅 1≤2=1；
        # bar3 前导全部 ≤5=3；bar4 前导 5>4 即断=0
        assert _vals(out, "X") == [0.0, 0.0, 1.0, 3.0, 0.0]

    def test_lowrange(self):
        out = evaluate_outputs("X:LOWRANGE(C);", _d2_df())
        # bar1 前导 3≥1 不断=1；bar2 前导 1<2 即断=0；bar4 仅 5≥4=1
        assert _vals(out, "X") == [0.0, 1.0, 0.0, 0.0, 1.0]

    def test_monotonic_history(self):
        # 单调上涨 → TOPRANGE 前导全 ≤ = [0,1,2,3,4]；LOWRANGE 前导即断 = 全 0
        up = pl.DataFrame({
            "Open": [1.0] * 5, "High": [1.0] * 5, "Low": [1.0] * 5,
            "Close": [1.0, 2.0, 3.0, 4.0, 5.0],
            "Volume": [1.0] * 5, "Amount": [1.0] * 5,
        })
        out = evaluate_outputs("A:TOPRANGE(C);B:LOWRANGE(C);", up)
        assert _vals(out, "A") == [0.0, 1.0, 2.0, 3.0, 4.0]
        assert _vals(out, "B") == [0.0] * 5

    def test_monotonic_down(self):
        # 单调下跌 → LOWRANGE=[0,1,2,3,4]（TQVERIFY6 K04 递减序列实证形态）
        down = pl.DataFrame({
            "Open": [1.0] * 5, "High": [1.0] * 5, "Low": [1.0] * 5,
            "Close": [5.0, 4.0, 3.0, 2.0, 1.0],
            "Volume": [1.0] * 5, "Amount": [1.0] * 5,
        })
        out = evaluate_outputs("A:TOPRANGE(C);B:LOWRANGE(C);", down)
        assert _vals(out, "A") == [0.0] * 5
        assert _vals(out, "B") == [0.0, 1.0, 2.0, 3.0, 4.0]

    def test_tie_breaks_streak(self):
        # 并列值同样断开 streak（TQVERIFY6 K03/K04 bar95 实证：前导=当根 → 0）
        tie = pl.DataFrame({
            "Open": [1.0] * 4, "High": [1.0] * 4, "Low": [1.0] * 4,
            "Close": [2.0, 2.0, 3.0, 3.0],
            "Volume": [1.0] * 4, "Amount": [1.0] * 4,
        })
        out = evaluate_outputs("A:TOPRANGE(C);B:LOWRANGE(C);", tie)
        # TOPRANGE：bar2 前导 2,2 均严格小于 3 → 2；bar3 前导 3 不小于 → 0
        # LOWRANGE：前导无严格大于当根 → 全 0
        assert _vals(out, "A") == [0.0, 0.0, 2.0, 0.0]
        assert _vals(out, "B") == [0.0] * 4


class TestBatch6PriceLimit:
    """ZTPRICE/DTPRICE：涨跌停价 = 精确十进制四舍五入(X*(1±A), 2)。"""

    def test_ztprice(self, df):
        # REF(C,1)=[N,1,2,3,4] → [N,1.1,2.2,3.3,4.4]
        out = evaluate_outputs("X:ZTPRICE(REF(C,1),0.1);", df)
        assert _vals(out, "X") == [None, 1.1, 2.2, 3.3, 4.4]

    def test_dtprice(self, df):
        out = evaluate_outputs("X:DTPRICE(REF(C,1),0.1);", df)
        assert _vals(out, "X") == [None, 0.9, 1.8, 2.7, 3.6]

    def test_rounding_half_up(self, df):
        # 9.87*1.1=10.857 → 10.86；9.87*0.9=8.883 → 8.88
        out = evaluate_outputs(
            "Z:ZTPRICE(C*0+9.87,0.1);D:DTPRICE(C*0+9.87,0.1);", df)
        assert _vals(out, "Z") == [10.86] * 5
        assert _vals(out, "D") == [8.88] * 5

    def test_rounding_exact_decimal_xx5(self, df):
        # 精确十进制 .xx5 亦进位：46.15*1.1=50.765 → 50.77
        #（二进制浮点 50.764999... 直接 half-up 会漏进位，TQVERIFY6 K05 实证）
        out = evaluate_outputs("X:ZTPRICE(C*0+46.15,0.1);", df)
        assert _vals(out, "X") == [50.77] * 5


class TestBatch6Accum:
    """MULAR：窗口累乘（部分窗口口径假设，与 SUM 一致）。"""

    def test_mular(self, df):
        out = evaluate_outputs("X:MULAR(V,2);", df)
        assert _vals(out, "X") == [10.0, 200.0, 600.0, 1200.0, 2000.0]

    def test_mular_w3(self, df):
        out = evaluate_outputs("X:MULAR(V,3);", df)
        assert _vals(out, "X") == [10.0, 200.0, 6000.0, 24000.0, 60000.0]


class TestBatch6TimeFilter:
    """TFILT/REFDATE/TIMETOSEC/IFN/FROMOPEN 族。"""

    @pytest.fixture()
    def week_df(self):
        # 2026-01-03 周六 ~ 01-07 周三，C 递增便于 REFDATE 定位
        return pl.DataFrame({
            "datetime": [
                datetime(2026, 1, 3), datetime(2026, 1, 4), datetime(2026, 1, 5),
                datetime(2026, 1, 6), datetime(2026, 1, 7),
            ],
            "Open": [3.0] * 5, "High": [3.0] * 5, "Low": [3.0] * 5,
            "Close": [3.0, 4.0, 5.0, 6.0, 7.0],
            "Volume": [1.0] * 5, "Amount": [1.0] * 5,
        })

    def test_tfilt_keeps_window(self, week_df):
        # 2026-01-03~07（1260103~1260107）全部落在窗口内 → 原值
        out = evaluate_outputs("X:TFILT(C,1251231,0,1260107,2400);", week_df)
        assert _vals(out, "X") == [3.0, 4.0, 5.0, 6.0, 7.0]

    def test_tfilt_outside_null(self, week_df):
        # 窗口外 → 无效（null 假设，TQVERIFY6 K08 实证）
        out = evaluate_outputs("X:TFILT(C,1250101,0,1251231,2400);", week_df)
        assert _vals(out, "X") == [None] * 5

    def test_tfilt_time_boundary(self):
        # dt_df 时间 09:30~09:50（HHMM 编码 930~950）；窗口 [9:40, 10:00] 含端点
        dt5 = pl.DataFrame({
            "datetime": [
                datetime(2026, 1, 5, 9, 30), datetime(2026, 1, 5, 9, 35),
                datetime(2026, 1, 5, 9, 40), datetime(2026, 1, 5, 9, 45),
                datetime(2026, 1, 5, 9, 50),
            ],
            "Open": [1.0] * 5, "High": [1.0] * 5, "Low": [1.0] * 5,
            "Close": [1.0, 2.0, 3.0, 4.0, 5.0],
            "Volume": [1.0] * 5, "Amount": [1.0] * 5,
        })
        out = evaluate_outputs("X:TFILT(C,1260105,940,1260105,1000);", dt5)
        assert _vals(out, "X") == [None, None, 3.0, 4.0, 5.0]

    def test_tfilt_reversed_window_empty(self):
        # 词典语义为有序区间（D1/M1 → D2/M2，如 1040101,1025→1040101,1345）；
        # 起点晚于终点（M1>M2）为空窗 → 全部无效（TQVERIFY6 K08 在线复核宽窗行为）
        dt5 = pl.DataFrame({
            "datetime": [
                datetime(2026, 1, 5, 9, 30), datetime(2026, 1, 5, 9, 35),
                datetime(2026, 1, 5, 9, 40), datetime(2026, 1, 5, 9, 45),
                datetime(2026, 1, 5, 9, 50),
            ],
            "Open": [1.0] * 5, "High": [1.0] * 5, "Low": [1.0] * 5,
            "Close": [1.0, 2.0, 3.0, 4.0, 5.0],
            "Volume": [1.0] * 5, "Amount": [1.0] * 5,
        })
        out = evaluate_outputs("X:TFILT(C,1260105,1000,1260105,940);", dt5)
        assert _vals(out, "X") == [None] * 5

    def test_refdate(self, week_df):
        # 引用 2026-01-05（1260105）那根的 C=5，整列平铺
        out = evaluate_outputs("X:REFDATE(C,1260105);", week_df)
        assert _vals(out, "X") == [5.0] * 5

    def test_refdate_missing(self, week_df):
        out = evaluate_outputs("X:REFDATE(C,1260110);", week_df)
        assert _vals(out, "X") == [None] * 5

    def test_timetosec_const(self):
        out = evaluate_outputs("X:TIMETOSEC(93000);", _d2_df())
        assert _vals(out, "X") == [34200.0] * 5

    def test_ifn(self, df):
        # IFN(X,A,B)：X 为假取 A，为真取 B（条件取反的 IF）
        out = evaluate_outputs("X:IFN(C>O,10,20);", df)
        assert _vals(out, "X") == [10.0, 20.0, 20.0, 20.0, 20.0]


class TestBatch6Bare:
    """FROMOPEN/TOTALFZNUM/USEDDATANUM（裸标识符）。"""

    @pytest.fixture()
    def intraday_df(self):
        # 覆盖开盘前/开盘/上午/午休/下午/收盘时刻
        return pl.DataFrame({
            "datetime": [
                datetime(2026, 1, 5, 9, 25), datetime(2026, 1, 5, 9, 30),
                datetime(2026, 1, 5, 10, 30), datetime(2026, 1, 5, 11, 30),
                datetime(2026, 1, 5, 13, 0), datetime(2026, 1, 5, 15, 0),
            ],
            "Open": [1.0] * 6, "High": [1.0] * 6, "Low": [1.0] * 6,
            "Close": [1.0] * 6, "Volume": [1.0] * 6, "Amount": [1.0] * 6,
        })

    def test_fromopen(self, intraday_df):
        # 距开盘分钟数：9:30=0，10:30=60，11:30=120，13:00=120，15:00=240
        out = evaluate_outputs("X:FROMOPEN;", intraday_df)
        assert _vals(out, "X") == [0.0, 0.0, 60.0, 120.0, 120.0, 240.0]

    def test_totalfznum_assume_fromopen(self, intraday_df):
        # 词典描述与 FROMOPEN 相同，暂按同值实现，TQVERIFY6 K10 实证后修正
        a = _vals(evaluate_outputs("X:FROMOPEN;", intraday_df), "X")
        b = _vals(evaluate_outputs("Y:TOTALFZNUM;", intraday_df), "Y")
        assert a == b

    def test_useddatanum_and_totalbarscount(self, df):
        # 假设均等于总 bar 数（TQVERIFY6 K12 实证 USEDDATANUM）
        out = evaluate_outputs("A:USEDDATANUM;T:TOTALBARSCOUNT;", df)
        assert _vals(out, "A") == [5.0] * 5
        assert _vals(out, "T") == [5.0] * 5

    def test_fromopen_daily_midnight(self):
        # 日线 bar 时间戳 00:00（TQ 日线索引口径）→ TDX 按整日上下文返回
        # 全天 240 分钟（TQVERIFY6 K09/K10 实证）
        daily = pl.DataFrame({
            "datetime": [datetime(2026, 1, 5), datetime(2026, 1, 6)],
            "Open": [1.0] * 2, "High": [1.0] * 2, "Low": [1.0] * 2,
            "Close": [1.0] * 2, "Volume": [1.0] * 2, "Amount": [1.0] * 2,
        })
        out = evaluate_outputs("A:FROMOPEN;B:TOTALFZNUM;", daily)
        assert _vals(out, "A") == [240.0, 240.0]
        assert _vals(out, "B") == [240.0, 240.0]


# ---------------------------------------------------------------- 扩展批次 7

class TestBatch7DynamicWindow:
    """REF/SUM/LLV/HHV 的窗口参数支持序列（逐 bar 动态，TDX 常用倒推写法）。

    语义沿用各函数静态口径的逐 bar 版本：
    - REF(X,N)：out[i]=X[i-n[i]]，n 为 null/越界 → null（负偏移为未来引用，越界 null）
    - SUM(X,N)：窗口 [i-n+1, i]，部分窗口口径同静态 SUM；n=0 → expanding；
      n 为 null → 按 0（expanding，TQVERIFY7 L02/L06 实证）
    - LLV/HHV(X,N)：同上，n=0 → 历史累计；n 为 null → null
    动态窗口值小数按 trunc 取整（BARSLAST 输出为整值 Float64，TQVERIFY7 实证）。
    其余窗口族（MA/COUNT 等）仍要求常量，不在本批次范围。
    """

    def test_ref_dyn_barslast(self, df):
        # BARSLAST(C=3)=[N,N,0,1,2] → REF 取 [N,N,C2,C2,C2]
        out = evaluate_outputs("X:REF(C,BARSLAST(C=3));", df)
        assert _vals(out, "X") == [None, None, 3.0, 3.0, 3.0]

    def test_ref_dyn_offset_plus1(self, df):
        # 偏移 1/2/3 → 均取 C[1]=2
        out = evaluate_outputs("X:REF(C,BARSLAST(C=3)+1);", df)
        assert _vals(out, "X") == [None, None, 2.0, 2.0, 2.0]

    def test_sum_dyn_barslast(self, df):
        # BARSLAST(C=3)=[N,N,0,1,2] → 前 2 根 null 窗口按 expanding：
        # bar0=1、bar1=3、bar2 w=0 expanding=6、bar3 w=1 → 4、bar4 w=2 → 9
        out = evaluate_outputs("X:SUM(C,BARSLAST(C=3));", df)
        assert _vals(out, "X") == [1.0, 3.0, 6.0, 4.0, 9.0]

    def test_llv_dyn(self, df):
        out = evaluate_outputs("X:LLV(C,BARSLAST(C=3)+1);", df)
        assert _vals(out, "X") == [None, None, 3.0, 3.0, 3.0]

    def test_hhv_dyn(self, df):
        # bar2 w=1 → 3；bar3 w=2 → max(3,4)=4；bar4 w=3 → 5
        out = evaluate_outputs("X:HHV(C,BARSLAST(C=3)+1);", df)
        assert _vals(out, "X") == [None, None, 3.0, 4.0, 5.0]

    def test_dyn_null_window(self, df):
        # 条件从未成立 → BARSLAST 全 null：REF/LLV/HHV 窗口无效 → 全 null
        out = evaluate_outputs("X:REF(C,BARSLAST(C=99));", df)
        assert _vals(out, "X") == [None] * 5
        out = evaluate_outputs("X:HHV(C,BARSLAST(C=99));", df)
        assert _vals(out, "X") == [None] * 5

    def test_sum_dyn_null_window_expanding(self, df):
        # SUM 的 null 窗口按 0（expanding）处理（TQVERIFY7 L02/L06 实证），
        # 区别于 REF/HHV/LLV 的 null→null
        out = evaluate_outputs("X:SUM(C,BARSLAST(C=99));", df)
        assert _vals(out, "X") == [1.0, 3.0, 6.0, 10.0, 15.0]

    def test_ref_dyn_negative_offset(self, df):
        # 负偏移 = 未来引用（与静态 REF(C,-1)=shift(-1) 一致）：下一根的 C
        out = evaluate_outputs("X:REF(C,C*0-1);", df)
        assert _vals(out, "X") == [2.0, 3.0, 4.0, 5.0, None]

    def test_ref_dyn_fraction_floor(self, df):
        # 动态偏移小数按 trunc 取整：1.9 → 1
        out = evaluate_outputs("X:REF(C,C*0+1.9);", df)
        assert _vals(out, "X") == [None, 1.0, 2.0, 3.0, 4.0]


# ---------------------------------------------------------------- 扩展批次 8

class TestBatch8Core:
    """批次 8 核心组：XMA/HHVLLV/MEDIAN/UPDOWN/MAX6/NEWSAR/DRAWNULL。

    语义以 TQVERIFY8 在线对比实证为准（000001.SZ 全历史逐 bar）：
    - XMA：偏移移动平均，窗口 [i-M, i+M]（M=N//2），左端截断取可用数据，
      词典注明使用未来 N/2 日数据（precheck leak 警告）
    - HHVLLV(X,T,N1,N2)：窗口 [i-N1, i-N2]（N2=0 → 含当根），T=0 最高 / T=1 最低；
      无效窗口（i-N2<0）输出 0.0 非 null；N1=0 或 N1<N2 → 整线 0.0（非 expanding）；
      预热段分路径（M03/M04 实证）：T=0 左端截断取可用数据，T=1 要求完整窗口
      （i<N1 → 0.0，非左截断）
    - MEDIAN(X,M,N)：窗口 [i-M-N+1, i-M] 中位数，左端截断；中位数取
      sorted[n//2]（偶数取上中位，非两中位均值）
    - UPDOWN(X)：SIGN(X-REF(X,1)) → -1/0/1，首根 null
    - MAX6：6 参数逐点最大值（null 按 0 处理，同 MAX 口径）
    - NEWSAR(N,S)：步长 S/1000 无 af 上限；空头初始化 SAR=HHV(H,N) k=0；
      当根创新极值加步（空头 L 新低/多头 H 新高，翻转后首根抑制），
      cand=SAR'+step*k*(当根L/H-SAR')；触发比较在当根递推值上
      （空头 C>cand 转多 / 多头 C<cand 转空）；翻转锚=前 N 根窗口极值
      （不含当根）、翻转后 k 重置 1（TQVERIFY8 8471 根零差异）
    - DRAWNULL：恒 null 占位
    """

    def test_xma(self, df):
        # 偏移 MA 假设 N=3 → 窗口 [i-1, i+1]：
        # bar0 [0,1]→1.5；bar1 [0,2]→2；bar2 [1,3]→3；bar3 [2,4]→4；bar4 [3,4]→4.5
        out = evaluate_outputs("X:XMA(C,3);", df)
        assert _vals(out, "X") == [1.5, 2.0, 3.0, 4.0, 4.5]

    def test_hhvllv_high(self, df):
        # HHVLLV(C,0,3,1)：窗口 [i-3, i-1]，左端截断；无效窗口（i-N2<0）→ 0.0
        # bar0 [—]→0.0；bar1 [0,0]→1；bar2 [0,1]→2；bar3 [0,2]→3；bar4 [1,3]→4
        out = evaluate_outputs("X:HHVLLV(C,0,3,1);", df)
        assert _vals(out, "X") == [0.0, 1.0, 2.0, 3.0, 4.0]

    def test_hhvllv_low_with_current(self, df):
        # T=1 最低，N2=0 含当根：HHVLLV(C,1,3,0)；T=1 路径要求完整窗口
        # （M04 实证：i<N1 输出 0.0，非左截断）→ bar0..2 → 0.0
        # bar3 [0,3]→1；bar4 [1,4]→2
        out = evaluate_outputs("X:HHVLLV(C,1,3,0);", df)
        assert _vals(out, "X") == [0.0, 0.0, 0.0, 1.0, 2.0]

    def test_hhvllv_low_full_window_m04(self, df):
        # M04 镜像（TQVERIFY8 在线实证：HHVLLV(C,1,10,3) bar0..9 → 0.0，
        # bar10 起才有值）：HHVLLV(C,1,4,2) → bar0..3 → 0.0；bar4 [0,2]→1
        out = evaluate_outputs("X:HHVLLV(C,1,4,2);", df)
        assert _vals(out, "X") == [0.0, 0.0, 0.0, 0.0, 1.0]

    def test_hhvllv_n1_zero_whole_line_zero(self, df):
        # 实证：N1=0 非 expanding，整线 0.0
        out = evaluate_outputs("X:HHVLLV(C,1,0,0);", df)
        assert _vals(out, "X") == [0.0] * 5

    def test_median(self, df):
        # MEDIAN(C,0,3)：窗口 [i-2, i] 左端截断，上中位 sorted[n//2]
        # bar0 [1]→1；bar1 [1,2]→2（上中位）；bar2 [1,2,3]→2；bar3→3；bar4→4
        out = evaluate_outputs("X:MEDIAN(C,0,3);", df)
        assert _vals(out, "X") == [1.0, 2.0, 2.0, 3.0, 4.0]
        # MEDIAN(C,1,2)：窗口 [i-2, i-1]：bar0 右端 i-1<0 → null；bar1 [0,0]→1；
        # bar2 [0,1]→2（上中位）；bar3 [1,2]→3；bar4 [2,3]→4
        out = evaluate_outputs("Y:MEDIAN(C,1,2);", df)
        assert _vals(out, "Y") == [None, 1.0, 2.0, 3.0, 4.0]

    def test_updown(self, df):
        # O=[1,1,2,3,3] → 差分 [N,0,+1,+1,0] → sign
        out = evaluate_outputs("X:UPDOWN(O);", df)
        assert _vals(out, "X") == [None, 0.0, 1.0, 1.0, 0.0]

    def test_max6(self, df):
        out = evaluate_outputs("X:MAX6(C,O,1,2,0,3);", df)
        assert _vals(out, "X") == [3.0, 3.0, 3.0, 4.0, 5.0]

    def test_newsar(self):
        # 逆向实证语义（TQVERIFY8，000001.SZ 全历史 8471 根零差异）：
        # 步长 S/1000 无 af 上限；触发比较在当根递推值上（C vs cand，
        # 非前根 SAR、非盘中 H/L 穿越）；翻转锚=前 N 根窗口极值（不含当根）
        ns_df = pl.DataFrame({
            "Open": [10.0, 10.0, 11.0, 12.0, 12.5, 13.0, 13.2, 13.5, 13.8, 13.6, 9.5, 9.0],
            "High": [11.0, 12.0, 13.0, 12.5, 13.0, 13.4, 13.8, 14.0, 13.9, 13.7, 10.0, 9.5],
            "Low": [9.0, 10.0, 10.0, 11.0, 12.0, 12.5, 12.8, 13.0, 13.2, 9.0, 8.5, 8.0],
            "Close": [10.0, 11.0, 12.0, 12.2, 12.9, 13.2, 13.5, 13.8, 13.6, 9.5, 9.0, 8.5],
            "Volume": [1.0] * 12, "Amount": [1.0] * 12,
        })
        out = evaluate_outputs("X:NEWSAR(3,50);", ns_df)
        got = _vals(out, "X")
        # bar2 锚=HHV(H,3)=13 空头 k=0；bar3/4 L 未新低 k=0 cand 恒 13；
        # bar5 C=13.2>13 翻多锚=min(L[2:5])=10、k 重置 1；bar6 翻转后首根抑制不加步；
        # bar7 H 新高 k=2；bar9 C=9.5<cand=11.1835 翻空锚=max(H[6:9])=14；
        # bar10 首根抑制（L 新低不加步）；bar11 L 新低 k=2
        exp = [None, None, 13.0, 13.0, 13.0, 10.0, 10.19, 10.571, 10.9039,
               14.0, 13.725, 13.1525]
        assert [None if v is None else round(v, 6) for v in got] == exp

    def test_drawnull(self, df):
        out = evaluate_outputs("X:DRAWNULL;", df)
        assert _vals(out, "X") == [None] * 5
        # 配合 IF 控制不画：条件假 → null
        out = evaluate_outputs("Y:IF(C>2,C,DRAWNULL);", df)
        assert _vals(out, "Y") == [None, None, 3.0, 4.0, 5.0]


class TestBatch8Filter:
    """批次 8 信号过滤组：TFILTER/TTFILTER（状态机配对去抖）。

    语义假设（TDX 帮助口径）：
    - TFILTER(A,B,N)：开仓 A → 平仓 B 必须配对；连续同方向只有首个有效。
      N=1 输出存续开仓信号 1.0；N=2 输出存续平仓信号 1.0；N=0 输出 1/2 编码
    - TTFILTER(买开,卖平,卖开,买平,N)：四态配对（flat→买开→long→卖平→flat→
      卖开→short→买平→flat）；N=1..4 输出对应类型存续信号 1.0；N=0 输出 1/2/3/4 编码
    """

    @pytest.fixture()
    def sig_df(self):
        # C=[1,2,3,2,1,2,3,3]：A(开仓)=C>2 于 bar2/6/7；B(平仓)=C<2 于 bar0/4
        return pl.DataFrame({
            "Open": [1.0] * 8, "High": [3.0] * 8, "Low": [1.0] * 8,
            "Close": [1.0, 2.0, 3.0, 2.0, 1.0, 2.0, 3.0, 3.0],
            "Volume": [1.0] * 8, "Amount": [1.0] * 8,
        })

    def test_tfilter_n1(self, sig_df):
        # bar2 开仓→1.0；bar4 平仓（N=1 不出）；bar6 再开仓→1.0；bar7 重复开仓被滤
        out = evaluate_outputs("A:C>2;B:C<2;X:TFILTER(A,B,1);", sig_df)
        assert _vals(out, "X") == [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]

    def test_tfilter_n2(self, sig_df):
        out = evaluate_outputs("A:C>2;B:C<2;X:TFILTER(A,B,2);", sig_df)
        assert _vals(out, "X") == [0.0] * 4 + [1.0, 0.0, 0.0, 0.0]

    def test_tfilter_n0_codes(self, sig_df):
        # N=0：开仓出 1、平仓出 2
        out = evaluate_outputs("A:C>2;B:C<2;X:TFILTER(A,B,0);", sig_df)
        assert _vals(out, "X") == [0.0, 0.0, 1.0, 0.0, 2.0, 0.0, 1.0, 0.0]

    def test_tfilter_unmatched_close_ignored(self, sig_df):
        # 无开仓时平仓信号被忽略（bar0 B 成立但不输出）
        out = evaluate_outputs("A:C>2;B:C<2;X:TFILTER(A,B,2);", sig_df)
        assert _vals(out, "X")[0] == 0.0

    def test_ttfilter_cycle(self):
        # 买开@1 → 卖平@3 → 卖开@5 → 买平@7；期间重复/错配信号全滤
        d8 = pl.DataFrame({
            "Open": [1.0] * 8, "High": [1.0] * 8, "Low": [1.0] * 8,
            "Close": [1.0] * 8, "Volume": [1.0] * 8, "Amount": [1.0] * 8,
        })
        env = ("KA:BARSCOUNT(C)=1;KB:BARSCOUNT(C)=3;"
               "KC:BARSCOUNT(C)=5;KD:BARSCOUNT(C)=7;")
        out = evaluate_outputs(
            env + "X:TTFILTER(KA,KB,KC,KD,0);", d8)
        assert _vals(out, "X") == [0.0, 1.0, 0.0, 2.0, 0.0, 3.0, 0.0, 4.0]

    def test_ttfilter_select_type(self):
        # N=3 → 第 3 路信号可执行位（KC@5，flat）出 1.0
        d8 = pl.DataFrame({
            "Open": [1.0] * 8, "High": [1.0] * 8, "Low": [1.0] * 8,
            "Close": [1.0] * 8, "Volume": [1.0] * 8, "Amount": [1.0] * 8,
        })
        env = ("KA:BARSCOUNT(C)=1;KB:BARSCOUNT(C)=3;"
               "KC:BARSCOUNT(C)=5;KD:BARSCOUNT(C)=7;")
        out = evaluate_outputs(env + "X:TTFILTER(KA,KB,KC,KD,3);", d8)
        assert _vals(out, "X") == [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0]

    def test_ttfilter_route_overlap_m18(self):
        # TQVERIFY8 M18 实证：第 3 路与第 1 路同信号时，flat 同真只执行
        # 买开（code=1），但 N=3 线同样在买开位出 1.0（标记独立于执行的路）
        d8 = pl.DataFrame({
            "Open": [1.0] * 8, "High": [1.0] * 8, "Low": [1.0] * 8,
            "Close": [1.0] * 8, "Volume": [1.0] * 8, "Amount": [1.0] * 8,
        })
        env = ("KA:BARSCOUNT(C)=1;KB:BARSCOUNT(C)=3;KC:KA;KD:BARSCOUNT(C)=999;")
        out0 = evaluate_outputs(env + "X:TTFILTER(KA,KB,KC,KD,0);", d8)
        assert _vals(out0, "X") == [0.0, 1.0, 0.0, 2.0, 0.0, 0.0, 0.0, 0.0]
        out3 = evaluate_outputs(env + "X:TTFILTER(KA,KB,KC,KD,3);", d8)
        assert _vals(out3, "X") == [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


class TestBatch8TimePeriod:
    """批次 8 时间/周期组：WEEKOFYEAR/PERIOD。

    语义以 TQVERIFY8/8B 在线对比实证为准：
    - WEEKOFYEAR：周一起始；周日 bar 归入下一周（+1 天后计算，1991-1993
      周日调休交易日实证）；第 1 周 = 含 1 月 1 日的整周（周一~周日），但
      1/1 恰为周日时该周日归上一年末周、第 1 周从 1/2（周一）开始
      （1995/2006/2012/2017/2023 五个 1/1 周日年份整年 -1 实证）；
      w = (rd - rd 所年锚点周一).days // 7 + 1
    - PERIOD：周期编号 1m=0, 5m=1, 15m=2, 30m=3, 60m(1h)=4, 1d=5, 1w=6, 1mon=7
      （需 evaluate_outputs 注入 period 上下文）
    - SEASON/MIN1..MIN60：跨周期引用锚点且 MIN 族不在 iQuant 运行时函数表
      （TQVERIFY8B 导入实测）→ 显式报错，批次 9 接入
    - WEEK：不在 iQuant 运行时函数表 → 显式报错，星期序号用 WEEKDAY
    """

    @pytest.fixture()
    def dt_df(self):
        # 2026-01-05(周一)…01-09(周五)，1 月第 1 个完整 ISO 周（W01）
        return pl.DataFrame({
            "datetime": [
                datetime(2026, 1, 5), datetime(2026, 1, 6), datetime(2026, 1, 7),
                datetime(2026, 1, 8), datetime(2026, 1, 9),
            ],
            "Open": [1.0] * 5, "High": [1.0] * 5, "Low": [1.0] * 5,
            "Close": [1.0, 2.0, 3.0, 4.0, 5.0],
            "Volume": [1.0] * 5, "Amount": [1.0] * 5,
        })

    def test_week_rejected_not_in_runtime(self, dt_df):
        # WEEK 不在国信 iQuant 运行时函数表（TQVERIFY8 导入实测报未知字符串，
        # 同 DAYOFWEEK 先例），为保持回测/实盘词表一致已移除；星期序号用 WEEKDAY
        with pytest.raises(FormulaEvalError, match="WEEK"):
            evaluate_outputs("X:WEEK;", dt_df)

    def test_season_rejected_cross_period_symbol(self):
        # SEASON 为跨周期季线引用符号（引用函数族，词典：「用于跨周期季线引用」），
        # 非季度函数——内置引擎不支持跨周期引用，显式报错（批次 9 接入）
        df = pl.DataFrame({
            "datetime": [datetime(2026, m, 15) for m in (1, 4, 7, 10, 12)],
            "Open": [1.0] * 5, "High": [1.0] * 5, "Low": [1.0] * 5,
            "Close": [1.0] * 5, "Volume": [1.0] * 5, "Amount": [1.0] * 5,
        })
        with pytest.raises(FormulaEvalError, match="SEASON"):
            evaluate_outputs("X:SEASON;", df)

    def test_weekofyear(self, dt_df):
        out = evaluate_outputs("X:WEEKOFYEAR;", dt_df)
        assert _vals(out, "X") == [2.0] * 5  # 2026-01-05..09 属 ISO 第 2 周

    def test_weekofyear_jan1_sunday_and_sunday_roll(self):
        # TQVERIFY8B N01 实证补充口径：
        # - 1/1 恰为周日（2023-01-01）：第 1 周从 1/2（周一）开始，
        #   1/3（周二）= 第 1 周（旧口径会算第 2 周——1995/2006/2012/2017/2023
        #   五个 1/1 周日年份整年 -1 的根因）
        # - 周日 bar 归入下一周：2026-01-04（周日）与 1/5（周一）同为第 2 周
        df = pl.DataFrame({
            "datetime": [datetime(2023, 1, 3), datetime(2023, 1, 9),
                         datetime(2026, 1, 4), datetime(2026, 1, 5)],
            "Open": [1.0] * 4, "High": [1.0] * 4, "Low": [1.0] * 4,
            "Close": [1.0, 2.0, 3.0, 4.0],
            "Volume": [1.0] * 4, "Amount": [1.0] * 4,
        })
        out = evaluate_outputs("X:WEEKOFYEAR;", df)
        assert _vals(out, "X") == [1.0, 2.0, 2.0, 2.0]

    def test_period(self, dt_df):
        out = evaluate_outputs("P:PERIOD;", dt_df, period="1d")
        assert _vals(out, "P") == [5.0] * 5   # 实证：日线编号=5

    def test_min_symbols_rejected_not_in_runtime(self, dt_df):
        # MIN1..MIN60 为跨周期引用锚点且不在 iQuant 运行时函数表
        # （TQVERIFY8B 导入实测报未知字符串）——显式报错，批次 9 按锚点语义接入
        with pytest.raises(FormulaEvalError, match="MIN5"):
            evaluate_outputs("X:MIN5;", dt_df)

    def test_period_minute(self, dt_df):
        out = evaluate_outputs("P:PERIOD;", dt_df, period="5m")
        assert _vals(out, "P") == [1.0] * 5   # 实证：5 分钟编号=1

    def test_period_requires_context(self, dt_df):
        with pytest.raises(FormulaEvalError, match="PERIOD"):
            evaluate_outputs("P:PERIOD;", dt_df)


class TestBatch8Index:
    """批次 8 大盘引用族：INDEXC/O/H/L/V/A（对应大盘指数 OHLCV）。

    指数数据由调用方（backtest_service）按个股所属市场取指数 K 线并按
    base df 时间轴行对齐后经 index_frames 注入；缺失时显式报错。
    """

    @pytest.fixture()
    def idx_df(self):
        return pl.DataFrame({
            "Open": [10.0, 11.0, 12.0, 13.0, 14.0],
            "High": [11.0, 12.0, 13.0, 14.0, 15.0],
            "Low": [9.0, 10.0, 11.0, 12.0, 13.0],
            "Close": [10.5, 11.5, 12.5, 13.5, 14.5],
            "Volume": [100.0, 200.0, 300.0, 400.0, 500.0],
            "Amount": [1000.0, 2000.0, 3000.0, 4000.0, 5000.0],
        })

    def test_index_ohlcv(self, df, idx_df):
        out = evaluate_outputs(
            "C:INDEXC;O:INDEXO;H:INDEXH;L:INDEXL;V:INDEXV;A:INDEXA;",
            df, index_frames=idx_df)
        assert _vals(out, "C") == [10.5, 11.5, 12.5, 13.5, 14.5]
        assert _vals(out, "O") == [10.0, 11.0, 12.0, 13.0, 14.0]
        assert _vals(out, "H") == [11.0, 12.0, 13.0, 14.0, 15.0]
        assert _vals(out, "L") == [9.0, 10.0, 11.0, 12.0, 13.0]
        assert _vals(out, "V") == [100.0, 200.0, 300.0, 400.0, 500.0]
        assert _vals(out, "A") == [1000.0, 2000.0, 3000.0, 4000.0, 5000.0]

    def test_index_in_expression(self, df, idx_df):
        # 个股相对大盘强弱
        out = evaluate_outputs("X:C/INDEXC;", df, index_frames=idx_df)
        assert _vals(out, "X") == [pytest.approx(v) for v in
                                    (0.095238095, 0.173913043, 0.24, 0.296296296, 0.344827586)]

    def test_index_requires_context(self, df):
        with pytest.raises(FormulaEvalError, match="INDEX"):
            evaluate_outputs("X:INDEXC;", df)


# ---------------------------------------------------------------- 批次 9a 跨周期引用

class TestBatch9CrossPeriod:
    """批次 9a 跨周期引用：FIELD#PERIOD 对齐上级周期序列。

    - 默认 TDX 语义：base bar 落入哪根上级 bar 区间即「所属」，取其整根终值
      （周内含未来引用，与通达信全历史显示一致）
    - no_leak=True（公式级开关，默认关）：所属上级 bar 未完成 → 取上一根已完成 bar
    - 仅支持引用更粗周期；更细周期 1:N 无法对齐，显式报错
    """

    @pytest.fixture()
    def base_dt_df(self) -> pl.DataFrame:
        # 6 根 base：首根早于上级首根（预热不足），其余分布在周 1/2/3
        return pl.DataFrame({
            "datetime": [
                datetime(2026, 1, 2, 10, 0),   # 早于上级首根 1/5
                datetime(2026, 1, 5, 10, 0),   # 周1 区间
                datetime(2026, 1, 6, 10, 0),   # 周1 区间
                datetime(2026, 1, 13, 10, 0),  # 周2 区间
                datetime(2026, 1, 14, 10, 0),  # 周2 区间
                datetime(2026, 1, 20, 10, 0),  # 周3 区间
            ],
            "Open": [1.0] * 6,
            "High": [1.0] * 6,
            "Low": [1.0] * 6,
            "Close": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            "Volume": [10.0] * 6,
            "Amount": [100.0] * 6,
        })

    @pytest.fixture()
    def week_df(self) -> pl.DataFrame:
        return pl.DataFrame({
            "datetime": [
                datetime(2026, 1, 5),
                datetime(2026, 1, 12),
                datetime(2026, 1, 19),
            ],
            "Open": [9.0, 19.0, 29.0],
            "High": [11.0, 21.0, 31.0],
            "Low": [8.0, 18.0, 28.0],
            "Close": [10.0, 20.0, 30.0],
            "Volume": [100.0, 200.0, 300.0],
            "Amount": [1000.0, 2000.0, 3000.0],
        })

    def test_week_default_tdx_semantics(self, base_dt_df, week_df):
        # 所属上级 bar 整根终值：周1 两根→w0，周2 两根→w1，周3→w2，早于首根→null
        out = evaluate_outputs("X:C#WEEK;", base_dt_df, period="1d",
                               period_frames={"1w": week_df})
        assert _vals(out, "X") == [None, 10.0, 10.0, 20.0, 20.0, 30.0]

    def test_week_no_leak(self, base_dt_df, week_df):
        # 无泄露：未完成所属 bar 取上一根已完成；首周无上一根 → null
        out = evaluate_outputs("X:C#WEEK;", base_dt_df, period="1d",
                               period_frames={"1w": week_df}, no_leak=True)
        assert _vals(out, "X") == [None, None, None, 10.0, 10.0, 20.0]

    def test_field_aliases(self, base_dt_df, week_df):
        # 字段别名：HIGH#WEEK 取上级 High 列
        out = evaluate_outputs("X:HIGH#WEEK;", base_dt_df, period="1d",
                               period_frames={"1w": week_df})
        assert _vals(out, "X") == [None, 11.0, 11.0, 21.0, 21.0, 31.0]

    def test_in_expression(self, base_dt_df, week_df):
        # 跨周期序列作为普通序列参与表达式（个股相对周线溢价）
        out = evaluate_outputs("X:C/C#WEEK;", base_dt_df, period="1d",
                               period_frames={"1w": week_df})
        assert _vals(out, "X")[1] == pytest.approx(2.0 / 10.0)
        assert _vals(out, "X")[3] == pytest.approx(4.0 / 20.0)

    def test_requires_period_frames(self, base_dt_df):
        with pytest.raises(FormulaEvalError, match="跨周期"):
            evaluate_outputs("X:C#WEEK;", base_dt_df)

    def test_unknown_suffix(self, base_dt_df, week_df):
        with pytest.raises(FormulaEvalError, match="周期后缀"):
            evaluate_outputs("X:C#YEAR;", base_dt_df, period="1d",
                             period_frames={"1w": week_df})

    def test_missing_higher_data(self, base_dt_df):
        with pytest.raises(FormulaEvalError, match="1w"):
            evaluate_outputs("X:C#WEEK;", base_dt_df, period="1d",
                             period_frames={})

    def test_same_period_identity(self, base_dt_df):
        # 引用自身周期：period_frames 未提供时用喂入 df 恒等对齐（默认语义）
        out = evaluate_outputs("X:C#DAY;", base_dt_df, period="1d")
        assert _vals(out, "X") == _vals(evaluate_outputs("Y:C;", base_dt_df), "Y")

    def test_same_period_no_leak_is_ref1(self, base_dt_df):
        # 无泄露下引用自身周期 = 只能用已完成 bar，即 REF(C,1)
        out = evaluate_outputs("X:C#DAY;", base_dt_df, period="1d", no_leak=True)
        assert _vals(out, "X") == [None, 1.0, 2.0, 3.0, 4.0, 5.0]

    def test_finer_period_rejected(self, base_dt_df):
        # 日线策略引用 5m：更细周期 1:N 无法对齐，显式报错
        m5 = base_dt_df.head(1)
        with pytest.raises(FormulaEvalError, match="更细"):
            evaluate_outputs("X:C#MIN5;", base_dt_df, period="1d",
                             period_frames={"5m": m5})

    def test_hour_key_compat(self, base_dt_df):
        # MIN60 → 周期键兼容 "1h"/"60m" 双写法（klines 实际用 "1h"）
        h1 = pl.DataFrame({
            "datetime": [datetime(2026, 1, 2, 10, 0), datetime(2026, 1, 2, 14, 0)],
            "Open": [1.0, 2.0], "High": [1.0, 2.0], "Low": [1.0, 2.0],
            "Close": [7.0, 8.0], "Volume": [1.0, 1.0], "Amount": [1.0, 1.0],
        })
        out = evaluate_outputs("X:C#MIN60;", base_dt_df, period="30m",
                               period_frames={"1h": h1})
        # 1/2 10:00 → h1[0]；其余（含末根 +∞ 区间）→ h1[1]
        assert _vals(out, "X") == [7.0, 8.0, 8.0, 8.0, 8.0, 8.0]

    def test_week_friday_stamped(self, base_dt_df):
        # TQ 实证：周线时间戳为周五标——日历归算到周一后分桶应与周一标一致
        fri_df = pl.DataFrame({
            "datetime": [
                datetime(2026, 1, 2),   # 周五标（含 1/2 当周，但首根早于部分 base）
                datetime(2026, 1, 9),
                datetime(2026, 1, 16),
                datetime(2026, 1, 23),
            ],
            "Open": [1.0] * 4, "High": [1.0] * 4, "Low": [1.0] * 4,
            "Close": [50.0, 60.0, 70.0, 80.0],
            "Volume": [1.0] * 4, "Amount": [1.0] * 4,
        })
        out = evaluate_outputs("X:C#WEEK;", base_dt_df, period="1d",
                               period_frames={"1w": fri_df})
        # 1/2(周五)→w0=50；1/5、1/6→w1=60；1/13、1/14→w2=70；1/20→w3=80
        assert _vals(out, "X") == [50.0, 60.0, 60.0, 70.0, 70.0, 80.0]

    def test_volume_weekly_accumulates(self, base_dt_df, week_df):
        # TDX 在线实证（TQVERIFY9 L05）：V#WEEK 为周内逐日累计（不含未来），
        # 非上级 bar 终值——周一=当日量、周五=周总量
        out = evaluate_outputs("X:V#WEEK;", base_dt_df, period="1d",
                               period_frames={"1w": week_df})
        # base Volume=[10]*6；1/2 早于 week_df 首根(1/5)→None；
        # 1/5、1/6 同周累计→10、20；1/13、1/14→10、20；1/20→10
        assert _vals(out, "X") == [None, 10.0, 20.0, 10.0, 20.0, 10.0]

    def test_volume_no_leak_prev_total(self, base_dt_df, week_df):
        # V#WEEK 无泄露：未完成周 → 上一根已完成周总量（上级 bar 终值）
        out = evaluate_outputs("X:V#WEEK;", base_dt_df, period="1d",
                               period_frames={"1w": week_df}, no_leak=True)
        # 1/2 早于首根→None；1/5、1/6 首周未完成且无上一根→None；
        # 1/13、1/14→w0 总量 100；1/20→w1 总量 200
        assert _vals(out, "X") == [None, None, None, 100.0, 100.0, 200.0]

    def test_cross_periods_of(self):
        from core.engine.tdx_formula.evaluator import cross_periods_of
        assert cross_periods_of("T:C#WEEK;") == {"1w"}
        assert cross_periods_of("X:REF(C#DAY,1)+C#MONTH;") == {"1d", "1mon"}
        assert cross_periods_of("X:MA(C,5);") == set()
        assert cross_periods_of("{注释里 C#WEEK 不算}") == set()
        # 未知后缀不进集合（取数层容错，求值层报错）
        assert cross_periods_of("X:C#YEAR;") == set()
        # 批次 9b：组合语法 公式名.线名#周期 同样收集上级周期键
        assert cross_periods_of("X:TESTA.A1#WEEK;") == {"1w"}
        assert cross_periods_of("X:TESTA.A1;") == set()


class TestBatch9XRef:
    """批次 9b 跨公式引用：公式名.线名[#周期]。

    - resolver(form, period_key|None) -> {线名: Series}
      （被引公式查找/memo/环检测由接线层负责）
    - 带周期后缀：在上级周期上求值被引公式 → _align_cross 折回 base
      （与 FIELD#PERIOD 同语义）
    - 公式名/线名/被引公式存在性运行时校验，静态检查不报错
    """

    @pytest.fixture()
    def base_dt_df(self) -> pl.DataFrame:
        # 与 TestBatch9CrossPeriod 同款：6 根日线，首根早于上级首根
        return pl.DataFrame({
            "datetime": [
                datetime(2026, 1, 2, 10, 0),
                datetime(2026, 1, 5, 10, 0),
                datetime(2026, 1, 6, 10, 0),
                datetime(2026, 1, 13, 10, 0),
                datetime(2026, 1, 14, 10, 0),
                datetime(2026, 1, 20, 10, 0),
            ],
            "Open": [1.0] * 6,
            "High": [1.0] * 6,
            "Low": [1.0] * 6,
            "Close": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            "Volume": [10.0] * 6,
            "Amount": [100.0] * 6,
        })

    @pytest.fixture()
    def week_df(self) -> pl.DataFrame:
        return pl.DataFrame({
            "datetime": [
                datetime(2026, 1, 5),
                datetime(2026, 1, 12),
                datetime(2026, 1, 19),
            ],
            "Open": [9.0, 19.0, 29.0], "High": [11.0, 21.0, 31.0],
            "Low": [8.0, 18.0, 28.0], "Close": [10.0, 20.0, 30.0],
            "Volume": [100.0, 200.0, 300.0], "Amount": [1000.0, 2000.0, 3000.0],
        })

    @staticmethod
    def _resolver(outs_by_period: dict):
        calls: list = []

        def resolver(form, period_key):
            calls.append((form, period_key))
            return outs_by_period.get(period_key)

        resolver.calls = calls
        return resolver

    def test_xref_identity(self, base_dt_df):
        # 同周期引用：resolver(form, None) 返回的 A1 线原样透传
        resolver = self._resolver({None: {"A1": base_dt_df["Close"]}})
        out = evaluate_outputs("X:TESTA.A1;", base_dt_df, xref_resolver=resolver)
        assert _vals(out, "X") == _vals(evaluate_outputs("Y:C;", base_dt_df), "Y")
        assert resolver.calls == [("TESTA", None)]

    def test_xref_in_expression(self, base_dt_df):
        # 跨公式序列作为普通序列参与表达式
        resolver = self._resolver({None: {"A1": pl.Series("A1", [10.0] * 6)}})
        out = evaluate_outputs("X:TESTA.A1-C;", base_dt_df, xref_resolver=resolver)
        assert _vals(out, "X") == [9.0, 8.0, 7.0, 6.0, 5.0, 4.0]

    def test_xref_line_missing(self, base_dt_df):
        resolver = self._resolver({None: {"B2": base_dt_df["Close"]}})
        with pytest.raises(FormulaEvalError, match="无输出线"):
            evaluate_outputs("X:TESTA.A1;", base_dt_df, xref_resolver=resolver)

    def test_xref_no_resolver(self, base_dt_df):
        with pytest.raises(FormulaEvalError, match="xref_resolver"):
            evaluate_outputs("X:TESTA.A1;", base_dt_df)

    def test_xref_cross_alignment(self, base_dt_df, week_df):
        # TESTA.A1#WEEK：在 1w 上求值 TESTA → 取 A1 线 → 对齐折回日线
        resolver = self._resolver({None: {"A1": base_dt_df["Close"]},
                                   "1w": {"A1": week_df["Close"]}})
        out = evaluate_outputs("X:TESTA.A1#WEEK;", base_dt_df, period="1d",
                               period_frames={"1w": week_df},
                               xref_resolver=resolver)
        # 与 FIELD#WEEK 同语义：早于首根→None，周1→10，周2→20，周3→30
        assert _vals(out, "X") == [None, 10.0, 10.0, 20.0, 20.0, 30.0]
        assert ("TESTA", "1w") in resolver.calls

    def test_xref_cross_no_leak(self, base_dt_df, week_df):
        resolver = self._resolver({None: {"A1": base_dt_df["Close"]},
                                   "1w": {"A1": week_df["Close"]}})
        out = evaluate_outputs("X:TESTA.A1#WEEK;", base_dt_df, period="1d",
                               period_frames={"1w": week_df}, no_leak=True,
                               xref_resolver=resolver)
        assert _vals(out, "X") == [None, None, None, 10.0, 10.0, 20.0]

    def test_xref_cross_unknown_suffix(self, base_dt_df, week_df):
        resolver = self._resolver({"1w": {"A1": week_df["Close"]}})
        with pytest.raises(FormulaEvalError, match="周期后缀"):
            evaluate_outputs("X:TESTA.A1#YEAR;", base_dt_df, period="1d",
                             period_frames={"1w": week_df},
                             xref_resolver=resolver)

    def test_xref_cross_missing_higher_data(self, base_dt_df):
        resolver = self._resolver({None: {"A1": base_dt_df["Close"]}})
        with pytest.raises(FormulaEvalError, match="上级周期数据"):
            evaluate_outputs("X:TESTA.A1#WEEK;", base_dt_df, period="1d",
                             period_frames={}, xref_resolver=resolver)

    def test_xref_names_of(self):
        from core.engine.tdx_formula import xref_names_of
        assert xref_names_of("X:TESTA.A1;") == {"TESTA"}
        assert xref_names_of("X:FOO.B2#WEEK+TESTA.A1;") == {"FOO", "TESTA"}
        assert xref_names_of("X:MA(C,5);") == set()
        assert xref_names_of("X:TESTA.A1;Y:TESTB.C3#MONTH;") == {"TESTA", "TESTB"}

    def test_xref_syntax_check_ok(self):
        # 静态检查不校验公式名/线名（运行时经 resolver 校验）
        from core.engine.tdx_formula import syntax_check
        r = syntax_check("X:TESTA.A1;Y:TESTA.A1#WEEK;")
        assert r["ok"] is True
        assert r["errors"] == []


# ---------------------------------------------------------------- 源码体检（lint）

class TestSourceLint:
    """source_lint 四类检测：不可见字符/中文标点/括号平衡/疑似漏分号。"""

    def test_invisible_chars(self):
        from core.engine.tdx_formula.evaluator import source_lint
        ps = source_lint("X:CLOSE\u3000;\nY:OPEN\u200b;")
        assert any("不可见字符" in p and "全角空格" in p and "第 1 行" in p for p in ps)
        assert any("零宽空格" in p and "第 2 行" in p for p in ps)
        assert source_lint("X:CLOSE;") == []

    def test_cn_punct_in_code(self):
        from core.engine.tdx_formula.evaluator import source_lint
        ps = source_lint("X:CLOSE，OPEN;")
        assert any("中文标点" in p and "，" in p and "第 1 行" in p for p in ps)

    def test_cn_punct_exempt_in_string_and_comment(self):
        from core.engine.tdx_formula.evaluator import source_lint
        # 字符串与 {...} 注释内的中文标点合法
        assert source_lint("X:CLOSE;{注释，中文标点；都在这里}") == []
        assert source_lint("X:CLOSE;'文本，内容';") == []

    def test_paren_balance(self):
        from core.engine.tdx_formula.evaluator import source_lint
        ps = source_lint("X:MA(CLOSE,5;")
        assert any("未闭合" in p and "第 1 行" in p for p in ps)
        ps = source_lint("X:CLOSE);")
        assert any("多余的右括号" in p and "第 1 行" in p for p in ps)
        assert source_lint("X:MA((CLOSE),5);") == []

    def test_missing_semicolon(self):
        from core.engine.tdx_formula.evaluator import source_lint
        ps = source_lint("X:CLOSE\nY:OPEN;")
        assert any("疑似漏分号" in p and "第 1 行" in p for p in ps)

    def test_multiline_statement_no_false_positive(self):
        from core.engine.tdx_formula.evaluator import source_lint
        # 语句合法跨行（下一行不是定义头）不误报
        assert source_lint("X:CLOSE +\n OPEN;") == []
        # 行尾分号后跟新定义不报
        assert source_lint("X:CLOSE;\nY:OPEN;") == []

    def test_missing_semicolon_skips_comment_lines(self):
        from core.engine.tdx_formula.evaluator import source_lint
        # 注释行不算「下一非空行」，但漏分号依旧通过其后的定义头检出
        ps = source_lint("X:CLOSE\n{说明}\nY:OPEN;")
        assert any("疑似漏分号" in p and "第 1 行" in p for p in ps)

    def test_syntax_check_includes_lint(self):
        from core.engine.tdx_formula import syntax_check
        r = syntax_check("X:CLOSE，OPEN;")
        assert r["ok"] is False
        assert any("中文标点" in e for e in r["errors"])
        # 干净源码 lint 零输出
        r = syntax_check("X:CLOSE;")
        assert r["ok"] is True
        assert r["errors"] == []
