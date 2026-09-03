"""桥正式版单测（0009 切片1）— Mock iQuant 内置函数，纯逻辑测试。

桥 live/bridge/iquant_bridge.py 是 iQuant 策略，iQuant API（passorder / get_trade_detail_data /
get_market_data_ex / download_history_data）在运行时注入策略命名空间（globals()）。测试通过
`br.<api> = fake` 注入 Mock，直接调 _handle/place_order/get_quote 等纯逻辑函数。
"""
import json
import os
import sys
import time

import pytest

# 把 live/bridge 加入 path 以 import 桥模块
BRIDGE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "live", "bridge")
)
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

import iquant_bridge as br  # noqa: E402


def _resp(method, path, headers=None, body=b""):
    """调桥的 _handle，返回 (status, parsed_json)。"""
    raw = br._handle(method, path, headers or {}, body)
    # 解析 HTTP 响应：取 status + JSON body
    head, _, payload = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    data = json.loads(payload.decode("utf-8"))
    return status, data


@pytest.fixture(autouse=True)
def _reset_state():
    """每个测试前重置全局状态。"""
    br.ALLOWED_STOCKS = set()
    br.MAX_VOLUME = 10000
    br.RATE_LIMIT = 1000
    br.RATE_WINDOW = 10
    br._placed.clear()
    br._placing.clear()
    br._requests.clear()
    br._quote_cache.clear()
    br._has_data.clear()
    br._last_download.clear()
    sys.modules.pop("xtquant", None)
    # 清掉可能注入的 fake（从模块命名空间移除，回到 _iq 找不到）
    for name in ("passorder", "get_trade_detail_data", "get_market_data_ex", "download_history_data"):
        br.__dict__.pop(name, None)
    yield


# ---------------- /ping ----------------
def test_ping():
    status, data = _resp("GET", "/ping")
    assert status == 200
    assert data["ok"] is True
    assert data["service"] == "iquant-bridge"


def test_unknown_path_404():
    status, data = _resp("GET", "/nope")
    assert status == 404
    assert data["ok"] is False


# ---------------- /order ----------------
def test_order_dry_run_does_not_call_passorder():
    br.DRY_RUN = True
    br.passorder = lambda *a, **k: pytest.fail("DRY_RUN 不应真调 passorder")
    body = json.dumps({"order_id": "oid1", "code": "600000.SH", "op": "buy",
                       "volume": 100, "price": 0}).encode()
    status, data = _resp("POST", "/order", {}, body)
    assert status == 200
    assert data["ok"] is True
    assert data["dry_run"] is True


def test_order_real_calls_passorder():
    br.DRY_RUN = False
    calls = []
    br.passorder = lambda *a, **k: calls.append(a) or 0
    body = json.dumps({"order_id": "oid1", "code": "600000.SH", "op": "buy",
                       "volume": 100, "price": 0}).encode()
    status, data = _resp("POST", "/order", {}, body)
    assert status == 200
    assert data["ok"] is True
    assert data["passorder_result"] == "0"
    assert len(calls) == 1
    # 前 5 参语义：opType=23(买), orderType=1101, account, code, prType int
    assert calls[0][0] == 23
    assert calls[0][2] == br.ACCOUNT
    assert calls[0][3] == "600000.SH"
    assert isinstance(calls[0][4], int)


def test_order_passes_remark_as_user_order_id():
    """Core oid 经 remark 透传为 passorder 的 userOrderId（写入 m_strRemark）。

    这是订单精确匹配的根基：Core 用确定性 oid（bridge_order_id），桥把它前 20 位
    作为 userOrderId 传给 passorder，柜台回填到委托/成交的 m_strRemark，Core 再按
    remark 精确认领本单，彻底告别 代码+方向+数量 模糊匹配撞到跨会话遗留单。
    passorder 11 参形式：...strategyName, quickTrade, userOrderId, ContextInfo。
    """
    br.DRY_RUN = False
    calls = []
    br.passorder = lambda *a, **k: calls.append(a) or 0
    body = json.dumps({"order_id": "oid-remark-1234567890abcdef",
                       "code": "600000.SH", "op": "buy",
                       "volume": 100, "price": 0,
                       "remark": "oid-remark-1234567890"}).encode()
    _resp("POST", "/order", {}, body)
    assert len(calls) == 1
    # 第 9 参(index 9)= userOrderId；第 10 参(index 10)= ContextInfo。
    # 桥防御性截断到 20 字符（m_strRemark 长度有限），Core 下发的也正是 oid[:20]。
    assert calls[0][9] == "oid-remark-123456789"  # 21 字符入参 → 截断为 20
    assert calls[0][10] is br._CTX


