"""单文件离线 HTML 回测分析报告（FR-21 / FR-22 / AC-9，Task 19）。

数据同源：入参 detail 必须是 services.get_record_detail 的返回 dict，
本模块只做 HTML 拼装与从快照派生净值/回撤曲线，**不重跑回测、不新增统计口径、
不手写数字**。产物为单个 .html：ECharts 运行时（web/node_modules vendored 副本）
与全部图表 option/JSON 内联，无任何外部资源引用（白名单见 ALLOWED_EXTERNAL_URLS，
均为 ECharts 压缩库自带的许可证/SVG 命名空间字符串）。
"""

import html as _html
import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ECHARTS_PATH = (
    Path(__file__).resolve().parents[3]
    / "web" / "node_modules" / "echarts" / "dist" / "echarts.min.js"
)

# 仅允许 vendored ECharts 压缩库自身携带的 URL（Apache 许可 + W3C SVG 命名空间 +
# zrender LICENSE）。报告模板/CSS/数据本身不允许出现任何 http(s) 链接。
ALLOWED_EXTERNAL_URLS: Tuple[str, ...] = (
    "http://www.apache.org/licenses/LICENSE-2.0",
    "http://www.w3.org/1999/xlink",
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/2000/xmlns/",
    "http://www.w3.org/XML/1998/namespace",
    "https://github.com/ecomfe/zrender/blob/master/LICENSE",
)

_RED = "#c0392b"
_GREEN = "#1e7e4f"
_GREY = "#6b7280"


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------
def esc(v: Any) -> str:
    return _html.escape("" if v is None else str(v))


def pct(v: Any, digits: int = 2) -> str:
    if v is None:
        return "—"
    return f"{float(v) * 100:.{digits}f}%"


def pct1(v: Any) -> str:
    return pct(v, 1)


def pct_signed(v: Any, digits: int = 2) -> str:
    if v is None:
        return "—"
    return f"{'+' if float(v) >= 0 else ''}{float(v) * 100:.{digits}f}%"


def num(v: Any, digits: int = 2) -> str:
    if v is None:
        return "—"
    return f"{float(v):.{digits}f}"


def cls(v: Any) -> str:
    """A 股配色：正红负绿。"""
    if v is None:
        return ""
    return "pos" if float(v) >= 0 else "neg"


# ---------------------------------------------------------------------------
# HTML 原语
# ---------------------------------------------------------------------------
def _kpi(label: str, value: str, sub: str = "", value_cls: str = "") -> str:
    return (
        '<div class="kpi"><div class="kpi-label"></div>'
        f'<div class="kpi-label">{esc(label)}</div>'
        f'<div class="kpi-value {value_cls}">{esc(value)}</div>'
        f'<div class="kpi-sub">{esc(sub)}</div></div>'
    )


def _table(headers: List[str], rows: List[List[str]],
           row_classes: Optional[List[List[Optional[str]]]] = None) -> str:
    thead = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = []
    for i, row in enumerate(rows):
        tds = []
        for j, cell in enumerate(row):
            c = row_classes[i][j] if row_classes and row_classes[i] else None
            tds.append(f'<td class="{c or ""}">{cell}</td>')
        body.append("<tr>" + "".join(tds) + "</tr>")
    return (
        '<table><thead><tr>' + thead + '</tr></thead><tbody>'
        + "".join(body) + '</tbody></table>'
    )


def _empty(text: str) -> str:
    return f'<div class="empty">🏭 {esc(text)}</div>'


def _chart(cid: str, height: int = 320) -> str:
    return f'<div id="{cid}" class="chart" style="height:{height}px"></div>'


