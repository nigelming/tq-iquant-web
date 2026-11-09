"""TR-19 单文件离线 HTML 报告：自包含/无外链/数字同源（FR-21/FR-22）。"""
import json
import re
import sys

import pytest

from core.services.html_report import (
    build_html_report,
    ALLOWED_EXTERNAL_URLS,
)
import core.services.html_report as _hr


def _detail(**overrides):
    detail = {
        "record": {
            "id": 11, "name": "17",
            "start_date": "2024-01-01", "end_date": "2026-09-23",
            "status": "completed",
        },
        "snapshots": [
            {"snap_date": "2024-01-02", "total_value": 1_000_000.0,
             "benchmark_value": 1000.0},
            {"snap_date": "2024-01-03", "total_value": 1_010_000.0,
             "benchmark_value": 1005.0},
            {"snap_date": "2024-01-04", "total_value": 998_000.0,
             "benchmark_value": 996.0},
        ],
        "evaluations": {
            "total_return": 0.3521,
            "annual_return": 0.1234,
            "max_drawdown": -0.0876,
            "sharpe_ratio": 1.23,
            "volatility": 0.21,
            "calmar_ratio": 0.77,
        },
        "crisis_alpha": {
            "status": "completed", "error": None,
            "benchmark_code": "000300.SH", "benchmark_name": "沪深300",
            "data": {
                "alpha_rating": "较强",
                "n_weeks": 12,
                "quadrant": {
                    "n_crisis": 5, "hit_rate": 0.8, "up_rate": 0.8,
                    "alpha_ratio": 0.6253, "follow_ratio": 0.4,
                    "odds": 2.1,
                },
                "multi_bench": [
                    {"name": "沪深300", "alpha_ratio": 0.6253,
                     "up_rate": 0.8, "n_down": 5, "is_main": True},
                ],
                "deep_dd_events": [],
            },
        },
        "market_regime": {
            "status": "completed", "error": None,
            "benchmark_code": "000300.SH", "benchmark_name": "沪深300",
            "data": {
                "meta": {"strategy_name": "热点精选", "benchmark_name": "沪深300",
                         "n_weeks_labeled": 12, "n_weeks_total": 12,
                         "state_week_counts": {}},
                "regime_table": {
                    "bull": {"label_cn": "趋势上涨", "weeks": 4, "ratio": 0.33,
                             "strategy_return": 0.08, "benchmark_return": 0.06,
                             "excess_return": 0.02, "win_rate": 0.7,
                             "max_drawdown": -0.02, "turnover": 1.2,
                             "cost_ratio": 0.003},
                },
                "opportunity": {"n_bull_weeks": 4, "up_capture": 0.9,
                                "miss_ratio": 0.1, "big_up_tests": [],
                                "cats": {}},
                "rebound": {"n_segments": 1, "avg_elasticity": 1.3,
                            "segments": []},
                "neutral": {"n_range_weeks": 3, "range_ratio": 0.01,
                            "cumulative_return": 0.02, "annualized_return": 0.04,
                            "win_rate": 0.6, "n_range_days": 15,
                            "monthly_trades": 2.0, "annual_turnover": 3.2,
                            "cost_gross_ratio": 0.1, "whipsaw_count": 1,
                            "whipsaw_days": 5,
                            "avg_trade_pnl": {"trend": None, "trend_count": 0,
                                              "range": None, "range_count": 0}},
                "style": {"available_codes": [], "skipped": [], "weeks": [],
                          "matrix": {}},
                "dispersion": {"available_codes": [], "skipped": [],
                               "series": [], "buckets": {}},
                "exposure": {"available_codes": [], "available_names": [],
                             "skipped": [], "dates": [], "corr": {}, "beta": {},
                             "window": 52},
                "industry_attribution": {
                    "status": "needs_rerun",
                    "taxonomy": "通达信二级行业(881)",
                    "reason": "该记录回测时未保存持仓明细，重新回测后可用",
                },
                "robustness": {
                    "status": "insufficient", "n_days": 12,
                    "error": "策略与基准共同交易日不足30天（12），稳健性无法分析",
                },
            },
        },
    }
    detail.update(overrides)
    return detail


