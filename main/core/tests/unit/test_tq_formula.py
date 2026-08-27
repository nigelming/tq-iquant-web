"""TQFormula 单测 — compute_injected 内存注入链路（0010）。

验证 compute_injected 的调用顺序与参数：
  formula_format_data → formula_set_data（每只股票）→ formula_process_mul_zb
  且 process_mul_zb 传 count=-1、不传时间范围（让公式用注入的全部数据）。
全部 mock tq 单例，不连真实通达信。
"""
from unittest.mock import MagicMock, patch

import pytest

from core.tq.formula import TQFormula


@pytest.fixture
def fake_tq():
    """mock core.tq.formula.get_tq 返回的 tq 单例。"""
    tq = MagicMock()
    tq.formula_format_data.return_value = {"600000.SH": [{"Open": 1}]}
    tq.formula_set_data.return_value = {"ErrorId": "0"}
    tq.formula_process_mul_zb.return_value = {
        "ErrorId": "0",
        "600000.SH": {"open_sig": [{"Date": "20260805", "Value": 1.0}]},
    }
    return tq


def _ohlcv_df():
    """构造注入用 OHLCV dict（{字段: DataFrame} 形态，这里用 list 占位）。"""
    return {"Open": [9.0], "High": [9.3], "Low": [9.0], "Close": [9.3],
            "Volume": [10000], "Amount": [93000.0]}


def test_compute_injected_calls_set_data_then_process(fake_tq):
    """compute_injected 应先 format → 每只股票 set_data → 最后 process_mul_zb。"""
    with patch("core.tq.formula.get_tq", return_value=fake_tq), \
         patch("core.tq.formula.get_tdx_lock"):
        formula = TQFormula()
        raw = formula.compute_injected(
            formula_name="MACROSSPRO", ohlcv_df=_ohlcv_df(),
            stocks=["600000.SH"], period="1m",
        )

    # 顺序：format → set_data → process
    fake_tq.formula_format_data.assert_called_once()
    fake_tq.formula_set_data.assert_called_once()
    fake_tq.formula_process_mul_zb.assert_called_once()
    # 返回 process_mul_zb 的结果
    assert raw is not None
    assert raw["ErrorId"] == "0"


def test_compute_injected_process_uses_count_neg1_no_time_range(fake_tq):
    """注入后 process_mul_zb 传 count=-1、start_time/end_time 空（用注入数据）。"""
    with patch("core.tq.formula.get_tq", return_value=fake_tq), \
         patch("core.tq.formula.get_tdx_lock"):
        formula = TQFormula()
        formula.compute_injected(
            formula_name="MACROSSPRO", ohlcv_df=_ohlcv_df(),
            stocks=["600000.SH"], period="5m",
        )

    kwargs = fake_tq.formula_process_mul_zb.call_args.kwargs
    assert kwargs["count"] == -1
    assert kwargs["start_time"] == ""
    assert kwargs["end_time"] == ""
    assert kwargs["stock_list"] == ["600000.SH"]
    assert kwargs["stock_period"] == "5m"
    assert kwargs["formula_name"] == "MACROSSPRO"


def test_compute_injected_set_data_uses_formatted_per_stock(fake_tq):
    """set_data 对每只股票调用，用 format 后的该股票数据 + dividend_type=0 注入。"""
    with patch("core.tq.formula.get_tq", return_value=fake_tq), \
         patch("core.tq.formula.get_tdx_lock"):
        formula = TQFormula()
        formula.compute_injected(
            formula_name="MACROSSPRO", ohlcv_df=_ohlcv_df(),
            stocks=["600000.SH"], period="1m",
        )

    kwargs = fake_tq.formula_set_data.call_args.kwargs
    assert kwargs["stock_code"] == "600000.SH"
    assert kwargs["stock_period"] == "1m"
    assert kwargs["dividend_type"] == 0
    # count 传 formatted 该股票数据长度
    assert kwargs["count"] == len(fake_tq.formula_format_data.return_value["600000.SH"])