# ---------------------------------------------------------------------------
# 图表 option（与前端同口径，静态序列化为 JSON 交给内联 ECharts 渲染）
# ---------------------------------------------------------------------------
def _nav_chart(snapshots: List[dict]) -> Optional[Tuple[str, dict]]:
    pts = [(s.get("snap_date"), s.get("total_value"), s.get("benchmark_value"))
           for s in snapshots if s.get("snap_date") and s.get("total_value")]
    if len(pts) < 2:
        return None
    dates = [p[0] for p in pts]
    base = float(pts[0][1])
    nav = [round(float(p[1]) / base, 4) for p in pts]
    # 回撤（%，负值）
    peak = base
    dd = []
    for p in pts:
        v = float(p[1])
        peak = max(peak, v)
        dd.append(round((v / peak - 1) * 100, 2))
    series = [{
        "name": "策略净值", "type": "line", "data": nav,
        "showSymbol": False, "xAxisIndex": 0, "yAxisIndex": 0,
        "color": _RED, "lineWidth": 1.5,
    }]
    bench_pts = [(d, b) for d, _, b in pts if b is not None]
    if len(bench_pts) >= 2:
        b0 = float(bench_pts[0][1])
        bench_map = {d: round(float(b) / b0, 4) for d, b in bench_pts}
        series.append({
            "name": "基准净值", "type": "line",
            "data": [bench_map.get(d) for d in dates],
            "connectNulls": True, "showSymbol": False,
            "xAxisIndex": 0, "yAxisIndex": 0, "color": "#4a7ec4",
        })
    series.append({
        "name": "回撤", "type": "line", "data": dd, "areaStyle": {"opacity": 0.25},
        "showSymbol": False, "xAxisIndex": 1, "yAxisIndex": 1,
        "color": _GREEN,
    })
    option = {
        "tooltip": {"trigger": "axis"},
        "legend": {"bottom": 0, "data": ["策略净值", "基准净值", "回撤"]},
        "grid": [
            {"left": 52, "right": 24, "top": 28, "height": "52%"},
            {"left": 52, "right": 24, "top": "72%", "height": "18%"},
        ],
        "xAxis": [
            {"type": "category", "data": dates, "boundaryGap": False,
             "axisLabel": {"formatter": "__DATEFMT__"}},
            {"type": "category", "data": dates, "gridIndex": 1,
             "boundaryGap": False, "show": False},
        ],
        "yAxis": [
            {"type": "value", "scale": True, "gridIndex": 0},
            {"type": "value", "gridIndex": 1, "max": 0,
             "axisLabel": {"formatter": "{value}%"}},
        ],
        "series": series,
    }
    return "chart-nav", option


def _dispersion_chart(d: dict) -> Optional[Tuple[str, dict]]:
    series = d.get("series") or []
    if not series:
        return None
    option = {
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 52, "right": 24, "top": 28, "bottom": 40, "containLabel": True},
        "xAxis": {"type": "category", "data": [p["date"] for p in series],
                  "boundaryGap": False,
                  "axisLabel": {"formatter": "__DATEFMT__"}},
        "yAxis": {"type": "value", "name": "截面标准差",
                  "axisLabel": {"formatter": "{value}"},
                  "splitLine": {"lineStyle": {"type": "dashed"}}},
        "series": [{"name": "宽基分化度", "type": "line", "showSymbol": False,
                    "color": "#7c3aed",
                    "data": [round(float(p["dispersion"]), 4) for p in series]}],
    }
    return "chart-disp", option


_IA_PALETTE = ['#4a7ec4', '#1f6e4f', '#d6403a', '#f0a429', '#7c3aed',
               '#0e7490', '#b45309', '#9d174d', '#6b7280', '#9ca3af']


def _industry_chart(ia: dict) -> Optional[Tuple[str, dict]]:
    exp = ia.get("exposure")
    if not exp:
        return None
    series = []
    for i, s in enumerate(exp.get("series", [])):
        series.append({
            "name": s["name"], "type": "line", "stack": "ia",
            "areaStyle": {"opacity": 0.75}, "symbol": "none", "smooth": True,
            "lineStyle": {"width": 0.5}, "color": _IA_PALETTE[i % len(_IA_PALETTE)],
            "data": [round(float(w) * 100, 2) for w in s.get("weights", [])],
        })
    series.append({
        "name": "现金", "type": "line", "stack": "ia",
        "areaStyle": {"opacity": 0.5}, "symbol": "none", "smooth": True,
        "lineStyle": {"width": 0.5}, "color": "#e5e7eb",
        "data": [round(float(w) * 100, 2) for w in exp.get("cash", [])],
    })
    option = {
        "tooltip": {"trigger": "axis"},
        "legend": {"bottom": 0, "type": "scroll"},
        "grid": {"left": 48, "right": 20, "bottom": 44, "top": 24, "containLabel": True},
        "xAxis": {"type": "category", "data": exp.get("dates", []),
                  "boundaryGap": False, "axisLabel": {"formatter": "__DATEFMT__"}},
        "yAxis": {"type": "value", "name": "仓位(%)", "min": 0, "max": 100,
                  "splitLine": {"lineStyle": {"type": "dashed"}}},
        "series": series,
    }
    return "chart-ia", option