def _robustness_payload():
    """稳健性检验 data 区（年度/3年滚动/集中度/样本内外）。"""
    return {
        "total_return": 10.0847, "benchmark_return": 0.8593,
        "years_positive": "13/13",
        "annual": [{"year": "2014", "return": 0.814, "max_drawdown": -0.079},
                   {"year": "2026", "return": 0.048, "max_drawdown": -0.005}],
        "rolling_3y": [{"start": "2014", "end": "2016", "cagr": 0.532,
                        "max_drawdown": -0.079}],
        "concentration": {
            "total_realized_pnl": 1000000,
            "gross_profit": 1400000, "gross_loss": 400000,
            "top": [{"code": "000408.SZ", "pnl": 338874, "share": 0.3389,
                     "share_gross": 0.2421, "share_net": 0.3389}],
            "top20_share": 0.42, "top20_share_gross": 0.30,
            "unrealized": {
                "total_pnl": 5845.01, "cost": 200404.99, "market_value": 206250,
                "rows": [{"code": "002230.SZ", "quantity": 5500,
                          "cost": 200404.99, "market_value": 206250,
                          "pnl": 5845.01, "share_net": 0.0058}],
            },
            "profitable_stocks": 195, "total_stocks": 262,
            "lots": {
                "count": 314, "winners": 177,
                "top": [{"rank": 1, "code": "002843.SZ", "buy_date": "2026-02-10",
                         "sell_date": "2026-03-03", "quantity": 10900,
                         "pnl": 115144.07, "share_gross": 0.0401,
                         "share_net": 0.0513}],
                "cumulative": [{"n": 5, "pct_of_lots": 0.0159, "pnl": 495833,
                                "share_gross": 0.1728, "share_net": 0.2209}],
                "hhi": 0.0145, "effective_sources": 69.0,
            },
        },
        "oos": {
            "split_year": "2023",
            "in_sample": {"start": "2014-01-02", "end": "2023-12-29",
                          "return": 8.145, "cagr": 0.248,
                          "max_drawdown": -0.079, "benchmark_return": 0.478},
            "out_of_sample": {"start": "2024-01-02", "end": "2026-10-09",
                              "return": 0.212, "cagr": 0.066,
                              "max_drawdown": -0.008, "benchmark_return": 0.275},
        },
    }


def test_tr19_1_html_self_contained_with_inline_echarts():
    html = build_html_report(_detail())
    assert html.lstrip().lower().startswith("<!doctype html>")
    # 内联 ECharts 运行时（压缩库体积约 1MB，内联后 HTML 同量级）
    assert len(html) > 500_000
    assert "exports.echarts" in html or "echarts" in html
    # 单文件：无 <script src>/<link href> 外链标签
    assert not re.search(r"<script[^>]+\ssrc=", html, re.I)
    assert not re.search(r"<link[^>]+href=", html, re.I)


def test_tr19_1_no_external_links_outside_whitelist():
    html = build_html_report(_detail())
    urls = set(re.findall(r"https?://[^\s\"'<>)]+", html))
    allowed = set(ALLOWED_EXTERNAL_URLS)
    # 白名单本身是 vendored ECharts 内自带的 6 个 URL
    assert allowed.issuperset({
        "http://www.apache.org/licenses/LICENSE-2.0",
        "http://www.w3.org/2000/svg",
        "https://github.com/ecomfe/zrender/blob/master/LICENSE",
    })
    illegal = [u for u in urls if not any(
        u == a or u.startswith(a) for a in allowed)]
    assert illegal == []


