"""单文件离线 HTML 回测分析报告（FR-21 / FR-22 / AC-9，Task 19）。

数据同源：入参 detail 必须是 services.get_record_detail 的返回 dict，
本模块只做 HTML 拼装与从快照派生净值/回撤曲线，**不重跑回测、不新增统计口径、
不手写数字**。产物为单个 .html：ECharts 运行时（web/node_modules vendored 副本）
与全部图表 option/JSON 内联，无任何外部资源引用（白名单见 ALLOWED_EXTERNAL_URLS，
均为 ECharts 压缩库自带的许可证/SVG 命名空间字符串）。
"""

import html as _html
import json
import sys
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
    # 外层 chart-box 承担窄窗口下的横向滚动：图表本身保底 760px 宽，
    # 避免窗口 <900px 时 12 个月热力图/多序列折线的文字被压扁重叠。
    return (f'<div class="chart-box">'
            f'<div id="{cid}" class="chart" style="height:{height}px"></div>'
            f'</div>')


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
            "xAxisIndex": 0, "yAxisIndex": 2, "color": "#4a7ec4",
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
            {"left": 70, "right": 84, "top": 34, "height": "52%"},
            {"left": 70, "right": 84, "top": "74%", "height": "16%"},
        ],
        "xAxis": [
            {"type": "category", "data": dates, "boundaryGap": False,
             "axisLabel": {"formatter": "__DATEFMT__", "fontSize": 11}},
            {"type": "category", "data": dates, "gridIndex": 1,
             "boundaryGap": False, "show": False},
        ],
        "yAxis": [
            # 左轴：策略净值（归一化）；轴名竖排居中，避免顶部被裁切
            {"type": "value", "scale": True, "gridIndex": 0, "name": "策略净值",
             "nameLocation": "middle", "nameRotate": 90, "nameGap": 54,
             "nameTextStyle": {"fontSize": 12}},
            # 网格1 的左轴：回撤（%）
            {"type": "value", "gridIndex": 1, "max": 0,
             "axisLabel": {"formatter": "{value}%"}},
            # 右轴：基准净值（独立刻度，避免与策略净值量级不同被压平）
            {"type": "value", "scale": True, "gridIndex": 0, "position": "right",
             "name": "基准净值", "nameLocation": "middle", "nameRotate": 90,
             "nameGap": 54, "nameTextStyle": {"fontSize": 12},
             "splitLine": {"show": False}},
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
        # 轴名竖排居中：默认 nameLocation='end' 会把「截面标准差」挤在左上角
        # 与刻度挤在一起（顶部 top=28 时被裁掉半截）
        "grid": {"left": 66, "right": 24, "top": 30, "bottom": 40},
        "xAxis": {"type": "category", "data": [p["date"] for p in series],
                  "boundaryGap": False,
                  "axisLabel": {"formatter": "__DATEFMT__"}},
        "yAxis": {"type": "value", "name": "截面标准差",
                  "nameLocation": "middle", "nameRotate": 90, "nameGap": 46,
                  "nameTextStyle": {"fontSize": 12},
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
        "grid": {"left": 24, "right": 20, "bottom": 44, "top": 24, "containLabel": True},
        "xAxis": {"type": "category", "data": exp.get("dates", []),
                  "boundaryGap": False, "axisLabel": {"formatter": "__DATEFMT__"}},
        # 竖排居中，避免「仓位(%)」在左上角与 100% 刻度重叠
        "yAxis": {"type": "value", "name": "仓位(%)", "min": 0, "max": 100,
                  "nameLocation": "middle", "nameRotate": 90, "nameGap": 44,
                  "nameTextStyle": {"fontSize": 12},
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
        "legend": {"bottom": 0, "itemGap": 18, "textStyle": {"fontSize": 12}},
        "grid": {"left": 68, "right": 24, "bottom": 54, "top": 34},
        "xAxis": {"type": "category", "data": labels,
                  "axisLabel": {"interval": 0, "fontSize": 11}},
        # 轴名竖排居中：默认 'end' 会与顶部刻度挤在一起，被裁切成半截
        "yAxis": {"type": "value", "name": "窗口数", "minInterval": 1,
                  "nameLocation": "middle", "nameRotate": 90, "nameGap": 52,
                  "nameTextStyle": {"fontSize": 12},
                  "axisLabel": {"fontSize": 11},
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
    # 回测告警（行情跳过/公式报错/输出线缺失/区间零信号）——置顶，解释"为什么没有交易"
    warning_block = ""
    warnings = rec.get("warnings") or []
    if warnings:
        items = "".join(f"<li>{esc(w)}</li>" for w in warnings)
        warning_block = (
            '<div class="spb"><div class="spb-t">'
            f'⚠️ 回测告警 {len(warnings)} 项'
            "<span>（回测已完成，但可能存在行情数据或信号问题）</span></div>"
            f"<ul>{items}</ul></div>"
        )
    return meta + warning_block + '<div class="kpi-grid">' + "".join(cards) + "</div>"


def _ca_nav_chart(d: dict) -> Optional[Tuple[str, dict]]:
    nav = d.get("nav") or {}
    dates, strategy, benchmark = nav.get("dates"), nav.get("strategy"), nav.get("benchmark")
    if not dates or not strategy:
        return None
    series = [{
        "name": "策略净值", "type": "line", "data": strategy,
        "showSymbol": False, "color": _RED, "yAxisIndex": 0,
    }]
    if benchmark:
        series.append({
            "name": "基准净值", "type": "line", "data": benchmark,
            "showSymbol": False, "color": "#4a7ec4", "yAxisIndex": 1,
        })
    option = {
        "tooltip": {"trigger": "axis"},
        "legend": {"bottom": 0},
        "grid": {"left": 74, "right": 92, "top": 34, "bottom": 46},
        "xAxis": {"type": "category", "data": dates, "boundaryGap": False,
                  "axisLabel": {"formatter": "__DATEFMT__", "fontSize": 11}},
        "yAxis": [
            # 左轴=策略净值、右轴=基准净值：两者量级差异大时同一刻度会把曲线压平；
            # 轴名均竖排居中，避免顶部横排被裁切
            {"type": "value", "scale": True, "name": "策略净值",
             "nameLocation": "middle", "nameRotate": 90, "nameGap": 54,
             "nameTextStyle": {"fontSize": 12}, "axisLabel": {"fontSize": 11}},
            {"type": "value", "scale": True, "name": "基准净值", "position": "right",
             "nameLocation": "middle", "nameRotate": 90, "nameGap": 54,
             "nameTextStyle": {"fontSize": 12}, "axisLabel": {"fontSize": 11},
             "splitLine": {"show": False}},
        ],
        "series": series,
    }
    return "chart-ca-nav", option


def _ca_scatter_chart(d: dict) -> Optional[Tuple[str, dict]]:
    weeks = d.get("weekly_detail") or []
    data = [(float(w["基准收益"]), float(w["策略收益"]))
            for w in weeks if w.get("基准收益") is not None and w.get("策略收益") is not None]
    if not data:
        return None
    option = {
        "tooltip": {"trigger": "item", "formatter": "基准 {c}"},
        # 轴名放轴中点（nameLocation=middle）：默认 'end' 会把「策略周收益」挤在左上角被
        # 裁切；grid 手动让出轴名与刻度的空间（containLabel 不再兜底，避免双重挤压）。
        "grid": {"left": 68, "right": 40, "top": 34, "bottom": 58},
        "xAxis": {"type": "value", "name": "基准周收益(%)", "scale": True,
                  "nameLocation": "middle", "nameGap": 30,
                  "nameTextStyle": {"fontSize": 12},
                  "splitLine": {"lineStyle": {"type": "dashed"}}},
        "yAxis": {"type": "value", "name": "策略周收益(%)", "scale": True,
                  "nameLocation": "middle", "nameGap": 46,
                  "nameTextStyle": {"fontSize": 12},
                  "splitLine": {"lineStyle": {"type": "dashed"}}},
        "series": [{
            "type": "scatter", "symbolSize": 8,
            "data": [[round(x * 100, 2), round(y * 100, 2)] for x, y in data],
            "itemStyle": {"color": "rgba(192,57,43,.55)"},
            "markLine": {"silent": True, "symbol": "none",
                         "data": [{"xAxis": 0}, {"yAxis": 0}],
                         "lineStyle": {"type": "dashed", "color": "#9ca3af"}},
        }],
    }
    return "chart-ca-scatter", option


def _ca_corr_chart(d: dict) -> Optional[Tuple[str, dict]]:
    rc = d.get("rolling_corr") or []
    ec = d.get("expanding_corr") or []
    if not rc:
        return None
    dates = []
    seen = set()
    for p in list(ec) + list(rc):
        if p.get("date") not in seen:
            seen.add(p["date"])
            dates.append(p["date"])
    pos = {v: i for i, v in enumerate(dates)}
    early = [None] * len(dates)
    for p in ec:
        early[pos[p["date"]]] = p.get("value")
    roll = [None] * len(dates)
    for p in rc:
        roll[pos[p["date"]]] = p.get("value")
    option = {
        "tooltip": {"trigger": "axis"},
        "legend": {"bottom": 0, "data": ["52周滚动相关", "扩展窗口相关"]},
        "grid": {"left": 72, "right": 34, "top": 34, "bottom": 46},
        "xAxis": {"type": "category", "data": dates, "boundaryGap": False,
                  "axisLabel": {"formatter": "__DATEFMT__", "fontSize": 11}},
        # 轴名放轴中点并竖排：默认 nameLocation='end' 会把「相关系数」挤到左上角被裁切
        "yAxis": {"type": "value", "name": "相关系数",
                  "nameLocation": "middle", "nameRotate": 90, "nameGap": 52,
                  "nameTextStyle": {"fontSize": 12},
                  "axisLabel": {"fontSize": 11},
                  "splitLine": {"lineStyle": {"type": "dashed"}}},
        "series": [
            {"name": "52周滚动相关", "type": "line", "data": roll,
             "showSymbol": False, "color": "#7c3aed", "connectNulls": False},
            {"name": "扩展窗口相关", "type": "line", "data": early,
             "showSymbol": False, "color": "#9ca3af", "lineStyle": {"type": "dashed"},
             "connectNulls": False},
        ],
    }
    return "chart-ca-corr", option


def _ca_multi_chart(d: dict) -> Optional[Tuple[str, dict]]:
    rows = d.get("multi_bench") or []
    if not rows:
        return None
    option = {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "grid": {"left": 24, "right": 24, "top": 24, "bottom": 40, "containLabel": True},
        "xAxis": {"type": "category",
                  "data": [r["name"] + ("（主）" if r.get("is_main") else "") for r in rows]},
        # 竖排居中：默认 'end' 会把「危机Alpha占比」挤在左上角被裁切
        "yAxis": {"type": "value", "name": "危机Alpha占比",
                  "nameLocation": "middle", "nameRotate": 90, "nameGap": 46,
                  "nameTextStyle": {"fontSize": 12},
                  "splitLine": {"lineStyle": {"type": "dashed"}}},
        "series": [{"type": "bar", "barMaxWidth": 40,
                    "data": [{"value": round(r.get("alpha_ratio", 0) * 100, 2),
                              "itemStyle": {"color": _RED if r.get("is_main") else "#f0a429"}}
                             for r in rows]}],
    }
    return "chart-ca-multi", option


def _ca_annual_bar(d: dict) -> Optional[Tuple[str, dict]]:
    rows = d.get("annual") or []
    if not rows:
        return None
    option = {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "grid": {"left": 24, "right": 24, "top": 24, "bottom": 40, "containLabel": True},
        "xAxis": {"type": "category",
                  "data": [f"{r['年份']}{'*' if not r.get('完整') else ''}" for r in rows]},
        # 竖排居中，避免「危机Alpha」轴名与顶部刻度重叠
        "yAxis": {"type": "value", "name": "危机Alpha",
                  "nameLocation": "middle", "nameRotate": 90, "nameGap": 42,
                  "nameTextStyle": {"fontSize": 12},
                  "splitLine": {"lineStyle": {"type": "dashed"}}},
        "series": [{"type": "bar", "barMaxWidth": 34,
                    "data": [{"value": round(r.get("危机Alpha", 0) * 100, 2),
                              "itemStyle": {"color": _RED if r.get("危机Alpha", 0) * 100 >= 85 else "#f0a429"}}
                             for r in rows]}],
    }
    return "chart-ca-annual", option


def _crisis_section(detail: dict, charts: List[Tuple[str, dict]]) -> str:
    ca = detail.get("crisis_alpha")
    title = "一、危机Alpha 防守专项"
    if ca is None:
        return _section(title, _empty("本次回测未计算危机 Alpha 专项"))
    if ca.get("status") != "completed":
        return _section(title, _empty(f"专项状态：{esc(ca.get('status'))}；{esc(ca.get('error') or '')}"))
    d = ca.get("data") or {}
    q = d.get("quadrant") or {}
    body = []

    # 一、核心结论
    cards = "".join([
        _kpi("危机评级", esc(d.get("alpha_rating") or "—"), "基准下跌周中的综合表现"),
        _kpi("危机Alpha占比", pct(q.get("alpha_ratio")), "下跌周上涨占比 − 同步下跌占比",
             cls(q.get("alpha_ratio"))),
        _kpi("危机周胜率", pct(q.get("hit_rate", q.get("up_rate"))),
             f"样本 {q.get('n_crisis', 0)} 周"),
        _kpi("跟跌比", pct(q.get("follow_ratio"))),
        _kpi("下跌周策略均值", pct(q.get("down_avg_s")), f"基准 {pct(q.get('down_avg_b'))}",
             cls(q.get("down_avg_s"))),
    ])
    body.append("<h3>1.1 核心结论</h3>")
    body.append(f'<div class="kpi-grid">{cards}</div>')

    # 二、产品与基准走势
    c = _ca_nav_chart(d)
    if c:
        charts.append(c)
        body.append("<h3>1.2 产品与基准走势</h3>" + _chart(c[0], 320))

    # 三、四档分类
    cats = ["逆势上涨", "跌得少", "跟着跌", "跌更多"]
    counts = q.get("counts") or {}
    avg_ret = q.get("avg_ret") or {}
    cat_rows, cat_cls = [], []
    for cat in cats:
        b = avg_ret.get(cat)
        s = avg_ret.get(cat)
        cat_rows.append([
            esc(cat), str(counts.get(cat, 0)),
            pct_signed(b), pct_signed(s),
            pct_signed(s - b) if s is not None and b is not None else "—",
        ])
        cat_cls.append([None, None, cls(b), cls(s),
                        cls(s - b) if s is not None and b is not None else None])
    total_s = q.get("down_avg_s")
    total_b = q.get("down_avg_b")
    cat_rows.append(["危机Alpha合计", str(q.get("n_down", 0)),
                     pct_signed(total_b), pct_signed(total_s),
                     pct_signed(total_s - total_b) if total_s is not None and total_b is not None else "—"])
    cat_cls.append([None, None, cls(total_b), cls(total_s),
                    cls(total_s - total_b) if total_s is not None and total_b is not None else None])
    body.append("<h3>1.3 危机Alpha四档分类</h3>")
    body.append(_table(["分类", "周数", "基准平均", "策略平均", "超额"],
                       cat_rows, cat_cls))

    # 四、极端下跌周 TOP
    top = d.get("extreme_top") or []
    if top:
        top_rows, top_cls = [], []
        for e in top:
            top_rows.append([esc(e.get("日期")), pct_signed(e.get("基准")),
                             pct_signed(e.get("策略")), pct_signed(e.get("超额")),
                             esc(e.get("分类"))])
            top_cls.append([None, cls(e.get("基准")), cls(e.get("策略")), cls(e.get("超额")), None])
        body.append(f"<h3>1.4 极端下跌周 TOP{len(top)}</h3>")
        body.append(_table(["日期", "基准", "策略", "超额", "分类"], top_rows, top_cls))

    # 五、全周期四象限
    c = _ca_scatter_chart(d)
    if c:
        charts.append(c)
        body.append("<h3>1.5 全周期四象限分布</h3>" + _chart(c[0], 420))

    # 六、深度回撤事件
    events = d.get("deep_dd_events") or []
    if events:
        ev_rows = []
        for e in events:
            ev_rows.append([
                esc(e.get("start")), esc(e.get("end")),
                pct_signed(e.get("benchmark_ret")), pct_signed(e.get("strategy_ret")),
                pct_signed(e.get("excess")), esc(e.get("verdict")),
                pct(e.get("max_dd")), f"{e.get('days', 0)}天",
            ])
        body.append("<h3>1.6 深度回撤事件独立性</h3>")
        body.append(_table(["起点", "终点", "基准涨跌", "策略涨跌", "超额", "判定",
                            "期间最大回撤", "天数"], ev_rows))

    # 七、多宽基一致性
    multi = d.get("multi_bench") or []
    if multi:
        mb_rows, mb_cls = [], []
        for r in multi:
            mb_rows.append([esc(r["name"]) + ("（主）" if r.get("is_main") else ""),
                            str(r.get("n_down", 0)), pct(r.get("alpha_ratio")),
                            pct(r.get("up_rate"))])
            mb_cls.append([None, None, cls(r.get("alpha_ratio")), None])
        body.append("<h3>1.7 多宽基一致性</h3>")
        body.append(_table(["基准", "下跌周数", "危机Alpha", "逆势上涨比例"], mb_rows, mb_cls))
        c = _ca_multi_chart(d)
        if c:
            charts.append(c)
            body.append(_chart(c[0], 280))

    # 八、月度视角
    monthly = d.get("monthly") or {}
    states = monthly.get("states") or {}
    ms_rows, ms_cls = [], []
    for s in ["大涨", "小涨", "小跌", "大跌"]:
        st = states.get(s)
        if not st:
            continue
        ms_rows.append([esc(s), str(st.get("n", 0)), pct_signed(st.get("b_avg")),
                        pct_signed(st.get("s_avg")), pct1(st.get("win_rate"))])
        ms_cls.append([None, None, cls(st.get("b_avg")), cls(st.get("s_avg")), None])
    big_down = monthly.get("big_down") or []
    body.append("<h3>1.8 月度视角</h3>")
    body.append(_table(["市场状态", "月数", "股票月均", "策略月均", "正收益比例"],
                       ms_rows, ms_cls) if ms_rows else "")
    if big_down:
        bd_rows, bd_cls = [], []
        for r in big_down:
            bd_rows.append([esc(r.get("日期")), pct_signed(r.get("基准")),
                            pct_signed(r.get("策略"))])
            bd_cls.append([None, cls(r.get("基准")), cls(r.get("策略"))])
        body.append('<p class="note">大跌月明细（股票月跌 &gt;5%）</p>')
        body.append(_table(["月份", "基准", "策略"], bd_rows, bd_cls))
    body.append(f'<p class="note">下跌月份共 {pct1(monthly.get("hedge_ratio"))} 实现危机对冲（月度口径）。</p>')

    # 九、动态相关性与年度趋势
    c = _ca_corr_chart(d)
    if c:
        charts.append(c)
        body.append("<h3>1.9 动态相关性与年度趋势</h3>" + _chart(c[0], 300))
    annual = d.get("annual") or []
    if annual:
        an_rows, an_cls = [], []
        for r in annual:
            an_rows.append([f"{r.get('年份')}{'*' if not r.get('完整') else ''}",
                            str(r.get("下跌周", 0)), pct(r.get("危机Alpha")),
                            pct_signed(r.get("策略收益")), pct_signed(r.get("基准收益"))])
            an_cls.append([None, None, cls(r.get("危机Alpha")),
                           cls(r.get("策略收益")), cls(r.get("基准收益"))])
        body.append(_table(["年度", "下跌周", "危机Alpha", "策略收益", "基准收益"],
                           an_rows, an_cls))
        body.append('<p class="note">标 * 的年度为数据区间首尾的非完整年度（有效收益周不足 40 周）。</p>')
        c = _ca_annual_bar(d)
        if c:
            charts.append(c)
            body.append(_chart(c[0], 280))

    # 十、代价检验
    cost = d.get("cost") or {}
    cost_rows = [
        ["上涨周拖后腿比例", pct(cost.get("drag"))],
        ["上涨周同步上涨比例", pct(cost.get("sync_up"))],
        ["收益弹性比", num(cost.get("elasticity"))],
        ["下尾相关（下跌周内）", "—" if cost.get("tail_corr") is None else num(cost.get("tail_corr"))],
        ["Q1分位相关（最差25%周）", "—" if cost.get("q1_corr") is None else num(cost.get("q1_corr"))],
        ["上涨周数", str(cost.get("n_up", 0))],
    ]
    if cost:
        body.append("<h3>1.10 代价检验（上涨周表现）</h3>")
        body.append(_table(["指标", "数值"], cost_rows, [[None, None]] * len(cost_rows)))

    # 十一、综合危机保护评分
    score = d.get("score") or {}
    dims = score.get("dimensions") or []
    if dims:
        dim_rows, dim_cls = [], []
        for dim in dims:
            dim_rows.append([esc(dim.get("维度")), esc(dim.get("类型")),
                             pct(dim.get("权重")), num(dim.get("得分")),
                             esc(dim.get("评级")), pct(dim.get("贡献"))])
            dim_cls.append([None, None, None, cls(dim.get("得分")), None, cls(dim.get("贡献"))])
        dim_rows.append(["综合评分", "", "", num(score.get("total_score")),
                         esc(score.get("rating")), ""])
        dim_cls.append([None, None, None, None, None, None])
        body.append("<h3>1.11 综合危机保护评分</h3>")
        body.append(_table(["维度", "类型", "权重", "得分", "评级", "贡献"], dim_rows, dim_cls))

    # 十二、全部下跌周明细
    down = d.get("down_weeks") or []
    if down:
        dn_rows, dn_cls = [], []
        for r in down:
            dn_rows.append([esc(r.get("日期")), pct_signed(r.get("基准收益")),
                            pct_signed(r.get("策略收益")), pct_signed(r.get("超额")),
                            esc(r.get("分类"))])
            dn_cls.append([None, cls(r.get("基准收益")), cls(r.get("策略收益")),
                           cls(r.get("超额")), None])
        body.append(f"<h3>1.12 全部下跌周明细（{len(down)} 周）</h3>")
        body.append(f'<div class="scroll-tbl">{_table(["日期", "基准", "策略", "超额", "分类"], dn_rows, dn_cls)}</div>')
    return _section(title, "".join(body))


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
         "日胜率", "最大回撤", "换手（双边）", "成本/日均净值"], rows, row_cls)


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
        _kpi("年化换手（双边）", "-" if n.get("annual_turnover") is None
             else f"{num(n.get('annual_turnover'))}×/年"),
        _kpi("成本/毛利", pct1(n.get("cost_gross_ratio"))),
        _kpi("假突破磨损", f"{n.get('whipsaw_count', 0)} 次",
             f"{n.get('whipsaw_days', 0)} 个交易日"),
    ])
    return f'<div class="kpi-grid">{cards}</div>'