def _robustness_chart(rb: dict) -> Optional[Tuple[str, dict]]:
    wins = (rb.get("rolling") or {}).get("windows") or []
    if not wins or wins[0].get("n_samples", 0) == 0:
        return None
    labels = [b["label"] for b in wins[0]["histogram"]]
    option = {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": {"bottom": 0},
        "grid": {"left": 40, "right": 20, "bottom": 40, "top": 24, "containLabel": True},
        "xAxis": {"type": "category", "data": labels,
                  "axisLabel": {"interval": 0, "fontSize": 10}},
        "yAxis": {"type": "value", "name": "窗口数", "minInterval": 1,
                  "splitLine": {"lineStyle": {"type": "dashed"}}},
        "series": [
            {"name": f"{w['window']} 周滚动收益", "type": "bar", "barMaxWidth": 26,
             "color": "#4a7ec4" if i == 0 else "#f0a429",
             "data": [b["count"] for b in w["histogram"]]}
            for i, w in enumerate(wins)
        ],
    }
    return "chart-rb", option


# ---------------------------------------------------------------------------
# 模块渲染
# ---------------------------------------------------------------------------
def _section(title: str, body: str) -> str:
    return f'<section><h2>{esc(title)}</h2>{body}</section>'


def _overview(detail: dict) -> str:
    rec = detail.get("record") or {}
    ev = detail.get("evaluations") or {}
    ca = detail.get("crisis_alpha") or {}
    ca_data = ca.get("data") or {}
    q = ca_data.get("quadrant") or {}
    cards = [
        _kpi("总收益", pct(ev.get("total_return")), value_cls=cls(ev.get("total_return"))),
        _kpi("年化收益", pct(ev.get("annual_return")), value_cls=cls(ev.get("annual_return"))),
        _kpi("最大回撤", pct(ev.get("max_drawdown")), value_cls="neg"),
        _kpi("夏普比率", num(ev.get("sharpe_ratio")), value_cls=""),
        _kpi("年化波动率", pct(ev.get("volatility"))),
        _kpi("卡玛比率", num(ev.get("calmar_ratio"))),
    ]
    if ca.get("status") == "completed":
        cards.append(_kpi("危机Alpha占比", pct(q.get("alpha_ratio")),
                          sub=esc(ca_data.get("alpha_rating") or ""),
                          value_cls=cls(q.get("alpha_ratio"))))
    mr = detail.get("market_regime") or {}
    strategy_name = (
        ca_data.get("strategy_name")
        or ((mr.get("data") or {}).get("meta") or {}).get("strategy_name")
        or "")
    meta = (
        f'<div class="meta"><b>{esc(rec.get("name"))}</b>'
        f'（记录 #{rec.get("id")}）　{esc(rec.get("start_date"))} ~ '
        f'{esc(rec.get("end_date"))}'
        + (f'　策略：{esc(strategy_name)}' if strategy_name else "")
        + f'　基准：{esc(ca.get("benchmark_name") or mr.get("benchmark_name") or "—")}'
        f'<div class="gen">报告生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
        "（离线单文件，数字与平台详情接口同源）</div></div>"
    )
    return meta + '<div class="kpi-grid">' + "".join(cards) + "</div>"