def test_tr19_2_core_numbers_match_detail_payload():
    html = build_html_report(_detail())
    # 与详情接口 evaluations/crisis payload 同值（FR-22，禁止手写猜测）
    assert "35.21%" in html   # 总收益
    assert "12.34%" in html   # 年化
    assert "8.76%" in html    # 最大回撤（取绝对值展示）
    assert "62.53%" in html   # 危机Alpha占比
    assert "较强" in html     # 危机评级
    assert "热点精选" in html


def test_report_contains_all_modules_and_gate_states():
    html = build_html_report(_detail())
    # 老记录门禁与稳健性不足原样呈现
    assert "该记录回测时未保存持仓明细，重新回测后可用" in html
    assert "策略与基准共同交易日不足30天（12），稳健性无法分析" in html
    # 四态表行
    assert "趋势上涨" in html


def test_uncomputed_modules_show_empty_state():
    detail = _detail(crisis_alpha=None, market_regime=None)
    html = build_html_report(detail)
    assert "本次回测未计算危机 Alpha 专项" in html
    assert "本次回测未计算市场状态分析" in html


def test_robustness_ok_renders_tables_and_chart():
    detail = _detail()
    detail["market_regime"]["data"]["robustness"] = {
        "status": "ok", "n_days": 600,
        "start_date": "2024-01-02", "end_date": "2026-06-30",
        "half_year_matrix": [
            {"period": "2024H1", "start_date": "2024-01-02",
             "end_date": "2024-06-28", "strategy_return": 0.08,
             "benchmark_return": 0.05, "excess_return": 0.03,
             "max_drawdown": 0.04, "sharpe": 1.2}],
        "rolling": {"weekly_points": 130, "windows": [
            {"window": 26, "n_samples": 80,
             "returns": {"median": 0.06, "min": -0.08, "max": 0.15,
                         "negative_ratio": 0.2},
             "sharpe": {"median": 0.8, "min": -0.5, "max": 1.6,
                        "negative_ratio": 0.25},
             "histogram": [{"label": "0%~5%", "count": 30}]},
        ]},
        "longest_loss_window": {"start_date": "2024-09-10",
                                "trough_date": "2024-10-18",
                                "end_date": "2024-12-05", "days": 86,
                                "drawdown": 0.09},
        "benchmark_sensitivity": [
            {"code": "000300.SH", "name": "沪深300", "is_primary": True,
             "available": True, "n_days": 600, "strategy_total_return": 0.25,
             "benchmark_total_return": 0.12, "beta": 0.72,
             "jensen_alpha": 0.06, "excess_annual_return": 0.05,
             "tracking_error": 0.08, "information_ratio": 0.62,
             "up_capture_ratio": 0.8, "down_capture_ratio": 0.5,
             "excess_max_drawdown": 0.07}],
    }
    html = build_html_report(detail)
    assert "2024H1" in html
    assert "86 天" in html
    assert "基准替换敏感性" in html
    # 图表 JSON 中含稳健性直方图容器 id
    assert "chart-rb" in html


def test_robustness_check_section_renders():
    """离线报告含「稳健性检验」：年度/3年滚动/集中度/样本内外表。"""
    detail = _detail()
    detail["robustness"] = {
        "status": "completed", "computed_at": "2026-10-12T10:00:00",
        "data": _robustness_payload(),
    }
    html = build_html_report(detail)
    for token in ["稳健性检验", "年度收益与年内最大回撤", "3年滚动窗口",
                  "盈利贡献分布（已实现 / 未实现 / 单笔）", "样本内 / 样本外对比",
                  "000408.SZ", "断点年份", "13/13", "2024 ~ 2026"]:
        assert token in html, f"报告缺少 {token!r}"