def test_compute_injected_returns_none_on_set_data_error(fake_tq):
    """某股票 set_data 失败（ErrorId != 0）→ 返回 None，不调 process。"""
    fake_tq.formula_set_data.return_value = {"ErrorId": "1", "Error": "inject fail"}
    with patch("core.tq.formula.get_tq", return_value=fake_tq), \
         patch("core.tq.formula.get_tdx_lock"):
        formula = TQFormula()
        raw = formula.compute_injected(
            formula_name="MACROSSPRO", ohlcv_df=_ohlcv_df(),
            stocks=["600000.SH"], period="1m",
        )

    assert raw is None
    fake_tq.formula_process_mul_zb.assert_not_called()


def test_compute_injected_multi_stocks_all_set_before_process(fake_tq):
    """多股票：每只都 set_data 成功后才 process；任一失败即返回 None。"""
    fake_tq.formula_format_data.return_value = {
        "600000.SH": [{"Open": 1}],
        "000001.SZ": [{"Open": 2}],
    }
    with patch("core.tq.formula.get_tq", return_value=fake_tq), \
         patch("core.tq.formula.get_tdx_lock"):
        formula = TQFormula()
        formula.compute_injected(
            formula_name="MACROSSPRO", ohlcv_df=_ohlcv_df(),
            stocks=["600000.SH", "000001.SZ"], period="1m",
        )

    assert fake_tq.formula_set_data.call_count == 2
    # process 的 stock_list 含全部
    kwargs = fake_tq.formula_process_mul_zb.call_args.kwargs
    assert kwargs["stock_list"] == ["600000.SH", "000001.SZ"]


def test_compute_injected_logs_timing_debug(fake_tq, caplog):
    """compute_injected 打 DEBUG 分段耗时日志（fmt/set/proc/total），供午后变慢节拍诊断。

    逐 code 调用量大，默认 DEBUG 不刷屏；需要下钻单只股票 tqcenter 各阶段耗时（定位
    是 set_data 累积还是 process count=-1 全量重算慢）时开 core.tq.formula=DEBUG。
    """
    import logging
    with patch("core.tq.formula.get_tq", return_value=fake_tq), \
         patch("core.tq.formula.get_tdx_lock"), \
         caplog.at_level(logging.DEBUG, logger="core.tq.formula"):
        formula = TQFormula()
        formula.compute_injected(
            formula_name="MACROSSPRO", ohlcv_df=_ohlcv_df(),
            stocks=["600000.SH"], period="1m",
        )

    timed = [r for r in caplog.records if "compute_injected" in r.message]
    assert timed, "expected DEBUG timing log from compute_injected"
    msg = timed[-1].message
    assert "MACROSSPRO" in msg and "period=1m" in msg
    for token in ("fmt=", "set=", "proc=", "total="):
        assert token in msg


def _single_col_ohlcv(code):
    """构造单列 ohlcv_df（{字段: 单列 DataFrame，列=code}），模拟 _bars_to_formula_df 输出。"""
    import pandas as pd
    from datetime import datetime
    idx = pd.DatetimeIndex([datetime(2026, 8, 5, 10, i) for i in range(3)])
    return {
        "Open": pd.DataFrame({code: [9.0] * 3}, index=idx),
        "High": pd.DataFrame({code: [9.3] * 3}, index=idx),
        "Low": pd.DataFrame({code: [9.0] * 3}, index=idx),
        "Close": pd.DataFrame({code: [9.3] * 3}, index=idx),
        "Volume": pd.DataFrame({code: [10000] * 3}, index=idx),
        "Amount": pd.DataFrame({code: [93000.0] * 3}, index=idx),
    }