def _crisis_section(detail: dict) -> str:
    ca = detail.get("crisis_alpha")
    if ca is None:
        return _section("一、危机Alpha 防守专项",
                        _empty("本次回测未计算危机 Alpha 专项"))
    if ca.get("status") != "completed":
        return _section("一、危机Alpha 防守专项",
                        _empty(f"专项状态：{esc(ca.get('status'))}；{esc(ca.get('error') or '')}"))
    d = ca.get("data") or {}
    q = d.get("quadrant") or {}
    cards = "".join([
        _kpi("危机评级", esc(d.get("alpha_rating") or "—"), "基准下跌周中的综合表现"),
        _kpi("危机Alpha占比", pct(q.get("alpha_ratio")), "下跌周上涨占比 − 同步下跌占比",
             cls(q.get("alpha_ratio"))),
        _kpi("危机周胜率", pct(q.get("hit_rate", q.get("up_rate"))),
             f"样本 {q.get('n_crisis', 0)} 周"),
        _kpi("跟跌比", pct(q.get("follow_ratio"))),
        _kpi("盈亏比", num(q.get("odds"))),
    ])
    mb_rows = []
    mb_cls = []
    for r in d.get("multi_bench") or []:
        name = r["name"] + ("（主）" if r.get("is_main") else "")
        mb_rows.append([esc(name), pct(r.get("alpha_ratio")),
                        pct(r.get("up_rate")), str(r.get("n_down", 0))])
        mb_cls.append([None, cls(r.get("alpha_ratio")), None, None])
    tables = _table(["基准", "危机Alpha占比", "下跌周胜率", "下跌周数"],
                    mb_rows, mb_cls)
    events = d.get("deep_dd_events") or []
    if events:
        ev_rows = []
        for e in events[:10]:
            ev_rows.append([
                esc(e.get("start")), esc(e.get("end")),
                pct_signed(e.get("benchmark_ret")),
                pct_signed(e.get("strategy_ret")),
                pct_signed(e.get("excess")),
                esc(e.get("verdict")), pct(e.get("max_dd")),
                f"{e.get('days', 0)}天",
            ])
        tables += '<h3>深度回撤事件（Top 10）</h3>' + _table(
            ["起点", "终点", "基准涨跌", "策略涨跌", "超额", "判定",
             "期间最大回撤", "天数"], ev_rows)
    return _section("一、危机Alpha 防守专项",
                    f'<div class="kpi-grid">{cards}</div>{tables}')


_STATE_ORDER = ("bull", "bear", "range", "rebound")


def _regime_states(d: dict) -> str:
    rt = d.get("regime_table") or {}
    rows, row_cls = [], []
    for k in _STATE_ORDER:
        r = rt.get(k)
        if not r:
            continue
        rows.append([
            esc(r.get("label_cn")), str(r.get("weeks", 0)), pct1(r.get("ratio")),
            pct_signed(r.get("strategy_return")),
            pct_signed(r.get("benchmark_return")),
            pct_signed(r.get("excess_return")),
            pct1(r.get("win_rate")), pct(r.get("max_drawdown")),
            f"{num(r.get('turnover'))}x", pct1(r.get("cost_ratio")),
        ])
        row_cls.append([None, None, None, cls(r.get("strategy_return")),
                        None, cls(r.get("excess_return")), None, "neg",
                        None, None])
    return _table(
        ["市场状态", "周数", "占比", "策略收益", "基准收益", "超额",
         "日胜率", "最大回撤", "换手", "成本占比"], rows, row_cls)


def _opportunity(d: dict) -> str:
    o = d.get("opportunity") or {}
    cards = "".join([
        _kpi("趋势上涨周数", str(o.get("n_bull_weeks", 0))),
        _kpi("上涨捕获率", pct1(o.get("up_capture"))),
        _kpi("踏空比例", pct1(o.get("miss_ratio"))),
    ])
    big = o.get("big_up_tests") or []
    tables = ""
    if big:
        tables = _table(
            ["基准大涨阈值", "次数", "策略平均", "基准平均", "策略胜率"],
            [[f"≥ +{round(float(t['threshold']) * 100)}%", str(t.get("n", 0)),
              pct_signed(t.get("avg_strategy")), pct_signed(t.get("avg_benchmark")),
              pct1(t.get("win_rate"))] for t in big])
    cats = o.get("cats") or {}
    if cats:
        tables += _table(
            ["上涨分类", "次数", "策略平均收益"],
            [[esc(name), str(c.get("count", 0)), pct_signed(c.get("avg_strategy"))]
             for name, c in cats.items()])
    return f'<div class="kpi-grid">{cards}</div>{tables}'


def _rebound(d: dict) -> str:
    rb = d.get("rebound") or {}
    cards = "".join([
        _kpi("反弹段数", str(rb.get("n_segments", 0))),
        _kpi("平均反弹弹性", num(rb.get("avg_elasticity"))),
    ])
    segs = rb.get("segments") or []
    table = ""
    if segs:
        rows = []
        for s in segs:
            rows.append([
                esc(s.get("start_date")), esc(s.get("trough_date")),
                esc(s.get("end_date")), str(s.get("weeks", 0)),
                pct_signed(s.get("benchmark_rebound_pct")),
                pct_signed(s.get("strategy_rebound_pct")),
                f"{num(s.get('elasticity'))}x",
                (f"{s.get('benchmark_recover_days')}天"
                 if s.get("benchmark_recovered") else "未收复"),
                (f"{s.get('strategy_recover_days')}天"
                 if s.get("strategy_recovered") else "未收复"),
            ])
        table = _table(
            ["起点", "谷底", "终点", "周数", "基准反弹", "策略反弹", "弹性",
             "基准收复", "策略收复"], rows)
    return f'<div class="kpi-grid">{cards}</div>{table}'