def test_robustness_concentration_new_blocks_render():
    """3.3 节新版：未实现单列 + 单笔贡献分布（累计表 + Top 明细 + HHI）。"""
    detail = _detail()
    detail["robustness"] = {"status": "completed", "data": _robustness_payload()}
    html = build_html_report(detail)
    for token in ["已实现净利 1,000,000.00", "毛利 1,400,000.00",
                  "前20只占毛利 30.00%", "占净利 42.00%",
                  "期末未平仓（未实现盈亏单列，不计入已实现净利）",
                  "FIFO 平仓批次 314 笔", "HHI 0.0145", "69.0 笔",
                  "前 5 笔", "2026-02-10 → 2026-03-03"]:
        assert token in html, f"报告缺少 {token!r}"
    # 002230.SZ 必须出现在未实现表里（旧口径会把它当负贡献进个股 Top 表）
    assert "002230.SZ" in html


def test_persistence_check_section_renders():
    """离线报告含「持续性检验」：判定横幅 + 分年度指标/超额矩阵 + 滚动年化。"""
    detail = _detail()
    detail["persistence"] = {
        "status": "completed", "computed_at": "2026-10-21T10:00:00",
        "data": {
            "years": [
                {"year": 2024, "is_partial": False, "nav_return": 0.6, "max_drawdown": 0.08,
                 "sharpe": 2.5, "calmar": 7.0, "win_rate": 0.6, "profit_factor": 1.9,
                 "turnover": 22.5, "cost_ratio": 0.001,
                 "excess": {"000300.SH": 0.30}, "bench_returns": {"000300.SH": 0.30}},
                {"year": 2025, "is_partial": False, "nav_return": 0.3, "max_drawdown": 0.2,
                 "sharpe": 1.0, "calmar": 1.5, "win_rate": 0.45, "profit_factor": 1.2,
                 "turnover": 34.7, "cost_ratio": 0.002,
                 "excess": {"000300.SH": 0.05}, "bench_returns": {"000300.SH": 0.25}},
            ],
            "rolling": {"dates": ["2024-06-01", "2025-06-01"],
                        "strat_6m": [0.12, 0.08], "strat_12m": [0.10, 0.06],
                        "bench_12m": [0.06, 0.05]},
            "benchmarks": [{"code": "000300.SH", "name": "沪深300", "is_main": True, "available": True}],
            "verdict": {
                "level": "alpha_decay", "reasons": ["超额收敛", "风险恶化", "结构退化"],
                "summary": "疑似阿尔法衰减", "caveat": "结论供参考，非自动判定",
                "signals": [
                    {"key": "excess", "label": "超额 vs 沪深300",
                     "detail": "2024 +30.00% → 2025 +5.00%", "status": "bad", "word": "收敛"},
                    {"key": "risk", "label": "风险指标",
                     "detail": "夏普 2.50→1.00 · 卡玛 7.00→1.50 · 回撤 8.0%→20.0%",
                     "status": "bad", "word": "恶化"},
                    {"key": "struct", "label": "交易结构",
                     "detail": "胜率 60.0%→45.0% · 盈亏比 1.90→1.20 · 换手 22.50→34.70×",
                     "status": "bad", "word": "退化"},
                ],
                "window_years": [2024, 2025],
            },
        },
    }
    html = build_html_report(detail)
    for token in ["持续性检验", "疑似阿尔法衰减", "分年度指标矩阵", "分年度超额收益",
                  "滚动年化收益", "结论供参考，非自动判定", "沪深300", "34.7", "chart-p-rolling",
                  "判定信号", "判定窗口", "夏普 2.50→1.00", "收敛", "退化"]:
        assert token in html, f"报告缺少 {token!r}"