def _window_note(d: dict, dates: Optional[List[str]]) -> str:
    """披露实际统计窗口；窗口短于回测区间时写明被指数历史长度截断。

    五/六/七/七-B 均对「全体指数求交集」取周（regime_alpha._exposure_block），
    科创50(000688.SH) 等历史较短的指数会把窗口整体截断——不写明用户无从判断
    数字只覆盖了回测区间的一部分。口径与前端 weekWindowText 一致。
    """
    if not dates:
        return ""
    n = len(dates)
    total = ((d.get("meta") or {}).get("n_weeks_labeled"))
    base = f"实际统计窗口 {dates[0]} ~ {dates[n - 1]}（{n} 周）"
    if isinstance(total, int) and n < total:
        return f'<p class="note">{esc(base)}；短于回测区间 {total} 周' \
               "（受宽基指数历史长度限制，仅在该窗口内计算）</p>"
    return f'<p class="note">{esc(base)}</p>'


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
    out += _window_note(d, [w.get("date") for w in (st.get("weeks") or [])])
    if st.get("skipped"):
        out += f'<p class="note">跳过指数：{esc("、".join(st["skipped"]))}</p>'
    return out


def _dispersion_block(d: dict, charts: List[Tuple[str, dict]]) -> str:
    dp = d.get("dispersion") or {}
    chart = _dispersion_chart(dp)
    out = _window_note(d, [s.get("date") for s in (dp.get("series") or [])])
    out += _chart("chart-disp") if chart else ""
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