def test_compute_injected_batch_formats_per_stock_but_processes_once(fake_tq):
    """批量注入：逐只 format_data+set_data（各喂自己的 bars），process_mul_zb 只调 1 次。

    DLL 往返 N(set)+1(process) 而非逐只的 2N；每只数据独立、不做跨股票时间轴对齐
    （无矩形 NaN 风险）。这是 1m 全池 111 只/分钟的性能路径。
    """
    codes = ["600000.SH", "000001.SZ"]

    def fmt(df):
        code = df["Open"].columns[0]
        return {code: [{"Open": 1, "Close": 9.3} for _ in range(3)]}

    fake_tq.formula_format_data.side_effect = fmt
    fake_tq.formula_set_data.return_value = {"ErrorId": "0"}
    fake_tq.formula_process_mul_zb.return_value = {
        "ErrorId": "0",
        **{c: {"open_sig": [{"Date": "202608051002", "Value": 1.0}]} for c in codes},
    }
    ohlcv_by_code = {c: _single_col_ohlcv(c) for c in codes}
    with patch("core.tq.formula.get_tq", return_value=fake_tq), \
         patch("core.tq.formula.get_tdx_lock"):
        formula = TQFormula()
        raw = formula.compute_injected_batch(
            formula_name="MACROSSPRO", ohlcv_by_code=ohlcv_by_code, period="1m",
        )

    assert raw is not None and raw["ErrorId"] == "0"
    assert fake_tq.formula_format_data.call_count == 2   # 逐只 format
    assert fake_tq.formula_set_data.call_count == 2       # 逐只 set
    assert fake_tq.formula_process_mul_zb.call_count == 1  # process 合并 1 次
    kwargs = fake_tq.formula_process_mul_zb.call_args.kwargs
    assert kwargs["stock_list"] == codes
    assert kwargs["count"] == -1
    assert kwargs["start_time"] == "" and kwargs["end_time"] == ""
    # set_data 仍是内存注入（dividend_type=0）、各自 period
    for c in fake_tq.formula_set_data.call_args_list:
        assert c.kwargs["dividend_type"] == 0
        assert c.kwargs["stock_period"] == "1m"


def test_compute_injected_batch_skips_failed_set_data_stock(fake_tq):
    """某只 set_data 失败 → 剔除出 stock_list，process 仍对成功 code 调用，raw 不含失败 code。"""
    codes = ["600000.SH", "000001.SZ"]

    def fmt(df):
        code = df["Open"].columns[0]
        return {code: [{"Open": 1} for _ in range(3)]}

    fake_tq.formula_format_data.side_effect = fmt
    fake_tq.formula_set_data.side_effect = [
        {"ErrorId": "0"}, {"ErrorId": "1", "Error": "inject fail"},
    ]
    fake_tq.formula_process_mul_zb.return_value = {
        "ErrorId": "0", "600000.SH": {"open_sig": [{"Value": 1.0}]},
    }
    ohlcv_by_code = {c: _single_col_ohlcv(c) for c in codes}
    with patch("core.tq.formula.get_tq", return_value=fake_tq), \
         patch("core.tq.formula.get_tdx_lock"):
        formula = TQFormula()
        raw = formula.compute_injected_batch("MACROSSPRO", ohlcv_by_code, "1m")

    kwargs = fake_tq.formula_process_mul_zb.call_args.kwargs
    assert kwargs["stock_list"] == ["600000.SH"]  # 失败那只被剔除
    assert "000001.SZ" not in raw


def test_compute_injected_batch_empty_returns_none(fake_tq):
    """空输入 / 全部失败 → None，不调 process。"""
    with patch("core.tq.formula.get_tq", return_value=fake_tq), \
         patch("core.tq.formula.get_tdx_lock"):
        formula = TQFormula()
        assert formula.compute_injected_batch("MACROSSPRO", {}, "1m") is None
    fake_tq.formula_process_mul_zb.assert_not_called()


@pytest.mark.parametrize("period", ["1m", "5m", "15m", "30m", "1h", "1d"])
def test_compute_injected_passes_period_through(fake_tq, period):
    """compute_injected 应把 period 原样透传给 set_data 与 process_mul_zb 的 stock_period。

    6 个实盘可配周期（VALID_PERIODS，open-questions Q4）逐一断言，防止某周期在
    注入链路被错误改写/归一化。真机注入等价性已由 verify_formula_inject.py 验过
    （注入=自取全等），此单测验的是代码层 period 字符串透传无误。
    """
    with patch("core.tq.formula.get_tq", return_value=fake_tq), \
         patch("core.tq.formula.get_tdx_lock"):
        formula = TQFormula()
        formula.compute_injected(
            formula_name="MACROSSPRO", ohlcv_df=_ohlcv_df(),
            stocks=["600000.SH"], period=period,
        )

    set_kwargs = fake_tq.formula_set_data.call_args.kwargs
    proc_kwargs = fake_tq.formula_process_mul_zb.call_args.kwargs
    assert set_kwargs["stock_period"] == period
    assert proc_kwargs["stock_period"] == period