def test_new_sections_returns_buckets_decisions_trades_strategy_eval():
    """收益分布/风控统计/交易明细/策略评估四区块在新版离线报告完整渲染。"""
    detail = _detail()
    detail["returns_buckets"] = {
        "monthly_matrix": {
            "years": [2024, 2025], "months": ["1月", "2月"],
            "matrix": [[1.5, -0.8], [2.0, 0.6]],
            "text": [["+1.50%", "-0.80%"], ["+2.00%", "+0.60%"]],
        },
        "yearly": [
            # 日/月收益样本均 ≥5（样本 <5 无法构成箱体，整年剔除——与前端同口径）
            {"year": 2024, "daily_returns": [0.01, -0.02, 0.03, 0.005, -0.01, 0.02],
             "monthly_returns": [0.05, -0.03, 0.02, 0.01, -0.01], "annual_return": 0.25},
            {"year": 2025, "daily_returns": [0.02, 0.01, -0.01, 0.03, -0.02],
             "monthly_returns": [0.03, 0.04, -0.02, 0.01, 0.005], "annual_return": 0.08},
        ],
    }
    detail["decision_summary"] = [{
        "gate": "stop_loss", "layer": "strategy_risk", "action": "trigger",
        "param_name": "stop_loss_ratio", "param_value": 0.05,
        "count": 3, "stock_count": 2,
        "first_bar_time": "2024-01-02T09:30:00", "last_bar_time": "2024-01-05T09:30:00",
    }]
    detail["decisions"] = [{
        "gate": "stop_loss", "param_name": "stop_loss_ratio",
        "bar_time": "2024-01-02T09:30:00", "strategy_id": 1,
        "stock_code": "000001.SZ", "param_value": 0.05, "actual_value": 0.062,
        "message": "止损：成本 10.000 现价 9.380 亏 6.20%",
    }]
    detail["trades"] = [
        {"bar_time": "2024-01-02T09:30:00", "strategy_id": 1, "strategy_name": "s1",
         "stock_code": "000001.SZ", "trade_type": "BUY", "price": 10.0,
         "quantity": 1000, "amount": 10000.0, "commission": 5.0, "stamp_duty": 0.0},
        {"bar_time": "2024-01-05T09:30:00", "strategy_id": 1, "strategy_name": "s1",
         "stock_code": "000001.SZ", "trade_type": "SELL", "price": 11.0,
         "quantity": 1000, "amount": 11000.0, "commission": 5.0, "stamp_duty": 5.5},
    ]
    detail["strategy_evaluations"] = [{
        "strategy_id": 1, "strategy_name": "s1",
        "total_return": 0.3, "annual_return": 0.12, "max_drawdown": -0.06,
        "sharpe_ratio": 1.1,
    }]
    html = build_html_report(detail)
    for token in ["收益分布", "月度收益热力图", "年度日收益箱线图", "各年年收益柱状图",
                  "风控与拦截统计", "止损", "交易明细", "000001.SZ",
                  "策略绩效评估", "组合整体指标", "策略 #1：s1", "买入"]:
        assert token in html, f"报告缺少 {token!r}"
    assert "chart-return-heat" in html
    assert "chart-box-daily" in html
    assert "chart-return-annual" in html


def test_market_regime_window_disclosure():
    """五/六/七/七-B 披露实际统计窗口；短于回测区间时写明被指数历史截断。

    背景：_exposure_block 对全体指数求交集，科创50(000688.SH) 仅 2019-12-31 起，
    会把窗口整体截断（如 651 周回测只剩 343 周）而不告知用户。
    """
    detail = _detail()
    data = detail["market_regime"]["data"]
    data["meta"]["n_weeks_labeled"] = 651
    data["style"]["weeks"] = [{"date": "2020-01-10"}, {"date": "2020-01-17"}]
    data["dispersion"]["series"] = [
        {"date": "2020-01-10", "dispersion": 0.02},
        {"date": "2020-01-17", "dispersion": 0.03},
    ]
    data["exposure"].update({
        "available_codes": ["000300.SH"], "available_names": ["沪深300"],
        "dates": ["2020-01-10", "2020-01-17"],
        "corr": {"000300.SH": [0.4, 0.5]}, "beta": {"000300.SH": [0.3, 0.35]},
    })
    # 七-B 覆盖全区间（651 周）→ 不应被标为截断
    full_dates = ["2014-01-10"] + [f"2014-01-{17 + i}" for i in range(649)] + ["2026-09-18"]
    assert len(full_dates) == 651
    data["quadrant_exposure"] = {
        "available_codes": ["399372.SZ"], "available_names": ["大盘成长"],
        "skipped": [], "dates": full_dates,
        "corr": {"399372.SZ": [0.4] * 651}, "beta": {"399372.SZ": [0.3] * 651},
        "window": 52,
    }
    html = build_html_report(detail)
    assert "实际统计窗口 2020-01-10 ~ 2020-01-17（2 周）" in html
    assert "短于回测区间 651 周" in html
    # 七-B 全区间窗口只报窗口，不报截断
    assert "实际统计窗口 2014-01-10 ~ 2026-09-18（651 周）" in html
    assert "实际统计窗口 2014-01-10 ~ 2026-09-18（651 周）；短于" not in html