def _exposure_table(ex: dict, first_col: str, empty_text: str,
                    window_note: str = "") -> str:
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
    out += window_note
    if ex.get("skipped"):
        out += f'<p class="note">跳过指数：{esc("、".join(ex["skipped"]))}</p>'
    return out


def _exposure_block(d: dict) -> str:
    ex = d.get("exposure") or {}
    return _exposure_table(ex, "宽基指数", "宽基样本不足", _window_note(d, ex.get("dates")))


def _quadrant_block(d: dict) -> str:
    """增强A：巨潮四象限纯风格暴露。老记录 payload 无此键 → 空态不报错。"""
    ex = d.get("quadrant_exposure")
    if ex is None:
        return _empty("该记录暂无四象限暴露数据（老记录重算后可用）")
    return _exposure_table(ex, "风格指数", "四象限指数样本不足",
                           _window_note(d, ex.get("dates")))


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


def _robustness_section(detail: dict) -> str:
    """三、稳健性检验：年度/3年滚动/个股集中度/样本内外（纯后处理）。"""
    title = "三、稳健性检验（年度 / 3年滚动 / 集中度 / 样本内外）"
    rb = detail.get("robustness")
    if rb is None:
        return _section(title, _empty("本次回测未计算稳健性检验"))
    if rb.get("status") != "completed":
        return _section(title, _empty(
            f"分析状态：{esc(rb.get('status'))}；{esc(rb.get('error') or '')}"))
    d = rb.get("data") or {}
    cards = "".join([
        _kpi("总收益", pct(d.get("total_return")), value_cls=cls(d.get("total_return"))),
        _kpi("基准收益", pct(d.get("benchmark_return")),
             value_cls=cls(d.get("benchmark_return"))),
        _kpi("正收益年", esc(d.get("years_positive") or "—"),
             "全部年份中正收益年份占比"),
    ])
    body = [f'<div class="kpi-grid">{cards}</div>']

    annual = d.get("annual") or []
    ann_rows, ann_cls = [], []
    for a in annual:
        ann_rows.append([esc(a.get("year")), pct_signed(a.get("return")),
                         pct(a.get("max_drawdown"))])
        ann_cls.append([None, cls(a.get("return")), "neg"])
    body.append("<h3>3.1 年度收益与年内最大回撤</h3>")
    body.append(_table(["年度", "收益", "年内最大回撤"], ann_rows, ann_cls)
                if ann_rows else _empty("无年度数据"))

    r3 = d.get("rolling_3y") or []
    r3_rows, r3_cls = [], []
    for w in r3:
        r3_rows.append([f"{esc(w.get('start'))} ~ {esc(w.get('end'))}",
                        pct_signed(w.get("cagr")), pct(w.get("max_drawdown"))])
        r3_cls.append([None, cls(w.get("cagr")), "neg"])
    body.append("<h3>3.2 3年滚动窗口（CAGR / 窗口内最大回撤）</h3>")
    body.append(_table(["窗口", "CAGR", "窗口内最大回撤"], r3_rows, r3_cls)
                if r3_rows else _empty("区间不足 3 年，无滚动窗口"))

    conc = d.get("concentration") or {}
    top = conc.get("top") or []
    conc_rows, conc_cls = [], []
    for i, t in enumerate(top, 1):
        pnl = t.get("pnl")
        conc_rows.append([str(i), esc(t.get("code")),
                          f"{pnl:,.2f}" if isinstance(pnl, (int, float)) else "—",
                          pct(t.get("share"))])
        conc_cls.append([None, None, cls(pnl), None])
    conc_summary = (
        f"盈利个股 {conc.get('profitable_stocks', 0)} / {conc.get('total_stocks', 0)}　"
        f"前20只占总净利 {pct(conc.get('top20_share'))}　"
        f"总净利 {conc.get('total_realized_pnl', 0):,.2f}"
    )
    body.append("<h3>3.3 个股已实现净 P&L 集中度</h3>")
    body.append(f'<p class="note">{esc(conc_summary)}</p>')
    body.append(_table(["#", "代码", "净 P&L", "占比"], conc_rows, conc_cls)
                if conc_rows else _empty("无成交记录"))

    body.append("<h3>3.4 样本内 / 样本外对比（默认 75/25 自动切分）</h3>")
    oos = d.get("oos")
    if not oos:
        body.append(_empty("区间不足 2 年，无法切分样本内/样本外"))
    else:
        def _seg(label: str, seg: dict) -> list:
            return [
                f"{label}（{esc(str(seg.get('start'))[:4])} ~ {esc(str(seg.get('end'))[:4])}）",
                pct_signed(seg.get("return")), pct_signed(seg.get("cagr")),
                pct(seg.get("max_drawdown")), pct(seg.get("benchmark_return")),
            ]
        in_seg = oos.get("in_sample") or {}
        out_seg = oos.get("out_of_sample") or {}
        oos_rows = [_seg("样本内", in_seg), _seg("样本外", out_seg)]
        oos_cls = [
            [None, cls(in_seg.get("return")), cls(in_seg.get("cagr")), "neg", cls(in_seg.get("benchmark_return"))],
            [None, cls(out_seg.get("return")), cls(out_seg.get("cagr")), "neg", cls(out_seg.get("benchmark_return"))],
        ]
        body.append(f'<p class="note">断点年份：{esc(oos.get("split_year"))}（样本外年化显著低于样本内'
                    " → alpha 随时间衰减；两者接近 → 较稳健）</p>")
        body.append(_table(["区间", "累计收益", "年化", "最大回撤", "基准"],
                           oos_rows, oos_cls))
    return _section(title, "".join(body))


