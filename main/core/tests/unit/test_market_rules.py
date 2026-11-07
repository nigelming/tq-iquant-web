"""market_rules（涨跌停幅度）共享模块测试。

_limit_ratio_for 原属 backtest_engine，1.3.1 迁到 core.engine.market_rules 供
实盘 TdxTqDispatcher 涨跌停前置校验共用；backtest_engine 保留 re-export，
既有 import 路径不破。
"""
from decimal import Decimal

from core.engine.backtest_engine import _limit_ratio_for as from_backtest
from core.engine.market_rules import _limit_ratio_for, tick_size


def test_main_board_ten_percent():
    assert _limit_ratio_for("000001.SZ") == Decimal("0.10")
    assert _limit_ratio_for("600000.SH") == Decimal("0.10")


def test_gem_and_star_twenty_percent():
    assert _limit_ratio_for("300750.SZ") == Decimal("0.20")
    assert _limit_ratio_for("301236.SZ") == Decimal("0.20")
    assert _limit_ratio_for("688981.SH") == Decimal("0.20")


def test_bse_thirty_percent():
    assert _limit_ratio_for("430047.BJ") == Decimal("0.30")
    assert _limit_ratio_for("830799.BJ") == Decimal("0.30")
    # 92xxxx 为北交所新股段，同 30%（原实现漏判，按 9 开头误落 10%）
    assert _limit_ratio_for("920718.BJ") == Decimal("0.30")
    assert _limit_ratio_for("920000") == Decimal("0.30")


def test_plain_code_without_suffix():
    assert _limit_ratio_for("600000") == Decimal("0.10")
    assert _limit_ratio_for("300750") == Decimal("0.20")


def test_backtest_engine_reexport_kept():
    """迁移后 backtest_engine 既有导入路径不破（re-export 同一函数）。"""
    assert from_backtest is _limit_ratio_for


def test_tick_size_etf_is_one_milli():
    """ETF（沪 5 开头 / 深 1 开头）最小价差 0.001。"""
    assert tick_size("510300.SH") == Decimal("0.001")
    assert tick_size("588000.SH") == Decimal("0.001")
    assert tick_size("159915.SZ") == Decimal("0.001")
    assert tick_size("159888.SZ") == Decimal("0.001")


def test_tick_size_stock_is_one_cent():
    """股票（沪 6 / 深 0、3 / 北 4、8）最小价差 0.01。"""
    assert tick_size("600000.SH") == Decimal("0.01")
    assert tick_size("000001.SZ") == Decimal("0.01")
    assert tick_size("300750.SZ") == Decimal("0.01")
    assert tick_size("688981.SH") == Decimal("0.01")
    assert tick_size("830799.BJ") == Decimal("0.01")


def test_tick_size_plain_code_without_suffix():
    assert tick_size("510300") == Decimal("0.001")
    assert tick_size("600000") == Decimal("0.01")