def test_appendix_raw_json_removed():
    """附录原始 JSON 已移除（1.8M 字符噪声不再出现在报告中）。"""
    html = build_html_report(_detail())
    assert "附录 · 原始数据 JSON" not in html
    assert 'class="json-dump"' not in html


def _rb():
    return {
        "monthly_matrix": {
            "years": [2024, 2025], "months": ["1月", "2月"],
            "matrix": [[1.5, -0.8], [2.0, 0.6]],
            "text": [["+1.50%", "-0.80%"], ["+2.00%", "+0.60%"]],
        },
        "yearly": [
            {"year": 2024, "daily_returns": [0.01, -0.02, 0.03, 0.005, -0.01, 0.02],
             "monthly_returns": [0.05, -0.03, 0.02, 0.01, -0.01], "annual_return": 0.25},
            {"year": 2025, "daily_returns": [0.02, 0.01, -0.01, 0.03, -0.02],
             "monthly_returns": [0.03, 0.04, -0.02, 0.01, 0.005], "annual_return": 0.08},
        ],
    }


def test_heatmap_labels_show_values_on_blocks():
    """月度收益热力图条块上常驻显示数值（带 % 符号）。

    ECharts 数值模板在本报告内联运行时下不解析，故每个数据项自带字面量 formatter；
    同时深浅底色对应白/深灰字，保证可读。
    """
    out = _hr._monthly_heat_chart(_rb())
    assert out is not None
    _, opt = out
    series = opt["series"][0]
    assert series["label"]["show"] is True
    items = series["data"]
    assert items
    for it in items:
        assert "%" in it["label"]["formatter"]
        assert it["label"]["color"] in ("#ffffff", "#111827")
        # value 必须保持 [x, y, 数值] 三维：多加一维文本会让 visualMap 取错维度而不着色
        assert len(it["value"]) == 3
    # visualMap 显式取数值维 index=2，tooltip 直接引用同一维
    assert opt["visualMap"]["dimension"] == 2
    assert "{@[2]}" in opt["tooltip"]["formatter"]


def test_box_chart_has_zero_axis_and_mean_markers():
    """箱线图含零轴虚线、金色菱形均值点、中位数富 tooltip。"""
    out = _hr._box_chart(_rb(), "daily_returns", "日")
    assert out is not None
    _, opt = out
    series_types = [s["type"] for s in opt["series"]]
    assert "boxplot" in series_types
    assert "scatter" in series_types  # 均值菱形
    markline = next(s for s in opt["series"] if s.get("markLine"))
    assert markline["markLine"]["data"][0] == {"yAxis": 0}
    assert "{c3}" in opt["tooltip"]["formatter"]  # 中位数


def test_box_stats_table_renders_per_year():
    """箱线图下方常驻统计表：每年一行，含均值/中位/Q1/Q3/极值/正值占比。"""
    table = _hr._box_stats_table(_rb(), "daily_returns")
    assert table
    assert "样本数" in table
    assert "正值占比" in table