# ---------------------------------------------------------------------------
# 收益分布（月度热力图 / 年度箱线图 / 年收益柱）
# ---------------------------------------------------------------------------
def _boxplot_data(values: list):
    """[min, Q1, median, Q3, max]（与前端 boxplotData 同口径，线性插值分位）。

    样本 <5 无法构成箱体（前端同口径剔除），返回 None。
    """
    if not values or len(values) < 5:
        return None
    import statistics
    v = sorted(values)
    n = len(v)

    def q(p):
        pos = p * (n - 1)
        lo = int(pos)
        hi = min(lo + 1, n - 1)
        frac = pos - lo
        return v[lo] + (v[hi] - v[lo]) * frac

    return [round(v[0], 4), round(q(0.25), 4), round(q(0.5), 4),
            round(q(0.75), 4), round(v[-1], 4)]


def _monthly_heat_chart(rb: dict) -> Optional[Tuple[str, dict]]:
    m = rb.get("monthly_matrix") or {}
    years, months, matrix = m.get("years"), m.get("months"), m.get("matrix")
    if not years or not months or not matrix:
        return None
    data = [[ci, ri, val] for ri, row in enumerate(matrix)
            for ci, val in enumerate(row) if val is not None]
    if not data:
        return None
    abs_max = max(max(abs(d[2]) for d in data), 5)
    # 标签文本预先算好写成纯字符串：
    # ECharts 的 `{@[2]:+.1f}` 格式化在本报告内置的运行时里不解析数值（只渲染出“%”），
    # 逐项 label.formatter 传字面量最稳。
    # 关键：value 必须保持 [x, y, 数值] 三维，第 4 维若塞文本，visualMap 默认维度会取
    # 「最后一维」（即字符串）→ 取不到数值，整张图不着色（颜色条与格子不匹配的根因）。
    items = []
    for ci, ri, val in data:
        txt = f"{val:+.1f}%"
        # 仅最深的红/绿两档用白字，其余浅色档用深字
        strong = abs(val) >= abs_max * 2 / 3
        items.append({
            "name": f"{years[ri]}年 {months[ci]}",
            "value": [ci, ri, round(val, 2)],
            "label": {
                "formatter": txt,
                "fontSize": 12,
                "color": "#ffffff" if strong else "#111827",
            },
        })

    def _edge(k: float) -> float:
        return abs_max * k

    # 分段色阶：图例是离散档位并带区间文字，与格子颜色一一对应；
    # 连续渐变在数据整体偏正时（如本月度矩阵 -0.2%~+15%）会让大多数格子停留在
    # 浅橙中间色，用户观感就是「右侧颜色条与图形对不上」。
    pieces = [
        {"min": -abs_max, "max": -_edge(2 / 3), "color": "#1e7e4f",
         "label": f"≤ -{_edge(2 / 3):.1f}%"},
        {"min": -_edge(2 / 3), "max": -_edge(1 / 3), "color": "#5da283",
         "label": f"-{_edge(2 / 3):.1f}% ~ -{_edge(1 / 3):.1f}%"},
        {"min": -_edge(1 / 3), "max": 0, "color": "#b9d8c8",
         "label": f"-{_edge(1 / 3):.1f}% ~ 0"},
        {"min": 0, "max": _edge(1 / 3), "color": "#f3c7b9",
         "label": f"0 ~ +{_edge(1 / 3):.1f}%"},
        {"min": _edge(1 / 3), "max": _edge(2 / 3), "color": "#d98873",
         "label": f"+{_edge(1 / 3):.1f}% ~ +{_edge(2 / 3):.1f}%"},
        {"min": _edge(2 / 3), "max": abs_max, "color": "#c0392b",
         "label": f"≥ +{_edge(2 / 3):.1f}%"},
    ]
    option = {
        "tooltip": {"trigger": "item", "formatter": "{b}<br/>收益率：{@[2]}%"},
        "grid": {"left": 64, "right": 132, "top": 26, "bottom": 34},
        "xAxis": {"type": "category", "data": months,
                  "axisLabel": {"fontSize": 12}},
        "yAxis": {"type": "category", "data": [str(y) for y in years], "inverse": True,
                  "axisLabel": {"fontSize": 12}},
        "visualMap": {
            "type": "piecewise",
            # 显式指定取数值维（index=2），不依赖 default 的「最后一维」
            "dimension": 2,
            "orient": "vertical", "right": 0, "top": "center",
            "itemWidth": 20, "itemHeight": 14, "itemGap": 6,
            "textStyle": {"fontSize": 11},
            "pieces": pieces,
        },
        "series": [{
            "type": "heatmap", "data": items,
            "encode": {"x": 0, "y": 1, "value": 2},
            # 白色描边分隔每个年度×月份格子，避免相邻色块糊成一片
            "itemStyle": {"borderColor": "#ffffff", "borderWidth": 1},
            "label": {"show": True, "fontSize": 11, "fontWeight": "bold"},
            "emphasis": {"itemStyle": {"shadowBlur": 8,
                                       "shadowColor": "rgba(0,0,0,0.4)"}},
        }],
    }
    return "chart-return-heat", option