def test_order_rejected_when_passorder_returns_nonzero():
    """回归：passorder 返回非 0（被券商/客户端拒绝）时桥必须返回 ok=False。

    旧逻辑无视返回值恒返回 ok=True，导致 Core 端把已被拒的单当受理，order_ref
    永远匹配不到、却一直挂 submitted。0=已受理（真机验证），其余一律视为拒绝。
    """
    br.DRY_RUN = False
    br.passorder = lambda *a, **k: -1
    body = json.dumps({"order_id": "oid-rej", "code": "600000.SH", "op": "buy",
                       "volume": 100, "price": 0}).encode()
    status, data = _resp("POST", "/order", {}, body)
    assert status == 200
    assert data["ok"] is False
    assert "passorder" in data["error"]


def test_do_place_uses_prtype_14_and_ordertype_1101():
    """0009 切片4：_do_place 固定 prType=14(对手价) + orderType=1101(单股标准)。"""
    br.DRY_RUN = False
    calls = []
    br.passorder = lambda *a, **k: calls.append(a) or 0
    body = json.dumps({"order_id": "oid1", "code": "600000.SH", "op": "buy",
                       "volume": 100, "price": 0}).encode()
    _resp("POST", "/order", {}, body)
    assert calls[0][1] == 1101            # orderType=1101 单股/单账号/普通/按股数
    assert calls[0][4] == 14              # prType=14 对手价(对方一档)
    # SELL 同样用 14/1101
    calls.clear()
    body = json.dumps({"order_id": "oid2", "code": "600000.SH", "op": "sell",
                       "volume": 100, "price": 0}).encode()
    _resp("POST", "/order", {}, body)
    assert calls[0][0] == 24              # opType=24 (sell)
    assert calls[0][1] == 1101
    assert calls[0][4] == 14


def test_dry_run_returns_pr_type():
    """DRY_RUN 返回 params 含 pr_type=14，便于观测/断言。"""
    br.DRY_RUN = True
    body = json.dumps({"order_id": "oid1", "code": "600000.SH", "op": "buy",
                       "volume": 100, "price": 0}).encode()
    status, data = _resp("POST", "/order", {}, body)
    assert data["params"]["pr_type"] == 14


def test_order_missing_code():
    br.DRY_RUN = True
    body = json.dumps({"order_id": "oid1", "op": "buy", "volume": 100}).encode()
    status, data = _resp("POST", "/order", {}, body)
    assert status == 200
    assert data["ok"] is False
    assert "code" in data["error"]


# ---------------- 幂等 ----------------
def test_order_idempotent():
    br.DRY_RUN = False
    calls = []
    br.passorder = lambda *a, **k: calls.append(a) or 0
    body = json.dumps({"order_id": "same", "code": "600000.SH", "op": "buy",
                       "volume": 100, "price": 0}).encode()
    st1, d1 = _resp("POST", "/order", {}, body)
    st2, d2 = _resp("POST", "/order", {}, body)     # 同 order_id 重复
    assert st1 == st2 == 200
    assert d1 == d2
    assert len(calls) == 1                          # passorder 只调一次


# ---------------- 白名单 / 限额 ----------------
def test_whitelist_rejects_other_stock():
    br.ALLOWED_STOCKS = {"600000.SH"}
    br.DRY_RUN = True
    body = json.dumps({"order_id": "oid1", "code": "000001.SZ", "op": "buy",
                       "volume": 100}).encode()
    status, data = _resp("POST", "/order", {}, body)
    assert status == 200
    assert data["ok"] is False
    assert "not allowed" in data["error"]


def test_whitelist_allows_listed_stock():
    br.ALLOWED_STOCKS = {"600000.SH"}
    br.DRY_RUN = True
    body = json.dumps({"order_id": "oid1", "code": "600000.SH", "op": "buy",
                       "volume": 100}).encode()
    status, data = _resp("POST", "/order", {}, body)
    assert status == 200
    assert data["ok"] is True


def test_max_volume_rejects():
    br.MAX_VOLUME = 10000
    br.DRY_RUN = True
    body = json.dumps({"order_id": "oid1", "code": "600000.SH", "op": "buy",
                       "volume": 20000}).encode()
    status, data = _resp("POST", "/order", {}, body)
    assert data["ok"] is False
    assert "volume" in data["error"]


def test_rate_limit_rejects_excess():
    br.RATE_LIMIT = 2
    br.RATE_WINDOW = 10
    br.DRY_RUN = True
    for i in range(2):
        body = json.dumps({"order_id": "oid%d" % i, "code": "600000.SH",
                           "op": "buy", "volume": 100}).encode()
        status, data = _resp("POST", "/order", {}, body)
        assert data["ok"] is True
    # 第三个超限
    body = json.dumps({"order_id": "oid3", "code": "600000.SH",
                       "op": "buy", "volume": 100}).encode()
    status, data = _resp("POST", "/order", {}, body)
    assert data["ok"] is False
    assert "rate" in data["error"]