def test_signal_preflight_warnings_render_at_top():
    """record.warnings 非空 → 报告顶部渲染信号预检警告条及每条警告（转义）。"""
    detail = _detail()
    detail["record"]["warnings"] = [
        '策略「s1」(1d) 的开仓信号「买」（触发值=2）在回测区间内从未触发。',
        '<img src=x onerror=alert(1)>',
    ]
    html = build_html_report(detail)

    assert 'class="spb"' in html
    assert "回测告警 2 项" in html
    assert "从未触发" in html
    # 警告文案中的 HTML 必须转义
    assert "<img src=x" not in html
    assert "&lt;img src=x" in html
    # 警告条必须在首个 KPI 网格之前（置顶）
    assert html.index('class="spb"') < html.index('class="kpi-grid"')


def test_no_signal_preflight_banner_when_warnings_empty():
    """record.warnings 为空/缺省 → 不渲染警告条。"""
    assert 'class="spb"' not in build_html_report(_detail())


def test_echarts_source_falls_back_to_exe_dir(monkeypatch, tmp_path):
    """打包环境（__file__ 推算路径缺失 + 无 node_modules）回退到 exe 同级 echarts.min.js。

    回归：打包后 /export/html 曾 500——报告需内联 ECharts 运行时，却只从
    __file__.parents 推算 web/node_modules（包内不存在）。"""
    from core.services import html_report as hr

    fake_exe = tmp_path / "tq-iquant.exe"
    fake_exe.write_bytes(b"")
    (tmp_path / "echarts.min.js").write_text(
        "window.echarts = { version: '5' };", encoding="utf-8")
    monkeypatch.setattr(hr, "_ECHARTS_PATH",
                        tmp_path / "not-exist" / "echarts.min.js")
    monkeypatch.setattr(sys, "argv", [str(fake_exe)])
    hr._echarts_source.cache_clear()
    try:
        # 回退命中 exe 同级清单文件
        assert "window.echarts" in hr._echarts_source()
    finally:
        hr._echarts_source.cache_clear()


def test_echarts_source_all_candidates_missing_raises_clear(monkeypatch, tmp_path):
    """所有候选都缺失 → 抛出带候选清单的 FileNotFoundError，便于定位。"""
    from core.services import html_report as hr

    monkeypatch.setattr(hr, "_ECHARTS_PATH",
                        tmp_path / "none" / "echarts.min.js")
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "x.exe")])
    hr._echarts_source.cache_clear()
    try:
        with pytest.raises(FileNotFoundError) as ei:
            _hr._echarts_source()
        assert "echarts.min.js" in str(ei.value)
        assert "已尝试" in str(ei.value)
    finally:
        hr._echarts_source.cache_clear()


def test_boxplot_data_requires_min_5_samples():
    """样本 <5 无法构成箱体（与前端 boxplotData 同口径），返回 None。"""
    assert _hr._boxplot_data([]) is None
    assert _hr._boxplot_data([0.1, -0.2, 0.3, 0.05]) is None
    five = _hr._boxplot_data([0.1, -0.2, 0.3, 0.05, 0.0])
    assert five is not None and len(five) == 5


def test_box_chart_drops_sparse_year_without_axis_misalignment():
    """<5 样本年份整年剔除，且年份轴与箱体一一对齐（不错位）。

    回归：旧实现先对全量年份生成 categories、再过滤 box_data 空值，
    中间年份缺箱体时后续箱体整体左移，与年份标签错位。
    """
    yearly = [
        {"year": 2022, "daily_returns": [0.001 * i for i in range(10)]},
        # 3 个样本（夹在中间）→ 整年剔除
        {"year": 2023, "daily_returns": [0.001, -0.002, 0.003]},
        {"year": 2024, "daily_returns": [-0.001 * i for i in range(8)]},
    ]
    out = _hr._box_chart({"yearly": yearly}, "daily_returns", "日")
    assert out is not None
    _, option = out
    cats = option["xAxis"]["data"]
    boxes = option["series"][0]["data"]
    assert cats == ["2022", "2024"]
    assert len(boxes) == len(cats) == 2