def _neutral(d: dict) -> str:
    n = d.get("neutral") or {}
    cards = "".join([
        _kpi("震荡周数", str(n.get("n_range_weeks", 0))),
        _kpi("震荡周占比", pct(n.get("range_ratio"))),
        _kpi("区间累计收益", pct_signed(n.get("cumulative_return")),
             value_cls=cls(n.get("cumulative_return"))),
        _kpi("年化收益", pct_signed(n.get("annualized_return"))),
        _kpi("周胜率", pct1(n.get("win_rate"))),
        _kpi("月均交易", num(n.get("monthly_trades"), 1)),
        _kpi("年化换手", num(n.get("annual_turnover"))),
        _kpi("成本/毛利", pct1(n.get("cost_gross_ratio"))),
        _kpi("假突破磨损", f"{n.get('whipsaw_count', 0)} 次",
             f"{n.get('whipsaw_days', 0)} 个交易日"),
    ])
    return f'<div class="kpi-grid">{cards}</div>'


def _style_block(d: dict) -> str:
    st = d.get("style") or {}
    matrix = st.get("matrix") or {}
    rows, row_cls = [], []
    for r in matrix.values():
        rows.append([
            esc(r.get("label")), str(r.get("weeks", 0)), pct1(r.get("ratio")),
            pct_signed(r.get("strategy_return")),
            pct_signed(r.get("leader_return")),
            pct_signed(r.get("excess_return")),
            pct1(r.get("win_rate")), pct(r.get("max_drawdown")),
        ])
        row_cls.append([None, None, None, cls(r.get("strategy_return")),
                        None, cls(r.get("excess_return")), None, "neg"])
    out = _table(["领涨风格", "周数", "占比", "策略收益", "领涨指数",
                  "超额", "胜率", "最大回撤"], rows, row_cls) if rows else _empty(
        "风格行情样本不足")
    if st.get("skipped"):
        out += f'<p class="note">跳过指数：{esc("、".join(st["skipped"]))}</p>'
    return out


def _dispersion_block(d: dict, charts: List[Tuple[str, dict]]) -> str:
    dp = d.get("dispersion") or {}
    chart = _dispersion_chart(dp)
    out = _chart("chart-disp") if chart else ""
    if chart:
        charts.append(chart)
    buckets = dp.get("buckets") or {}
    rows = []
    labels = {"low": "低分化", "mid": "中分化", "high": "高分化"}
    for k in ("low", "mid", "high"):
        b = buckets.get(k) or {}
        rows.append([labels[k], str(b.get("count", 0)),
                     pct_signed(b.get("mean_excess")), pct1(b.get("win_rate"))])
    if rows:
        out += _table(["分化档位", "周数", "平均超额", "胜率"], rows)
    if dp.get("skipped"):
        out += f'<p class="note">跳过指数：{esc("、".join(dp["skipped"]))}</p>'
    return out or _empty("宽基数据不足，分化度不可用")


def _exposure_table(ex: dict, first_col: str, empty_text: str) -> str:
    names = ex.get("available_names") or []
    codes = ex.get("available_codes") or []
    corr = ex.get("corr") or {}
    beta = ex.get("beta") or {}
    rows = []
    for i, code in enumerate(codes):
        name = names[i] if i < len(names) else code
        cvals = corr.get(code) or []
        bvals = beta.get(code) or []
        last_corr = next((v for v in reversed(cvals) if v is not None), None)
        last_beta = next((v for v in reversed(bvals) if v is not None), None)
        rows.append([esc(name), num(last_corr), num(last_beta)])
    out = _table([first_col, f"最新滚动相关（{ex.get('window', 52)}周）",
                  "最新 OLS Beta"], rows) if rows else _empty(empty_text)
    if ex.get("skipped"):
        out += f'<p class="note">跳过指数：{esc("、".join(ex["skipped"]))}</p>'
    return out