def _box_chart(rb: dict, key: str, name: str) -> Optional[Tuple[str, dict]]:
    yearly = rb.get("yearly") or []
    # 先配对（年份, 箱体, 均值）再统一过滤：<5 样本年份整年剔除，
    # categories 与 box_data 始终保持一一对齐，避免箱体与年份轴错位。
    raw_pairs = [
        (str(y.get("year")),
         [round(r * 100, 4) for r in (y.get(key) or [])])
        for y in yearly
    ]
    pairs = [(c, vals) for c, vals in raw_pairs if len(vals) >= 5]
    if not pairs:
        return None
    categories = [c for c, _ in pairs]
    box_data = [_boxplot_data(vals) for _, vals in pairs]
    means = [round(sum(vals) / len(vals), 4) for _, vals in pairs]
    cid = "chart-box-daily" if key == "daily_returns" else "chart-box-month"
    color = "#4a7ec4" if key == "daily_returns" else _GREEN
    option = {
        "tooltip": {
            "trigger": "item",
            "formatter": (
                f"{{b}}（{name}收益）<br/>"
                "最大值：<b>{c5}</b>%<br/>"
                "上四分位 Q3：{c4}%<br/>"
                "中位数：<b>{c3}</b>%<br/>"
                "下四分位 Q1：{c2}%<br/>"
                "最小值：{c1}%"
            ),
        },
        "grid": {"left": 24, "right": 24, "top": 30, "bottom": 30, "containLabel": True},
        "xAxis": {"type": "category", "data": categories},
        # 竖排居中：默认 'end' 会把「日收益率(%)」等轴名挤到左上角被裁切
        "yAxis": {"type": "value", "name": f"{name}收益率(%)",
                  "nameLocation": "middle", "nameRotate": 90, "nameGap": 46,
                  "nameTextStyle": {"fontSize": 12},
                  "splitLine": {"show": True}},
        "series": [
            {"name": "箱体", "type": "boxplot", "data": box_data,
             "itemStyle": {"color": color, "borderColor": color}},
            # 零轴参考线（虚线）：正红负绿的分界
            {"name": "零轴", "type": "line", "markLine": {
                "silent": True, "symbol": "none",
                "lineStyle": {"type": "dashed", "color": "#999", "width": 1},
                "data": [{"yAxis": 0}],
            }},
            # 均值点：金色菱形，常驻可见
            {"name": "均值", "type": "scatter", "data": means,
             "symbol": "diamond", "symbolSize": 10,
             "itemStyle": {"color": "#d4a017"},
             "label": {"show": True, "position": "top", "fontSize": 10,
                       "formatter": "{c}%", "color": "#d4a017"}},
        ],
    }
    return cid, option


def _box_stats_table(rb: dict, key: str) -> str:
    """箱线图下方常驻统计表：与图表同一份计算逻辑，保证口径一致。"""
    yearly = rb.get("yearly") or []
    rows = []
    for y in yearly:
        vals = [round(r * 100, 4) for r in (y.get(key) or [])]
        if len(vals) < 5:
            continue
        b = _boxplot_data(vals)
        mean = sum(vals) / len(vals)
        pos = sum(1 for v in vals if v > 0)
        rows.append((
            str(y.get("year")), len(vals),
            f"{mean:+.2f}", f"{b[2]:+.2f}", f"{b[1]:+.2f}",
            f"{b[3]:+.2f}", f"{b[0]:+.2f}", f"{b[4]:+.2f}",
            f"{pos / len(vals) * 100:.0f}%",
        ))
    if not rows:
        return ""
    head = ["年份", "样本数", "均值", "中位数", "Q1", "Q3", "最小", "最大", "正值占比"]
    body = ["<table class='data-table' style='font-size:12px;margin-top:8px'><thead><tr>"
            + "".join(f"<th>{h}</th>" for h in head) + "</tr></thead><tbody>"]
    for r in rows:
        def cell(v, i):
            # 均值/中位/Q1/Q3/最小/最大 按正负着色（正红负绿，A股口径）
            if 2 <= i <= 7:
                num = float(v)
                color = _RED if num > 0 else _GREEN if num < 0 else "inherit"
                return f"<td style='color:{color}'>{v}%</td>"
            return f"<td>{v}</td>"
        body.append("<tr>" + "".join(cell(v, i) for i, v in enumerate(r)) + "</tr>")
    body.append("</tbody></table>")
    return "".join(body)