# ---------------- 查询端点 ----------------
def test_positions_calls_trade_detail():
    class FakePos:
        m_strInstrumentID = "600000.SH"
        m_nVolume = 1000
    br.get_trade_detail_data = lambda acc, typ, dt: [FakePos()]
    status, data = _resp("GET", "/positions", {}, b"")
    assert status == 200
    assert data["ok"] is True
    assert data["data"][0]["instrument"] == "600000.SH"


def test_query_orders_includes_remark():
    """/orders 每行回带 m_strRemark（= 下单时传的 userOrderId），供 Core 按 remark 精确认领。"""
    class FakeOrder:
        m_strOrderRef = "ref-1"
        m_strOrderSysID = "sys-1"
        m_strInstrumentID = "600000"
        m_strExchangeID = "SH"
        m_nDirection = 48
        m_dLimitPrice = 9.3
        m_dTradedPrice = 9.3
        m_nVolumeTotalOriginal = 600
        m_nVolumeTraded = 600
        m_nOrderStatus = 56
        m_strSource = "BRIDGE"
        m_strOrderStrategyType = ""
        m_strInsertTime = "100000"
        m_strInsertDate = "20260813"
        m_dCancelAmount = 0
        m_strRemark = "abc123def456ghij7890"
    br.get_trade_detail_data = lambda acc, typ, dt: [FakeOrder()]
    status, data = _resp("GET", "/orders", {}, b"")
    assert status == 200
    assert data["ok"] is True
    assert data["data"][0]["remark"] == "abc123def456ghij7890"
    assert data["data"][0]["order_ref"] == "ref-1"


def test_query_deals_includes_remark():
    """/deals 每行回带 m_strRemark，Core 按 order_ref（聚合键）回填，但 remark 可用于核对。"""
    class FakeDeal:
        m_strOrderRef = "ref-1"
        m_strOrderSysID = "sys-1"
        m_strTradeID = "trade-1"
        m_strInstrumentID = "600000"
        m_strExchangeID = "SH"
        m_nDirection = 48
        m_dPrice = 9.25
        m_nVolume = 600
        m_dTradeAmount = 5550.0
        m_dCommission = 1.39
        m_strTradeTime = "100001"
        m_strTradeDate = "20260813"
        m_strSource = "BRIDGE"
        m_strOrderStrategyType = ""
        m_strRemark = "abc123def456ghij7890"
    br.get_trade_detail_data = lambda acc, typ, dt: [FakeDeal()]
    status, data = _resp("GET", "/deals", {}, b"")
    assert status == 200
    assert data["ok"] is True
    assert data["data"][0]["remark"] == "abc123def456ghij7890"


# ---------------- /quote 行情缓存 ----------------
def _make_fake_df():
    import pandas as pd
    return pd.DataFrame(
        {
            "open": [9.28, 9.29],
            "high": [9.29, 9.31],
            "low": [9.28, 9.30],
            "close": [9.29, 9.31],
            "volume": [3700, 727],
            "time": [1785895560000, 1785895680000],
        },
        index=pd.Index(["20260805100600", "20260805100800"], name="stime"),
    )


def test_quote_fetches_and_returns_bars():
    calls = []
    br.get_market_data_ex = lambda *a, **k: calls.append(k) or {"600000.SH": _make_fake_df()}
    status, data = _resp("GET", "/quote?code=600000.SH&period=1m&count=10", {}, b"")
    assert status == 200
    assert data["ok"] is True
    bars = data["data"]["600000.SH"]
    assert isinstance(bars, list) and len(bars) == 2
    assert bars[-1]["close"] == 9.31
    assert bars[-1]["stime"] == "20260805100800"
    assert calls[0]["period"] == "1m"


def test_quote_cache_hit_within_ttl():
    br.QUOTE_CACHE_TTL = 100          # 拉大 TTL，保证两次调用在窗口内，避免跨秒 flaky
    calls = []
    br.get_market_data_ex = lambda *a, **k: calls.append(1) or {"600000.SH": _make_fake_df()}
    # 第一次拉取
    st, d1 = _resp("GET", "/quote?code=600000.SH&period=1m&count=10", {}, b"")
    assert len(calls) == 1
    # TTL 内第二次 → 读缓存，不再调 get_market_data_ex
    st, d2 = _resp("GET", "/quote?code=600000.SH&period=1m&count=10", {}, b"")
    assert d1["data"] == d2["data"]      # bar 数据一致
    assert d2["cached"] is True          # 第二次命中缓存
    assert len(calls) == 1               # get_market_data_ex 只调一次


