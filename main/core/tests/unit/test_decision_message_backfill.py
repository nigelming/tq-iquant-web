from core.services.decision_message_backfill import reformat_message


def test_stop_loss_reformats_long_decimals():
    assert reformat_message(
        "stop_loss", "止损：成本 41.10000000 现价 38.52 亏 6.28%"
    ) == "止损：成本 41.100 现价 38.520 亏 6.28%"


def test_take_profit_reformats_long_decimals():
    assert reformat_message(
        "take_profit", "止盈：成本 13.34000000 现价 17.67 盈 32.46%"
    ) == "止盈：成本 13.340 现价 17.670 盈 32.46%"


def test_trailing_stop_reformats_long_decimals():
    assert reformat_message(
        "trailing_stop", "移动止损：最高 11.21000000 现价 10.76 回撤 4.01%"
    ) == "移动止损：最高 11.210 现价 10.760 回撤 4.01%"


def test_trailing_stop_already_clean_gets_canonical_three_decimals():
    assert reformat_message(
        "trailing_stop", "移动止损：最高 38.45 现价 37.16 回撤 3.36%"
    ) == "移动止损：最高 38.450 现价 37.160 回撤 3.36%"


def test_daily_loss_reformats_money_with_thousands():
    assert reformat_message(
        "daily_loss",
        "日内盈亏 -58259.73150000000000（基准 1341473.33050000000000）亏损 5.83% 触发当日暂停",
    ) == "日内盈亏 -58,259.73（基准 1,341,473.33）亏损 5.83% 触发当日暂停"


def test_order_shrunk_reformats_qty_money_price():
    assert reformat_message(
        "order_shrunk", "资金不足缩量：请求 6700 股，按可用现金 197563.58450000000000 缩至 6600 股 @29.61"
    ) == "资金不足缩量：请求 6,700 股，按可用现金 197,563.58 缩至 6,600 股 @29.610"


def test_unrelated_gates_left_unchanged():
    assert reformat_message(
        "open_already_holding", "OPEN 信号时本票已持仓 1900 股（加仓走 ADD），忽略开仓"
    ) == "OPEN 信号时本票已持仓 1900 股（加仓走 ADD），忽略开仓"
    assert reformat_message(
        "halted_bar_no_price", "停牌/无数据 bar close=0，无法定价/计算下单量"
    ) == "停牌/无数据 bar close=0，无法定价/计算下单量"
    assert reformat_message(
        "halted_buy_strip", "熔断/日内暂停中剥掉 BUY 新开仓 301183.SZ（最终强交易买）"
    ) == "熔断/日内暂停中剥掉 BUY 新开仓 301183.SZ（最终强交易买）"
    assert reformat_message(
        "bridge_rejected", "桥业务拒单: passorder accepted but no order record (dropped)"
    ) == "桥业务拒单: passorder accepted but no order record (dropped)"


def test_parse_failure_returns_original():
    assert reformat_message("stop_loss", "止损：成本 ? 现价 38.52 亏 6.28%") == "止损：成本 ? 现价 38.52 亏 6.28%"
    assert reformat_message("stop_loss", None) is None