def _annual_bar_chart(rb: dict) -> Optional[Tuple[str, dict]]:
    yearly = rb.get("yearly") or []
    data = [{"value": round((y.get("annual_return") or 0) * 100, 2),
             "itemStyle": {"color": _RED if (y.get("annual_return") or 0) >= 0 else _GREEN}}
            for y in yearly]
    if not data:
        return None
    option = {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "grid": {"left": 24, "right": 24, "top": 30, "bottom": 30, "containLabel": True},
        "xAxis": {"type": "category", "data": [str(y.get("year")) for y in yearly]},
        # 竖排居中，避免「年收益(%)」与顶部刻度挤在一起
        "yAxis": {"type": "value", "name": "年收益(%)",
                  "nameLocation": "middle", "nameRotate": 90, "nameGap": 46,
                  "nameTextStyle": {"fontSize": 12},
                  "splitLine": {"lineStyle": {"type": "dashed"}}},
        "series": [{"type": "bar", "data": data, "barMaxWidth": 30}],
    }
    return "chart-return-annual", option


def _persistence_rolling_chart(d: dict) -> Optional[Tuple[str, dict]]:
    """持续性检验：滚动年化曲线（策略 6M/12M vs 基准 12M）。值×100 → %，避免 lambda 不可序列化。"""
    r = d.get("rolling") or {}
    dates = r.get("dates") or []
    if not dates:
        return None

    def scale(vals):
        return [round(v * 100, 2) if v is not None else None for v in (vals or [])]

    return "chart-p-rolling", {
        "tooltip": {"trigger": "axis"},
        "legend": {"itemGap": 20, "textStyle": {"fontSize": 12}},
        "grid": {"left": 70, "right": 24, "bottom": 46, "top": 46},
        "xAxis": {"type": "category", "data": dates,
                  "axisLabel": {"formatter": "__DATEFMT__", "fontSize": 11}},
        # 竖排居中：默认 'end' 会把「滚动年化(%)」挤在左上角被裁切
        "yAxis": {"type": "value", "name": "滚动年化(%)",
                  "nameLocation": "middle", "nameRotate": 90, "nameGap": 52,
                  "nameTextStyle": {"fontSize": 12},
                  "axisLabel": {"fontSize": 11},
                  "splitLine": {"lineStyle": {"type": "dashed"}}},
        "series": [
            {"name": "策略 6M", "type": "line", "data": scale(r.get("strat_6m")),
             "symbol": "none", "lineStyle": {"color": "#5a8fc0", "width": 1.2}},
            {"name": "策略 12M", "type": "line", "data": scale(r.get("strat_12m")),
             "symbol": "none", "lineStyle": {"color": "#e06550", "width": 2}},
            {"name": "基准 12M", "type": "line", "data": scale(r.get("bench_12m")),
             "symbol": "none", "lineStyle": {"color": "#c9a24a", "width": 1.4, "type": "dashed"}},
        ],
    }


def _persistence_section(detail: dict, charts: List[Tuple[str, dict]]) -> str:
    """四、持续性检验（阿尔法衰减 vs 风格钟摆）：分年度超额/风险/交易结构 + 滚动年化。"""
    title = "四、持续性检验（阿尔法衰减 vs 风格钟摆）"
    pp = detail.get("persistence")
    if pp is None:
        return _section(title, _empty("本次回测未计算持续性检验"))
    if pp.get("status") != "completed":
        return _section(title, _empty(
            f"分析状态：{esc(pp.get('status'))}；{esc(pp.get('error') or '')}"))
    d = pp.get("data") or {}
    v = d.get("verdict") or {}
    level_label = {
        "alpha_decay": "疑似阿尔法衰减",
        "style_pendulum": "风格钟摆（市场温度周期）",
        "stable": "稳定",
        "mixed": "信号混合",
        "insufficient": "样本不足",
    }.get(v.get("level"), v.get("level") or "—")
    main_name = ""
    for b in d.get("benchmarks") or []:
        if b.get("is_main"):
            main_name = b.get("name") or b.get("code") or ""
            break
    window = v.get("window_years") or []
    window_text = f"{window[0]}–{window[-1]}（{len(window)} 个满年）" if window else "—"
    cards = "".join([
        _kpi("判定", level_label, value_cls="lv-" + str(v.get("level") or "")),
        _kpi("主基准", esc(main_name) if main_name else "—"),
        _kpi("判定窗口", esc(window_text)),
        _kpi("计算时间", esc(str(pp.get("computed_at") or ""))[:19] or "—"),
    ])
    body = [f'<div class="kpi-grid">{cards}</div>']
    body.append(f'<p class="note">{esc(v.get("summary") or "")}</p>')
    signals = v.get("signals") or []
    if signals:
        s_rows, s_cls = [], []
        for s in signals:
            st = s.get("status")
            s_rows.append([
                f"<b>{esc(s.get('label') or '')}</b>",
                esc(s.get("detail") or "—"),
                f'<span class="sig-{st}">{esc(s.get("word") or "")}</span>' if st in ("bad", "mixed", "good")
                else f'<span>{esc(s.get("word") or "")}</span>',
            ])
            s_cls.append([None, None, None])
        body.append("<h3>4.0 判定信号（判定窗口实测 → 结论）</h3>")
        body.append(_table(["维度", "实测", "判定"], s_rows, s_cls))
    elif v.get("reasons"):
        body.append('<ul class="note">')
        body += [f'<li>{esc(r)}</li>' for r in v["reasons"]]
        body.append('</ul>')
    body.append(f'<p class="note">{esc(v.get("caveat") or "")}</p>')

    years = d.get("years") or []
    m_rows, m_cls = [], []
    for y in years:
        m_rows.append([
            f"{esc(str(y.get('year')))}" + (" *" if y.get("is_partial") else ""),
            pct_signed(y.get("nav_return")), pct(y.get("max_drawdown")),
            num(y.get("sharpe")), num(y.get("calmar")),
            pct(y.get("win_rate")), num(y.get("profit_factor")),
            ("-" if y.get("turnover") is None else f"{num(y.get('turnover'))}×"),
            pct(y.get("cost_ratio")),
        ])
        m_cls.append([None, cls(y.get("nav_return")), "neg", None, None, None, None, None, None])
    body.append("<h3>4.1 分年度指标矩阵（* 未满 200 交易日，不参与判定）</h3>")
    body.append(_table(["年份", "收益", "最大回撤", "夏普", "卡玛", "胜率", "盈亏比", "换手×（双边）", "成本/日均净值"],
                       m_rows, m_cls) if m_rows else _empty("无年度数据"))

    benchs = d.get("benchmarks") or []
    head = ["年份"] + [b.get("name") + ("（主）" if b.get("is_main") else "")
                       + ("（无数据）" if not b.get("available") else "") for b in benchs]
    e_rows, e_cls = [], []
    for y in years:
        row, rc = [str(y.get("year"))], [None]
        for b in benchs:
            ex = y.get("excess") or {}
            val = ex.get(b.get("code"))
            row.append(pct_signed(val) if val is not None else "—")
            rc.append(cls(val) if val is not None else None)
        e_rows.append(row)
        e_cls.append(rc)
    body.append("<h3>4.2 分年度超额收益（vs 组合基准 + 宽基）</h3>")
    body.append(_table(head, e_rows, e_cls) if e_rows else _empty("无超额数据"))

    roll_opt = _persistence_rolling_chart(d)
    if roll_opt:
        charts.append(roll_opt)
        body.append("<h3>4.3 滚动年化收益（策略 6M/12M vs 基准 12M）</h3>")
        body.append(_chart(roll_opt[0], 340))
        body.append('<p class="note">解读：策略 12M 与基准 12M 同步回落→风格钟摆；策略 12M 单独下坠而基准平稳→疑似阿尔法衰减；6M 波动大于 12M 属正常，连续下行段才是关注点。</p>')
    return _section(title, "".join(body))