# ---------------- 下载窗口 / 读空重试（2026-09-03 首拉 178 只 ETF 30m 全 empty 复盘） ----------------
def _install_fake_xtquant():
    """注入 fake xtquant 模块——桥内 `from xtquant import xtdata` 按名字查 sys.modules。"""
    import types

    class FakeXtdata(object):
        def __init__(self):
            self.downloads = []
            self.read_results = []   # 每次读弹出队首；耗尽后重复最后一项
            self.reads = 0
            self.df = _make_fake_df()

        def download_history_data(self, code, period, start, end):
            self.downloads.append((code, period, start, end))

        def get_market_data_ex(self, *a, **k):
            self.reads += 1
            if len(self.read_results) > 1:
                return self.read_results.pop(0)
            if self.read_results:
                return self.read_results[0]
            return {"600000.SH": self.df}

    fake = FakeXtdata()
    mod = types.ModuleType("xtquant")
    mod.xtdata = fake
    sys.modules["xtquant"] = mod
    return fake


def test_history_days_scales_with_period_and_count():
    """下载窗口按 (period, count)放大：30m×500 根 ≈ 63 个交易日 → ~100 自然日。

    旧逻辑恒 HISTORY_DAYS=30（≈22 个交易日 ≈176 根 30m），预热要 500 根永远拉不满。
    小 count 走 HISTORY_DAYS 兜底，不无谓加大下载窗口。
    """
    assert br._history_days("30m", 500) >= 100
    assert br._history_days("1m", 10) == br.HISTORY_DAYS
    assert br._history_days("1d", 10) == br.HISTORY_DAYS


def test_fetch_quote_xtdata_downloads_scaled_window():
    fake = _install_fake_xtquant()
    try:
        st, data = _resp("GET", "/quote?code=600000.SH&period=30m&count=500", {}, b"")
        assert data["ok"] is True
        assert len(fake.downloads) == 1
        code, period, start, end = fake.downloads[0]
        assert (code, period) == ("600000.SH", "30m")
        assert start == br._history_start(br._history_days("30m", 500))
    finally:
        sys.modules.pop("xtquant", None)


def test_fetch_quote_retries_when_first_read_empty():
    """首拉读空重试：download_history_data 落盘有延迟（真机 178 只 ETF 首轮全
    empty、60s 后陆续到位），下载后立即读会读空——短重试把数据等回来。"""
    fake = _install_fake_xtquant()
    fake.read_results = [{}, {}, {"600000.SH": _make_fake_df()}]
    br.EMPTY_RETRY_INTERVAL = 0      # 测试不真睡
    try:
        st, data = _resp("GET", "/quote?code=600000.SH&period=30m&count=500", {}, b"")
        assert data["ok"] is True
        assert len(data["data"]["600000.SH"]) == 2
        assert fake.reads == 3       # 空×2 → 重试到第 3 次读到
    finally:
        sys.modules.pop("xtquant", None)


def test_fetch_quote_no_retry_once_session_has_data():
    """会话内该 (code, period) 读到过数据后不再重试——稳态零重试开销，
    真没数据（停牌/新代码）也不拖慢每轮拉取。"""
    fake = _install_fake_xtquant()
    fake.read_results = [{}]         # 恒空
    br.EMPTY_RETRY_INTERVAL = 0
    br._has_data.add(("600000.SH", "30m"))
    try:
        st, data = _resp("GET", "/quote?code=600000.SH&period=30m&count=500", {}, b"")
        assert data["ok"] is False   # 读空 → 无数据
        assert fake.reads == 1       # 只读一次，不重试
    finally:
        sys.modules.pop("xtquant", None)


def test_fetch_quote_download_dedup_within_interval():
    """同 (code, period) 在 DOWNLOAD_MIN_INTERVAL 内不重复触发下载
    （真机 download 走客户端数据通道，同轮双拉浪费且加剧落盘延迟）。"""
    fake = _install_fake_xtquant()
    br.DOWNLOAD_MIN_INTERVAL = 1000
    try:
        _resp("GET", "/quote?code=600000.SH&period=1m&count=10", {}, b"")
        # 换 count 避开 1s 读缓存；同 (code, period) 仍不应重下
        _resp("GET", "/quote?code=600000.SH&period=1m&count=20", {}, b"")
        assert len(fake.downloads) == 1
    finally:
        sys.modules.pop("xtquant", None)


def test_fetch_quote_fallback_retries_when_first_empty():
    """ContextInfo 兜底路径同样受读空重试保护（同源本地数据，落盘延迟一致）。"""
    br.EMPTY_RETRY_INTERVAL = 0
    results = [{}, {"600000.SH": _make_fake_df()}]

    def fake_fn(*a, **k):
        return results.pop(0) if results else {}

    br.get_market_data_ex = fake_fn
    st, data = _resp("GET", "/quote?code=600000.SH&period=1m&count=10", {}, b"")
    assert data["ok"] is True
    assert len(data["data"]["600000.SH"]) == 2