def _exposure_block(d: dict) -> str:
    return _exposure_table(d.get("exposure") or {}, "宽基指数", "宽基样本不足")


def _quadrant_block(d: dict) -> str:
    """增强A：巨潮四象限纯风格暴露。老记录 payload 无此键 → 空态不报错。"""
    ex = d.get("quadrant_exposure")
    if ex is None:
        return _empty("该记录暂无四象限暴露数据（老记录重算后可用）")
    return _exposure_table(ex, "风格指数", "四象限指数样本不足")


def _industry_block(d: dict, charts: List[Tuple[str, dict]]) -> str:
    ia = d.get("industry_attribution")
    if not ia or ia.get("status") == "needs_rerun":
        reason = (ia or {}).get("reason") or "该记录回测时未保存持仓明细，重新回测后可用"
        return _empty(reason)
    if ia.get("status") == "failed":
        return _empty(f"行业归因不可用：{ia.get('error', '')}")
    hr = ia.get("hit_rate") or {}
    cards = []
    for key, label in (("weekly", "周度领涨命中率"), ("monthly", "月度领涨命中率")):
        p = hr.get(key) or {}
        cards.append(_kpi(label, pct1(p.get("hit_rate")),
                          f"{p.get('hits', 0)}/{p.get('periods_holding', 0)} 期命中（Top{p.get('top_n', 3)}）"))
    out = f'<div class="kpi-grid">{"".join(cards)}</div>'
    chart = _industry_chart(ia)
    if chart:
        charts.append(chart)
        out += _chart("chart-ia", 300)
    unmapped = ia.get("unmapped_codes") or []
    if unmapped:
        out += f'<p class="note">未匹配行业（计入未分类）：{esc("、".join(unmapped))}</p>'
    contrib = ia.get("contribution") or {}
    if not contrib.get("available"):
        out += _empty("行业指数不可得，仅展示暴露与命中率")
    else:
        rows, row_cls = [], []
        for r in contrib.get("rows") or []:
            rows.append([esc(r.get("name")), pct1(r.get("avg_weight")),
                         pct_signed(r.get("index_return")),
                         pct_signed(r.get("contribution"))])
            row_cls.append([None, None, cls(r.get("index_return")),
                            cls(r.get("contribution"))])
        out += "<h3>行业收益贡献分解（期初权重 × 行业日收益）</h3>" + _table(
            ["行业", "平均权重", "行业区间涨跌", "配置贡献"], rows, row_cls)
    out += ('<p class="note">贡献为行业配置项（Σ 期初行业权重 × 行业日收益），'
            "不含行业内个股选择 alpha。</p>")
    return out