def _returns_bucket_section(detail: dict, charts: List[Tuple[str, dict]]) -> str:
    rb = detail.get("returns_buckets")
    if not rb:
        return ""
    body = []
    for builder, title, height, stat_key in (
        (_monthly_heat_chart, "月度收益热力图", 420, None),
        (lambda r: _box_chart(r, "daily_returns", "日"), "年度日收益箱线图", 300, "daily_returns"),
        (lambda r: _box_chart(r, "monthly_returns", "月"), "各年月收益箱线图", 300, "monthly_returns"),
        (_annual_bar_chart, "各年年收益柱状图", 300, None),
    ):
        c = builder(rb)
        if c is None:
            continue
        cid, option = c
        charts.append(c)
        body.append(f"<h3>{esc(title)}</h3>{_chart(cid, height)}")
        if stat_key:
            body.append(_box_stats_table(rb, stat_key))
    if not body:
        return ""
    return _section("五、收益分布",
                    "".join(body) + '<p class="note">月度热力图红涨绿跌、数值常驻显示；'
                    "箱线图方块为 Q1~Q3 四分位区间、中线为中位数、金色菱形◆为均值、"
                    "灰色虚线为零轴；下方统计表与图表同口径，正红负绿（A股口径）。"
                    "样本不足 5 个的年份不构成箱体，已整年剔除。</p>")


# ---------------------------------------------------------------------------
# 风控与拦截统计 + 下钻明细
# ---------------------------------------------------------------------------
_GATE_LABELS = {
    "stop_loss": "止损", "take_profit": "止盈", "trailing_stop": "移动止损",
    "max_drawdown": "回撤熔断", "daily_loss": "当日亏损暂停",
    "max_positions_full": "持仓数上限", "halted_buy_strip": "熔断剥单",
    "halted_bar_no_price": "停牌无价", "open_already_holding": "已持仓忽略开仓",
    "insufficient_funds": "资金不足拒单", "order_shrunk": "资金不足缩量",
    "t1_insufficient": "T+1可卖不足", "t1_clamp": "T+1钳量",
    "bridge_rejected": "桥拒单", "risk_recover": "熔断恢复",
    "missing_strategy_risk": "风控未注入", "after_close_block": "收盘后拦单",
    "inflight_skip": "在途跳单",
}


def _gate_label(g: str) -> str:
    return _GATE_LABELS.get(g, g or "—")


def _gate_val(v) -> str:
    if v is None:
        return "—"
    f = float(v)
    if f != 0 and abs(f) < 1:
        return f"{f * 100:.2f}%"
    return f"{f:.4f}"


def _decision_section(detail: dict) -> str:
    summary = detail.get("decision_summary") or []
    decisions = detail.get("decisions") or []
    title = "六、风控与拦截统计（调参可观测性）"
    if not summary and not decisions:
        return _section(title, _empty("本次回测无闸门拦截/触发记录"))
    rows, row_cls = [], []
    for s in sorted(summary, key=lambda x: -x.get("count", 0)):
        rows.append([
            f"{esc(_gate_label(s.get('gate')))}<span class='gate-code'>{esc(s.get('gate'))}</span>",
            esc(s.get("layer")), esc(s.get("action")),
            esc(s.get("param_name") or "—"), _gate_val(s.get("param_value")),
            str(s.get("count", 0)), esc(str(s.get("first_bar_time") or "")[:16]),
            esc(str(s.get("last_bar_time") or "")[:16]), str(s.get("stock_count", 0)),
        ])
        row_cls.append([None, None, None, None, None, None, None, None, None])
    body = ["<h3>闸门聚合（按触发次数降序）</h3>"]
    body.append(_table(["闸门", "层", "动作", "参数", "阈值", "触发次数", "首次", "末次", "涉及股票"],
                       rows, row_cls) if rows else _empty("无聚合记录"))
    # 下钻：按 gate 分组，每个 gate 一个折叠明细
    by_gate = {}
    for e in decisions:
        by_gate.setdefault((e.get("gate"), e.get("param_name")), []).append(e)
    if by_gate:
        body.append("<h3>逐笔下钻明细</h3>")
        for (gate, pname), evs in sorted(by_gate.items(), key=lambda kv: -len(kv[1]))[:20]:
            ev_rows = []
            for e in evs:
                ev_rows.append([
                    esc(str(e.get("bar_time") or "")[:16]),
                    f"#{e.get('strategy_id')}" if e.get("strategy_id") else "组合",
                    esc(e.get("stock_code") or "—"),
                    _gate_val(e.get("param_value")), _gate_val(e.get("actual_value")),
                    esc(e.get("message") or "—"),
                ])
            body.append(
                f"<details><summary>{esc(_gate_label(gate))}"
                f"{(' · ' + esc(str(pname))) if pname else ''} — {len(evs)} 条</summary>"
                + _table(["时间", "策略", "代码", "阈值", "实际", "原因"], ev_rows)
                + "</details>")
    return _section(title, "".join(body))


# ---------------------------------------------------------------------------
# 交易明细
# ---------------------------------------------------------------------------
def _trades_section(detail: dict) -> str:
    trades = detail.get("trades") or []
    title = "七、交易明细"
    if not trades:
        return _section(title, _empty("本次回测无成交记录"))
    rows, row_cls = [], []
    for t in sorted(trades, key=lambda x: str(x.get("bar_time") or "")):
        rows.append([
            esc(str(t.get("bar_time") or "")[:16]),
            esc(t.get("strategy_name") or f"#{t.get('strategy_id')}"),
            esc(t.get("stock_code")), esc(t.get("trade_type")),
            f"{t['price']:.3f}" if t.get("price") is not None else "—",
            str(t.get("quantity", 0)),
            f"{t['amount']:,.2f}" if t.get("amount") is not None else "—",
            f"{t['commission']:,.2f}" if t.get("commission") is not None else "—",
            f"{t['stamp_duty']:,.2f}" if t.get("stamp_duty") is not None else "—",
        ])
        row_cls.append([None, None, None,
                        "pos" if t.get("trade_type") == "BUY" else "neg",
                        None, None, None, None, None])
    body = [f'<div class="scroll-tbl">{_table(["时间", "策略", "代码", "类型", "价格", "数量", "金额", "佣金", "印花税"], rows, row_cls)}</div>']
    body.append(f'<p class="note">共 {len(trades)} 笔成交。买卖红涨绿跌为 A 股口径（买入红、卖出绿）。</p>')
    return _section(title, "".join(body))


