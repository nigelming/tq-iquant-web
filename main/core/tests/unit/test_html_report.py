"""TR-19 单文件离线 HTML 报告：自包含/无外链/数字同源（FR-21/FR-22）。"""
import json
import re

import pytest

from core.services.html_report import build_html_report, ALLOWED_EXTERNAL_URLS


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