def _robustness_block(d: dict, charts: List[Tuple[str, dict]]) -> str:
    rb = d.get("robustness")
    if not rb or rb.get("status") != "ok":
        reason = (rb or {}).get("error") or "稳健性分析不可用"
        return _empty(reason)
    w = rb.get("longest_loss_window") or {}
    loss_text = "无（净值持续创新高）"
    if w.get("days") and w.get("start_date"):
        loss_text = f"{w['start_date']} → {w['end_date']}（谷底 {w['trough_date']}）"
    cards = "".join([
        _kpi("最长亏损窗口", f"{w.get('days', 0)} 天", loss_text),
        _kpi("窗口最深回撤", pct1(w.get("drawdown")), "峰值日到收复日的谷底幅度",
             "neg"),
        _kpi("分析样本", f"{rb.get('n_days', 0)} 个交易日",
             f"{rb.get('start_date', '')} ~ {rb.get('end_date', '')}"),
    ])
    out = f'<div class="kpi-grid">{cards}</div>'

    hy_rows, hy_cls = [], []
    for r in rb.get("half_year_matrix") or []:
        hy_rows.append([
            esc(r.get("period")), pct_signed(r.get("strategy_return")),
            pct_signed(r.get("benchmark_return")),
            pct_signed(r.get("excess_return")),
            pct1(r.get("max_drawdown")), num(r.get("sharpe")),
        ])
        hy_cls.append([None, cls(r.get("strategy_return")),
                       cls(r.get("benchmark_return")),
                       cls(r.get("excess_return")), "neg", None])
    out += "<h3>逐半年指标矩阵</h3>" + _table(
        ["区间", "策略收益", "基准收益", "超额", "最大回撤", "夏普"],
        hy_rows, hy_cls)

    win_rows = []
    for win in (rb.get("rolling") or {}).get("windows") or []:
        if win.get("n_samples", 0) == 0:
            win_rows.append([f"{win['window']} 周", "0",
                             f"周频样本不足 {win['window']} 周，无法计算", "", "",
                             "", "", "", ""])
            continue
        rdist, sdist = win.get("returns") or {}, win.get("sharpe") or {}
        win_rows.append([
            f"{win['window']} 周", str(win.get("n_samples", 0)),
            pct_signed(rdist.get("median")), pct_signed(rdist.get("min")),
            pct_signed(rdist.get("max")), pct1(rdist.get("negative_ratio")),
            num(sdist.get("median")), num(sdist.get("min")),
            pct1(sdist.get("negative_ratio")),
        ])
    out += "<h3>滚动 26/52 周收益与夏普分布</h3>" + _table(
        ["窗口", "样本数", "收益中位", "最差", "最好", "负收益占比",
         "夏普中位", "夏普最差", "夏普为负占比"], win_rows)
    chart = _robustness_chart(rb)
    if chart:
        charts.append(chart)
        out += _chart("chart-rb", 300)

    sens_rows, sens_cls = [], []
    for r in rb.get("benchmark_sensitivity") or []:
        name = r.get("name") + ("（主）" if r.get("is_primary") else "")
        if not r.get("available"):
            sens_rows.append([esc(name),
                              f"对齐交易日不足（{r.get('n_days', 0)} 日），无法计算",
                              "", "", "", "", "", "", ""])
            sens_cls.append([None] * 9)
            continue
        sens_rows.append([
            esc(name), num(r.get("beta")), pct_signed(r.get("jensen_alpha")),
            pct_signed(r.get("excess_annual_return")),
            pct1(r.get("tracking_error")), num(r.get("information_ratio")),
            pct1(r.get("up_capture_ratio")), pct1(r.get("down_capture_ratio")),
            pct1(r.get("excess_max_drawdown")),
        ])
        sens_cls.append([None, None, cls(r.get("jensen_alpha")),
                         cls(r.get("excess_annual_return")), None,
                         cls(r.get("information_ratio")), None, None, "neg"])
    out += "<h3>基准替换敏感性（FR-1 相对指标逐基独立计算）</h3>" + _table(
        ["基准", "β", "Jensen α(年化)", "年化超额", "跟踪误差", "信息比率",
         "上行捕获", "下行捕获", "超额回撤"], sens_rows, sens_cls)
    out += ('<p class="note">全部统计仅基于每日净值快照与基准日线；滚动收益按周末'
            "净值几何累乘，夏普按周收益年化（×√52）。</p>")
    return out


def _market_regime_section(detail: dict,
                           charts: List[Tuple[str, dict]]) -> str:
    mr = detail.get("market_regime")
    if mr is None:
        return _section("二、市场状态×绩效（机遇/中性Alpha · 风格 · 行业 · 稳健性）",
                        _empty("本次回测未计算市场状态分析"))
    if mr.get("status") != "completed":
        return _section("二、市场状态×绩效（机遇/中性Alpha · 风格 · 行业 · 稳健性）",
                        _empty(f"分析状态：{esc(mr.get('status'))}；{esc(mr.get('error') or '')}"))
    d = mr.get("data") or {}
    body = ["<h3>2.1 市场状态×绩效总表</h3>", _regime_states(d),
            "<h3>2.2 机遇 Alpha（上涨捕获）</h3>", _opportunity(d),
            "<h3>2.3 反弹弹性（下跌后反弹段）</h3>", _rebound(d),
            "<h3>2.4 中性 Alpha（震荡磨损）</h3>", _neutral(d),
            "<h3>2.5 风格×绩效矩阵（结构性行情）</h3>", _style_block(d),
            "<h3>2.6 市场分化度（宽基截面标准差）</h3>",
            _dispersion_block(d, charts),
            "<h3>2.7 隐含风格暴露（滚动相关 / OLS Beta）</h3>",
            _exposure_block(d),
            "<h3>2.7-B 巨潮四象限·纯风格暴露（规模×成长/价值）</h3>",
            _quadrant_block(d),
            f"<h3>2.8 行业归因（{esc((d.get('industry_attribution') or {}).get('taxonomy') or '通达信二级行业')}）</h3>",
            _industry_block(d, charts),
            "<h3>2.9 稳健性体检 🔬（只读快照计算，不重跑回测）</h3>",
            _robustness_block(d, charts)]
    return _section("二、市场状态×绩效（机遇/中性Alpha · 风格 · 行业 · 稳健性）",
                    "".join(body))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _echarts_source() -> str:
    return _ECHARTS_PATH.read_text(encoding="utf-8")