# ---------------------------------------------------------------------------
# 策略绩效评估（组合 + 各策略指标）
# ---------------------------------------------------------------------------
_EVAL_ROWS = [
    ("total_return", "总收益", "pct"), ("annual_return", "年化收益", "pct"),
    ("max_drawdown", "最大回撤", "pct"), ("volatility", "年化波动率", "pct"),
    ("sharpe_ratio", "夏普比率", "num"), ("sortino_ratio", "索提诺比率", "num"),
    ("calmar_ratio", "卡玛比率", "num"), ("win_rate", "胜率", "pct"),
    ("profit_factor", "盈亏比", "num"), ("total_trades", "交易次数", "int"),
    ("benchmark_return", "基准收益", "pct"), ("avg_holding_days", "平均持仓天数", "num"),
    ("beta", "Beta", "num"), ("jensen_alpha", "Jensen α(年化)", "pct"),
    ("excess_annual_return", "年化超额", "pct"), ("tracking_error", "跟踪误差", "pct"),
    ("information_ratio", "信息比率", "num"), ("up_capture_ratio", "上行捕获", "pct"),
    ("down_capture_ratio", "下行捕获", "pct"), ("excess_max_drawdown", "超额回撤", "pct"),
    ("annual_turnover", "年化换手（双边）", "turnover"), ("total_cost_ratio", "交易成本/日均净值", "pct"),
    ("avg_position_utilization", "平均仓位利用率", "pct"), ("empty_days_ratio", "空仓日占比", "pct"),
    ("max_single_weight", "最大单票权重", "pct"), ("hhi", "持仓集中度 HHI", "num"),
    ("max_consecutive_loss_days", "最长连亏天数", "int"),
    ("max_consecutive_profit_days", "最长连盈天数", "int"),
    ("worst_daily_return", "最差单日收益", "pct"), ("worst_weekly_return", "最差单周收益", "pct"),
    ("return_skewness", "收益偏度", "num"), ("return_kurtosis", "收益峰度", "num"),
]


def _eval_rows(ev: dict) -> List[List[str]]:
    out = []
    for key, label, kind in _EVAL_ROWS:
        v = ev.get(key)
        if v is None:
            continue
        if kind == "pct":
            out.append([label, pct(v), cls(v)])
        elif kind == "int":
            out.append([label, str(int(v)), ""])
        elif kind == "turnover":
            out.append([label, f"{num(v)}×/年", cls(v)])
        else:
            out.append([label, num(v), cls(v)])
    return out


def _strategy_eval_section(detail: dict) -> str:
    ev = detail.get("evaluations")
    sevs = detail.get("strategy_evaluations") or []
    if not ev and not sevs:
        return ""
    title = "四、策略绩效评估"
    body = []
    if ev:
        cards = "".join([
            _kpi("总收益", pct(ev.get("total_return")), value_cls=cls(ev.get("total_return"))),
            _kpi("年化收益", pct(ev.get("annual_return")), value_cls=cls(ev.get("annual_return"))),
            _kpi("最大回撤", pct(ev.get("max_drawdown")), value_cls="neg"),
            _kpi("夏普比率", num(ev.get("sharpe_ratio"))),
            _kpi("年化波动率", pct(ev.get("volatility"))),
            _kpi("卡玛比率", num(ev.get("calmar_ratio"))),
        ])
        body.append(f'<div class="kpi-grid">{cards}</div>')
        body.append("<h3>组合整体指标</h3>")
        evs = _eval_rows(ev)
        rows = [[label, value] for label, value, _ in evs]
        body.append(_table(["指标", "数值"], rows, [[None, c] for _, _, c in evs]))
    if sevs:
        for s in sevs:
            body.append(f"<h3>策略 #{s.get('strategy_id')}：{esc(s.get('strategy_name'))}</h3>")
            srows = _eval_rows(s)
            rows = [[label, value] for label, value, _ in srows]
            body.append(_table(["指标", "数值"], rows,
                               [[None, c] for _, _, c in srows]) if srows else _empty("无评估数据"))
    return _section(title, "".join(body))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _echarts_source() -> str:
    return _find_echarts_source().read_text(encoding="utf-8")


def _find_echarts_source() -> Path:
    """定位 echarts.min.js 的完整路径。

    dev 环境用 __file__ 推算 web/node_modules（源码仓库内存在）；打包后
    (Nuitka standalone) __file__ 指向 _internal，仓库不存在于 exe 目录，
    因此回退到 argv[0]（启动的 exe）同级目录——build_package.py 会把
    echarts.min.js 复制到分发根目录（与 config.yaml 同级，定位口径与
    core/config.py 一致）。
    """
    candidates: List[Path] = [_ECHARTS_PATH]
    if sys.argv:
        exe_dir = Path(sys.argv[0]).resolve().parent
        candidates.append(
            exe_dir / "web" / "node_modules" / "echarts" / "dist" / "echarts.min.js")
        candidates.append(exe_dir / "echarts.min.js")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "未找到 echarts.min.js（离线报告需内联 ECharts 运行时），已尝试："
        + ", ".join(str(p) for p in candidates))


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
/* 持续性检验判定色（与涨跌红绿解耦，与前端 Backtest.vue 同口径）：
   蓝=良性(稳定/风格钟摆)、金=关注(信号混合)、红=恶化(疑似衰减)、灰=样本不足 */
.kpi-value.lv-alpha_decay{color:#c0392b}.kpi-value.lv-style_pendulum{color:#2b5c8a}
.kpi-value.lv-mixed{color:#8a6d1c}.kpi-value.lv-stable{color:#2b5c8a}
.kpi-value.lv-insufficient{color:#9ca3af}
.sig-bad{color:#c0392b;font-weight:700}.sig-mixed{color:#8a6d1c;font-weight:700}.sig-good{color:#2b5c8a;font-weight:700}
.empty{background:#f9fafb;border:1px dashed #d1d5db;border-radius:6px;padding:18px;text-align:center;color:#6b7280;font-size:13px}
.note{font-size:12px;color:#9ca3af;margin:6px 0}
.spb{background:rgba(180,83,9,.10);border:1px solid #e29b4f;border-left:4px solid #b45309;border-radius:6px;padding:12px 16px;margin:12px 0}
.spb-t{color:#b45309;font-weight:700;font-size:13.5px;margin-bottom:6px}
.spb-t span{font-weight:400;color:#6b7280;font-size:12px;margin-left:6px}
.spb ul{margin:0;padding-left:20px;color:#374151;font-size:12.5px;line-height:1.7}
.chart{width:100%;min-width:760px;margin:8px 0}
.chart-box{width:100%;overflow-x:auto;overflow-y:hidden}
details{margin:8px 0}summary{cursor:pointer;font-size:13px;color:#4b5563}
.scroll-tbl{max-height:520px;overflow:auto}
.gate-code{color:#9ca3af;font-size:10px;margin-left:4px}
pre.json-dump{background:#0f172a;color:#e2e8f0;border-radius:6px;padding:12px;font-size:11px;line-height:1.5;max-height:480px;overflow:auto;white-space:pre-wrap;word-break:break-all}
"""


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
        + _crisis_section(detail, charts)
        + _market_regime_section(detail, charts)
        + _robustness_section(detail)
        + _persistence_section(detail, charts)
        + _strategy_eval_section(detail)
        + _returns_bucket_section(detail, charts)
        + _decision_section(detail)
        + _trades_section(detail)
    )
    options = {cid: opt for cid, opt in charts}
    options_json = json.dumps(options, ensure_ascii=False).replace("</", "<\\/")
    boot = """
<script>
var __CHARTS__ = __OPTIONS__;
// 日期轴标签只显示月-日，避免拥挤
function __datefmt(v){ return v ? String(v).slice(5) : v; }
var __INSTANCES__ = [];
document.querySelectorAll('.chart').forEach(function(el){
  var opt = __CHARTS__[el.id];
  if(!opt) return;
  [opt.xAxis].flat().forEach(function(ax){
    if(ax && ax.axisLabel && ax.axisLabel.formatter === '__DATEFMT__'){
      ax.axisLabel.formatter = __datefmt;
    }
  });
  var ch = echarts.init(el, null, {renderer:'canvas'});
  ch.setOption(opt);
  __INSTANCES__.push(ch);
});
// 窗口尺寸变化/缩放后重算布局，避免 canvas 停在初始化时的宽度导致文字挤压
window.addEventListener('resize', function(){
  __INSTANCES__.forEach(function(ch){ ch.resize(); });
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