_CSS = """
body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;margin:0;background:#f5f6f8;color:#1f2937}
.wrap{max-width:1080px;margin:0 auto;padding:24px 28px 64px}
h1{font-size:22px;margin:0 0 6px} h2{font-size:17px;border-left:4px solid #4a7ec4;padding-left:10px;margin:28px 0 12px}
h3{font-size:14px;color:#374151;margin:18px 0 8px}
section{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:16px 20px;margin-top:18px}
.meta{font-size:13px;color:#4b5563;line-height:1.9;margin-bottom:14px}.gen{color:#9ca3af;font-size:12px}
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin:10px 0}
.kpi{background:#f9fafb;border:1px solid #eef0f3;border-radius:6px;padding:10px 12px}
.kpi-label{font-size:12px;color:#6b7280}.kpi-value{font-size:19px;font-weight:700;margin-top:2px}
.kpi-sub{font-size:11px;color:#9ca3af;margin-top:3px;min-height:14px}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin:8px 0 12px}
th,td{border:1px solid #e5e7eb;padding:6px 8px;text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{background:#f3f4f6;color:#374151}
.pos{color:#c0392b}.neg{color:#1e7e4f}
.empty{background:#f9fafb;border:1px dashed #d1d5db;border-radius:6px;padding:18px;text-align:center;color:#6b7280;font-size:13px}
.note{font-size:12px;color:#9ca3af;margin:6px 0}
.chart{width:100%;margin:8px 0}
details{margin:8px 0}summary{cursor:pointer;font-size:13px;color:#4b5563}
pre.json-dump{background:#0f172a;color:#e2e8f0;border-radius:6px;padding:12px;font-size:11px;line-height:1.5;max-height:480px;overflow:auto;white-space:pre-wrap;word-break:break-all}
"""


def _appendix(detail: dict) -> str:
    """附录：完整 detail payload 原始 JSON（折叠展示，离线复核/调试用）。

    Minor-4 修复：tasks 证据声称的「附录原始 JSON」补实现。esc 防 XSS。
    """
    raw = json.dumps(detail, ensure_ascii=False, default=str, indent=1)
    return _section(
        "附录 · 原始数据 JSON",
        f'<details><summary>展开查看完整回测详情数据（{len(raw)} 字符）</summary>'
        f'<pre class="json-dump">{esc(raw)}</pre></details>')


def build_html_report(detail: dict) -> str:
    charts: List[Tuple[str, dict]] = []
    nav = _nav_chart(detail.get("snapshots") or [])
    nav_html = ""
    if nav:
        charts.append(nav)
        nav_html = _section("净值 / 基准 / 回撤", _chart("chart-nav", 380))

    body = (
        _overview(detail)
        + nav_html
        + _crisis_section(detail)
        + _market_regime_section(detail, charts)
        + _appendix(detail)
    )
    options = {cid: opt for cid, opt in charts}
    options_json = json.dumps(options, ensure_ascii=False).replace("</", "<\\/")
    boot = """
<script>
var __CHARTS__ = __OPTIONS__;
// 日期轴标签只显示月-日，避免拥挤
function __datefmt(v){ return v ? String(v).slice(5) : v; }
document.querySelectorAll('.chart').forEach(function(el){
  var opt = __CHARTS__[el.id];
  if(!opt) return;
  [opt.xAxis].flat().forEach(function(ax){
    if(ax && ax.axisLabel && ax.axisLabel.formatter === '__DATEFMT__'){
      ax.axisLabel.formatter = __datefmt;
    }
  });
  echarts.init(el, null, {renderer:'canvas'}).setOption(opt);
});
</script>
""".replace("__OPTIONS__", options_json)
    rec = detail.get("record") or {}
    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>回测分析报告 - {esc(rec.get('name'))}</title>"
        f"<style>{_CSS}</style></head><body><div class=\"wrap\">"
        f"<h1>回测分析报告：{esc(rec.get('name'))}</h1>"
        f"<script>{_echarts_source()}</script>"
        f"{body}{boot}</div></body></html>"
    )
