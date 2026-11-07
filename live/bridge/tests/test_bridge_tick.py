"""桥 tick 运行模型单测（2026-11-06：init 返回 + schedule_run 驱动）。

背景：新版 QMT 客户端里 `init` 不返回会导致策略线程挂起——端口 LISTEN、TCP
三次握手成功，但循环体从不执行，请求全部停在 CLOSE_WAIT（现场只有一行
"bridge listening"）。新运行模型 = `init` 只绑定端口 + 注册 `C.schedule_run(tick, ...)`
后立即返回，由定时回调驱动**有界** accept/读/写循环。

本测试不依赖 QMT、不连任何交易终端：
- 用假 ContextInfo 驱动 `init`，取出注册的回调，手工反复调用 `tick()`；
- 用真实本机 socket 连桥发 HTTP/1.1 请求，断言拿到 200 + 预期 JSON；
- `/order` 只走 `DRY_RUN=True` 分支，绝不触发真实下单。

两个变体（仿真 8790 / 实盘 8791）都跑同一套断言（PORT 换成空闲端口）。
"""
import json
import os
import socket
import sys
import time

import pytest

BRIDGE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

import iquant_bridge as sim  # noqa: E402
import iquant_bridge_live as live  # noqa: E402


class FakeContext(object):
    """最小 QMT ContextInfo 替身：只提供桥用到的 set_account / schedule_run。"""

    def __init__(self):
        self.accID = None
        self.account = None
        self.scheduled = []

    def set_account(self, acc):
        self.account = acc

    def schedule_run(self, func, start, end, interval, tag):
        self.scheduled.append((func, start, end, interval, tag))


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


@pytest.fixture(params=[sim, live], ids=["sim", "live"])
def bridge(request, monkeypatch):
    """启动桥（假 C）+ 空闲端口；每个用例结束后释放 socket，避免端口泄漏。"""
    mod = request.param
    monkeypatch.setattr(mod, "PORT", _free_port())
    monkeypatch.setattr(mod, "TICK_BUDGET_SECONDS", 0.1)   # 让 tick 在测试里快速返回
    mod._clients.clear()
    mod._last_log.clear()
    mod._placed.clear()
    mod._requests.clear()
    mod._tick_count = 0
    mod._tick_served = 0
    mod._last_heartbeat = 0.0
    mod._shutdown_requested = False
    mod._ctx_quote_logged = False
    mod._quote_cache.clear()
    mod._has_data.clear()
    mod._CAL_CACHE["ts"] = 0.0
    mod._CAL_CACHE["dates"] = []
    ctx = FakeContext()
    mod.init(ctx)
    yield mod, ctx
    mod._reset_connections()
    if mod._listen_sock is not None:
        mod._close_quietly(mod._listen_sock)
        mod._listen_sock = None


def _call(mod, raw, timeout=5.0):
    """真实 socket 发原始 HTTP 请求；反复 tick() 直到响应收完，返回 (status, json)。"""
    s = socket.create_connection(("127.0.0.1", mod.PORT), timeout=timeout)
    s.settimeout(0.2)
    try:
        s.sendall(raw)
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            mod.tick()
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
            if b"\r\n\r\n" in buf:
                head, _, body = buf.partition(b"\r\n\r\n")
                for line in head.split(b"\r\n")[1:]:
                    if line.lower().startswith(b"content-length:"):
                        if len(body) >= int(line.split(b":")[1].strip()):
                            return int(head.split(b" ")[1]), json.loads(body.decode("utf-8"))
                continue
        raise AssertionError("no complete HTTP response; got %r" % buf[:200])
    finally:
        s.close()


# ---------------- 运行模型 ----------------
def test_shutdown_releases_port_after_response(bridge):
    """GET /shutdown：先完整回 ok，再于本次 tick 末关闭监听与连接表。"""
    mod, _ctx = bridge
    status, data = _call(mod, b"GET /shutdown HTTP/1.1\r\nHost: x\r\n\r\n")
    assert status == 200
    assert data["ok"] is True and data["shutting_down"] is True
    assert mod._listen_sock is None      # 端口已释放
    assert not mod._clients              # 连接表已清空
    mod.tick()                           # 释放后继续 tick 不能抛异常


def test_stop_hook_releases_port_and_never_raises(bridge):
    """stop() 钩子释放端口；QMT 不调用也无影响，重复调用不抛异常。"""
    mod, _ctx = bridge
    mod.stop(None)
    assert mod._listen_sock is None
    mod.stop(None)                       # 幂等，第二次也不能抛异常


def test_init_returns_and_registers_tick(bridge):
    """init 注册 schedule_run 后返回；回调即 tick，周期为 TICK_INTERVAL。"""
    mod, ctx = bridge
    assert ctx.accID == mod.ACCOUNT
    assert len(ctx.scheduled) == 1
    func, start, end, interval, tag = ctx.scheduled[0]
    assert func is mod.tick
    assert interval == mod.TICK_INTERVAL
    assert tag == mod.TICK_TAG
    assert end == -1              # 与 QMT 官方示例同形参：run forever


def test_init_without_schedule_run_returns_instead_of_blocking(monkeypatch):
    """没有 schedule_run 时也必须立即返回（打印 FATAL），绝不能退回 while 死循环。"""
    class NoTimerContext(object):
        def set_account(self, acc):
            pass

    monkeypatch.setattr(sim, "PORT", _free_port())
    sim._clients.clear()
    t0 = time.time()
    sim.init(NoTimerContext())
    try:
        assert time.time() - t0 < 2.0
        assert sim._listen_sock is not None      # 端口仍绑定，等用户修 schedule_run
    finally:
        sim._reset_connections()
        sim._close_quietly(sim._listen_sock)
        sim._listen_sock = None


def test_tick_is_bounded_when_idle(bridge):
    """空闲时 tick 快速返回，不会长时间占用策略线程。"""
    mod, _ctx = bridge
    t0 = time.time()
    for _ in range(5):
        mod.tick()
    assert time.time() - t0 < 1.5


def test_first_tick_prints_heartbeat_with_timestamp_and_count(bridge, capsys):
    """首 tick 即打印带时间戳与 tick 计数的心跳，供用户在控制台确认定时器在跑。"""
    mod, _ctx = bridge
    mod.tick()
    out = capsys.readouterr().out
    assert "tick#1 alive" in out
    assert "served_total=" in out


# ---------------- 端点（协议零变更） ----------------
def test_ping_returns_200_and_status_fields(bridge):
    mod, _ctx = bridge
    status, data = _call(mod, b"GET /ping HTTP/1.1\r\nHost: bridge\r\n\r\n")
    assert status == 200
    assert data["ok"] is True
    assert data["service"].startswith("iquant-bridge")
    assert data["port"] == mod.PORT
    assert data["account"] == mod.ACCOUNT
    assert data["account_type"] == mod.ACCOUNT_TYPE
    assert data["dry_run"] is False
    # 平台侧 HttpBridgeDispatcher 依赖的既有字段
    assert data["endpoints"] == list(mod.ENDPOINTS)


def test_positions_returns_200_and_rows(bridge, monkeypatch):
    mod, _ctx = bridge

    class FakePos(object):
        m_strInstrumentID = "600000"
        m_strExchangeID = "SH"
        m_nVolume = 1000
        m_nCanUseVolume = 800
        m_nYesterdayVolume = 1000
        m_nOnRoadVolume = 0
        m_dMarketValue = 9300.0

    monkeypatch.setattr(mod, "get_trade_detail_data",
                        lambda acc, typ, kind: [FakePos()], raising=False)
    status, data = _call(mod, b"GET /positions HTTP/1.1\r\n\r\n")
    assert status == 200
    assert data["ok"] is True
    row = data["data"][0]
    assert row["instrument"] == "600000"
    assert row["volume"] == 1000
    assert row["available"] == 800


def test_inf_and_nan_values_are_normalized_to_null(bridge, monkeypatch):
    """QMT 对停牌/异常持仓会返回 inf/nan/DBL_MAX：桥必须输出 null 并可严格序列化。"""
    mod, _ctx = bridge

    class FakePos(object):
        m_strInstrumentID = "601989"
        m_strExchangeID = "SH"
        m_nVolume = 100
        m_nCanUseVolume = 100
        m_nYesterdayVolume = 100
        m_nOnRoadVolume = 0
        m_dMarketValue = float("inf")        # 实测脏值：601989

    class FakeAcct(object):
        m_dAvailable = 1.7976931348623157e+308   # DBL_MAX 哨兵
        m_dAssetBalance = float("nan")
        m_dStockValue = 0.0
        m_dBalance = 1e9
        m_dFrozenCash = float("-inf")
        m_dCommission = 1.5
        m_dPositionProfit = 0.0

    monkeypatch.setattr(
        mod, "get_trade_detail_data",
        lambda acc, typ, kind: [FakePos()] if kind == "POSITION" else [FakeAcct()],
        raising=False)

    status, pos = _call(mod, b"GET /positions HTTP/1.1\r\n\r\n")
    assert status == 200
    assert pos["data"][0]["market_value"] is None
    json.dumps(pos, allow_nan=False)          # 严格序列化不得抛异常

    status, acct = _call(mod, b"GET /account HTTP/1.1\r\n\r\n")
    assert status == 200
    row = acct["data"][0]
    assert row["available"] == 0              # DBL_MAX → 无值 → 0
    assert row["total_asset"] == 1e9          # nan → m_dBalance 兜底
    assert row["frozen_cash"] == 0            # -inf → 0
    assert row["commission"] == 1.5           # 正常值不受影响
    json.dumps(acct, allow_nan=False)


def test_positions_reports_cost_with_fallbacks(bridge, monkeypatch):
    """持仓成本：m_dOpenPrice 优先，缺失回退 m_dAvgPrice 等，全缺失 → null。

    桥原先行里没有 cost，平台只能用 market_value/volume 推算；停牌票 QMT 市值
    返回 Infinity（归一成 None），推算失败 → 成本 0 → 止损/止盈失效。
    """
    mod, _ctx = bridge

    class PosOpen(object):
        m_strInstrumentID = "000001"
        m_strExchangeID = "SZ"
        m_nVolume = 100
        m_nCanUseVolume = 100
        m_nYesterdayVolume = 100
        m_nOnRoadVolume = 0
        m_dMarketValue = 1183.0
        m_dOpenPrice = 11.83
        m_dAvgPrice = 99.99          # 优先 m_dOpenPrice

    class PosAvgOnly(object):
        m_strInstrumentID = "601827"
        m_strExchangeID = "SH"
        m_nVolume = 100
        m_nCanUseVolume = 100
        m_nYesterdayVolume = 100
        m_nOnRoadVolume = 0
        m_dMarketValue = None
        m_dAvgPrice = 7.9

    class PosNoCost(object):
        m_strInstrumentID = "000939"
        m_strExchangeID = "SZ"
        m_nVolume = 93300
        m_nCanUseVolume = 93300
        m_nYesterdayVolume = 93300
        m_nOnRoadVolume = 0
        m_dMarketValue = float("inf")   # 停牌票：市值不可用，且无成本字段
        m_dOpenPrice = 1.7976931348623157e+308   # DBL_MAX 哨兵 → 无值

    monkeypatch.setattr(
        mod, "get_trade_detail_data",
        lambda acc, typ, kind: [PosOpen(), PosAvgOnly(), PosNoCost()],
        raising=False)

    status, data = _call(mod, b"GET /positions HTTP/1.1\r\n\r\n")
    assert status == 200
    rows = {r["instrument"]: r for r in data["data"]}
    assert rows["000001"]["cost"] == 11.83
    assert rows["601827"]["cost"] == 7.9
    assert rows["000939"]["cost"] is None       # 不伪造 0
    json.dumps(data, allow_nan=False)

    status, ping = _call(mod, b"GET /ping HTTP/1.1\r\n\r\n")
    assert status == 200
    assert ping["pos_missing_cost"] == 1        # 上次 /positions 缺成本行数


def _quote_frame(labels, closes):
    """真实客户端的返回形状：index 名为 stime，列只有显式请求的字段。"""
    import pandas as pd
    return pd.DataFrame(
        [{"open": 1.0, "high": 2.0, "low": 0.5, "close": c,
          "volume": 100, "amount": 150.0} for c in closes],
        index=pd.Index(labels, name="stime"))


def test_quote_uses_contextinfo_when_globals_lacks_it(bridge, monkeypatch, capsys):
    """ContextInfo 取数：显式字段 + subscribe=True + dividend_type=follow。"""
    mod, _ctx = bridge
    seen = []

    class CtxWithQuote(object):
        def get_market_data_ex(self, fields, stocks, **kwargs):
            seen.append((list(fields), kwargs))
            return {stocks[0]: _quote_frame(["20261106103000"], [1.5])}

    assert "get_market_data_ex" not in mod.__dict__
    monkeypatch.setattr(mod, "_CTX", CtxWithQuote())

    status, data = _call(mod, b"GET /quote?code=600000.SH&period=1m&count=3 HTTP/1.1\r\n\r\n")
    assert status == 200
    assert data["ok"] is True
    bar = data["data"]["600000.SH"][0]
    assert bar["stime"] == "20261106103000"
    assert bar["time"] == "20261106103000"
    assert bar["close"] == 1.5 and bar["amount"] == 150.0
    assert seen[0][0] == mod.CTX_FIELDS
    assert seen[0][1]["subscribe"] is True
    assert seen[0][1]["dividend_type"] == "follow"
    out = capsys.readouterr().out
    assert "quote via ContextInfo ok" in out
    assert "rows_before=1 rows_after=1" in out     # 取数 → 序列化 行数自证


def test_account_stock_uses_account_table_only(bridge, monkeypatch):
    """普通账户：只查 ACCOUNT，字段映射保持原有属性名。"""
    mod, _ctx = bridge
    monkeypatch.setattr(mod, "ACCOUNT_TYPE", "STOCK")
    calls = []

    class Acct(object):
        m_dAvailable = 1000.0
        m_dAssetBalance = 5000.0
        m_dStockValue = 4000.0
        m_dBalance = 5000.0

    def fake(acc, typ, kind):
        calls.append((typ, kind))
        return [Acct()] if kind == "ACCOUNT" else []

    monkeypatch.setattr(mod, "get_trade_detail_data", fake, raising=False)
    status, data = _call(mod, b"GET /account HTTP/1.1\r\n\r\n")
    assert status == 200
    row = data["data"][0]
    assert row["account_source"] == "ACCOUNT"
    assert row["total_asset"] == 5000.0
    assert row["market_value"] == 4000.0
    assert ("STOCK", "ASSET") not in calls


def test_account_credit_merges_asset_row(bridge, monkeypatch):
    """信用账户：ACCOUNT 的 0 值被 ASSET 的非零字段补上，并标注来源。"""
    mod, _ctx = bridge
    monkeypatch.setattr(mod, "ACCOUNT_TYPE", "CREDIT")

    class Acct(object):
        m_dAvailable = 200000000.0
        m_dAssetBalance = 0.0
        m_dStockValue = 0.0
        m_dBalance = 200000000.0

    class Asset(object):
        m_dTotalAsset = 205000000.0
        m_dMarketValue = 5000000.0

    def fake(acc, typ, kind):
        if kind == "ACCOUNT":
            return [Acct()]
        if kind == "ASSET":
            return [Asset()]
        return []

    monkeypatch.setattr(mod, "get_trade_detail_data", fake, raising=False)
    status, data = _call(mod, b"GET /account HTTP/1.1\r\n\r\n")
    assert status == 200
    row = data["data"][0]
    assert row["account_source"] == "ACCOUNT+ASSET"
    assert row["available"] == 200000000.0
    assert row["total_asset"] == 205000000.0
    assert row["market_value"] == 5000000.0
    assert row["raw"]["ASSET"]["m_dTotalAsset"] == 205000000.0
    json.dumps(data, allow_nan=False)


def test_account_fields_default_to_zero_when_both_sources_empty(bridge, monkeypatch):
    """两条来源都没有该字段时置 0（不是 null），raw 保留原始字段名。"""
    mod, _ctx = bridge
    monkeypatch.setattr(mod, "ACCOUNT_TYPE", "CREDIT")

    class Acct(object):
        m_dBalance = 0.0

    monkeypatch.setattr(mod, "get_trade_detail_data",
                        lambda acc, typ, kind: [Acct()] if kind == "ACCOUNT" else [],
                        raising=False)
    status, data = _call(mod, b"GET /account HTTP/1.1\r\n\r\n")
    row = data["data"][0]
    assert row["total_asset"] == 0
    assert row["market_value"] == 0
    assert row["raw"]["ACCOUNT"]["m_dBalance"] == 0.0
    json.dumps(data, allow_nan=False)


def test_bars_survive_duplicate_stime_index_and_column(bridge):
    """实机脏形状：index 与列都叫 stime（旧 reset_index() 抛错 → 空数组）。"""
    mod, _ctx = bridge
    import pandas as pd
    df = pd.DataFrame(
        [{"stime": 1762393800000, "open": 11.0, "high": 12.0, "low": 10.0,
          "close": 11.83, "volume": 100.0, "amount": 1183.0}],
        index=pd.Index([1762393800000], name="stime"))
    bars = mod._df_to_bars(df)
    assert len(bars) == 1
    assert bars[0]["stime"] == 1762393800000
    assert bars[0]["close"] == 11.83
    json.dumps(bars, allow_nan=False)


def test_bars_conversion_failure_is_logged(bridge, monkeypatch, capsys):
    """无法转成 bar 时不许静默：节流日志必须带异常类型。"""
    mod, _ctx = bridge

    class NotAFrame(object):
        columns = None

        def __len__(self):
            return 3

    bars = mod._df_to_bars(NotAFrame())
    assert bars == []
    assert "rows to bars FAIL" in capsys.readouterr().out


def test_ping_reports_ctx_ok(bridge, monkeypatch):
    """/ping 的 ctx_ok 自证 _CTX 是否真的持有 get_market_data_ex。"""
    mod, _ctx = bridge
    status, data = _call(mod, b"GET /ping HTTP/1.1\r\n\r\n")
    assert status == 200
    assert data["ctx_ok"] is False            # FakeContext 没有取数方法

    class CtxWithQuote(object):
        def get_market_data_ex(self, *args, **kwargs):
            return {}

    monkeypatch.setattr(mod, "_CTX", CtxWithQuote())
    status, data = _call(mod, b"GET /ping HTTP/1.1\r\n\r\n")
    assert data["ctx_ok"] is True


def test_quote_logs_when_both_paths_fail(bridge, monkeypatch, capsys):
    """两条取数路径都失败时不静默：返回 ok:false 且打 ContextInfo/xtdata 日志。"""
    mod, _ctx = bridge

    class BrokenCtx(object):
        def get_market_data_ex(self, *args, **kwargs):
            raise RuntimeError("quote service down")

    monkeypatch.setattr(mod, "_CTX", BrokenCtx())
    monkeypatch.setattr(mod, "EMPTY_READ_RETRIES", 1)

    status, data = _call(mod, b"GET /quote?code=600000.SH&period=1m&count=3 HTTP/1.1\r\n\r\n")
    assert status == 200
    assert data["ok"] is False
    assert "no data" in data["error"]
    out = capsys.readouterr().out
    assert "ContextInfo FAIL" in out


def test_quote_1d_uses_interval_and_truncates(bridge, monkeypatch):
    """1d 不靠 count：改用 start_time 区间 + count=-1，再截尾到请求条数。"""
    mod, _ctx = bridge
    seen = []

    class CtxWithDaily(object):
        def get_market_data_ex(self, fields, stocks, **kwargs):
            seen.append(kwargs)
            return {stocks[0]: _quote_frame(
                ["20261101", "20261102", "20261103"], [1.0, 1.5, 2.0])}

    monkeypatch.setattr(mod, "_CTX", CtxWithDaily())
    status, data = _call(mod, b"GET /quote?code=600000.SH&period=1d&count=2 HTTP/1.1\r\n\r\n")
    assert status == 200
    assert data["ok"] is True
    assert len(data["data"]["600000.SH"]) == 2      # 区间返回 3 行，截尾到 2
    assert seen[0]["count"] == -1
    assert seen[0]["start_time"].isdigit() and len(seen[0]["start_time"]) == 8


def test_thin_daily_read_is_reported(bridge, monkeypatch, capsys):
    """区间拉取仍只有 1 行时必须打可辨识文案，不能静默。"""
    mod, _ctx = bridge

    class CtxWithOneDailyRow(object):
        def get_market_data_ex(self, fields, stocks, **kwargs):
            return {stocks[0]: _quote_frame(["20261106"], [11.8])}

    monkeypatch.setattr(mod, "_CTX", CtxWithOneDailyRow())
    status, data = _call(mod, b"GET /quote?code=601989.SH&period=1d&count=5 HTTP/1.1\r\n\r\n")
    assert status == 200
    assert data["ok"] is True
    assert len(data["data"]["601989.SH"]) == 1
    assert "1d history unavailable, rows=1" in capsys.readouterr().out


def test_calendar_uses_contextinfo_when_available(bridge, monkeypatch):
    """/calendar 主路径走 ContextInfo.get_trading_dates，且只取最近两年。"""
    mod, _ctx = bridge
    seen = []

    def _ms(y, m, d):
        return int(time.mktime((y, m, d, 12, 0, 0, 0, 0, -1)) * 1000)

    class CtxWithCalendar(object):
        def get_trading_dates(self, market, start, end, count):
            seen.append((start, end))
            return [_ms(1990, 12, 19), _ms(2026, 11, 5), _ms(2026, 11, 6)]

    monkeypatch.setattr(mod, "_CTX", CtxWithCalendar())
    status, data = _call(mod, b"GET /calendar?market=SH HTTP/1.1\r\n\r\n")
    assert status == 200
    assert data["ok"] is True
    assert data["data"] == ["20261105", "20261106"]   # 1990 的历史被窗口过滤
    today = time.strftime("%Y%m%d")
    assert seen[0][1] == today
    assert seen[0][0] == "%d0101" % (int(today[:4]) - 1)


def test_calendar_year_param_narrows_window(bridge, monkeypatch):
    """?year=YYYY 优先：只取该自然年。"""
    mod, _ctx = bridge
    seen = []

    def _ms(y, m, d):
        return int(time.mktime((y, m, d, 12, 0, 0, 0, 0, -1)) * 1000)

    class CtxWithCalendar(object):
        def get_trading_dates(self, market, start, end, count):
            seen.append((start, end))
            return [_ms(1990, 12, 19), _ms(2024, 3, 1)]

    monkeypatch.setattr(mod, "_CTX", CtxWithCalendar())
    status, data = _call(mod, b"GET /calendar?market=SH&year=2024 HTTP/1.1\r\n\r\n")
    assert status == 200
    assert seen[0] == ("20240101", "20241231")
    assert data["data"] == ["20240301"]


def test_synth_bucket_aggregates_base_periods():
    """30m/1h/1w/1mon 的时间分桶（合成周期在 ContextInfo 路径下复用）。"""
    for mod in (sim, live):
        assert mod._synth_bucket("20261106103000", "30m") == (2026, 11, 6, 21)
        assert mod._synth_bucket("20261106103000", "1h") == (2026, 11, 6, 10)
        assert mod._synth_bucket("20261106103000", "1mon") == (2026, 11)
        assert mod._synth_bucket("2026-11-06 10:30:00", "1h") == (2026, 11, 6, 10)
        assert mod._synth_bucket("bad-label", "1h") is None
        bars = mod._synth_records(
            [{"index": "20261106103000", "open": 1, "high": 3, "low": 1,
              "close": 2, "volume": 10, "amount": 20},
             {"index": "20261106103500", "open": 2, "high": 5, "low": 2,
              "close": 4, "volume": 20, "amount": 40}],
            "1h", "index")
        assert len(bars) == 1
        assert bars[0]["open"] == 1 and bars[0]["close"] == 4
        assert bars[0]["high"] == 5 and bars[0]["volume"] == 30


def test_order_dry_run_never_calls_passorder(bridge, monkeypatch):
    """单笔下单只走 DRY_RUN 路径：真实 passorder 必须不被调用。"""
    mod, _ctx = bridge
    monkeypatch.setattr(mod, "DRY_RUN", True)

    def _boom(*a, **k):
        raise AssertionError("DRY_RUN 下不得调用 passorder")

    monkeypatch.setattr(mod, "passorder", _boom, raising=False)
    body = json.dumps({"order_id": "oid1", "code": "600000.SH", "op": "buy",
                       "volume": 100, "price": 0}).encode("utf-8")
    raw = (b"POST /order HTTP/1.1\r\nContent-Length: %d\r\n\r\n" % len(body)) + body
    status, data = _call(mod, raw)
    assert status == 200
    assert data["ok"] is True
    assert data["dry_run"] is True


def test_unknown_path_404(bridge):
    mod, _ctx = bridge
    status, data = _call(mod, b"GET /nope HTTP/1.1\r\n\r\n")
    assert status == 404
    assert data["ok"] is False


# ---------------- 跨 tick 状态 ----------------
def test_partial_request_is_finished_on_a_later_tick(bridge):
    """半截请求（无空行结尾）保留到下一次回调继续处理，不会被丢弃。"""
    mod, _ctx = bridge
    s = socket.create_connection(("127.0.0.1", mod.PORT), timeout=5.0)
    s.settimeout(0.2)
    try:
        s.sendall(b"GET /ping HTTP/1.1\r\n")
        for _ in range(3):
            mod.tick()
        with pytest.raises(socket.timeout):
            s.recv(65536)                     # 还没凑齐请求行尾，不能有响应
        s.sendall(b"Host: bridge\r\n\r\n")
        buf = b""
        deadline = time.time() + 5.0
        while time.time() < deadline:
            mod.tick()
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
        assert b"200 OK" in buf
        assert b'"ok": true' in buf
    finally:
        s.close()


# ---------------- 异常可见性 ----------------
def test_handler_exception_is_logged_then_throttled(bridge, monkeypatch, capsys):
    """业务异常返回 500 并打印一次（含类型），同类异常 60s 内不重复刷屏。"""
    mod, _ctx = bridge

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "_handle", _boom)
    status, data = _call(mod, b"GET /ping HTTP/1.1\r\n\r\n")
    assert status == 500
    assert data["ok"] is False
    out = capsys.readouterr().out
    assert "EXC handle" in out
    assert "RuntimeError" in out

    status2, _ = _call(mod, b"GET /ping HTTP/1.1\r\n\r\n")
    assert status2 == 500
    assert "EXC handle" not in capsys.readouterr().out


# ---------------- 双桥同步护栏 ----------------
def _body_after_config(filename):
    """取 config 区之后的全部代码，并把日志/服务名/标签归一化到仿真桥口径。"""
    with open(os.path.join(BRIDGE_DIR, filename), "r", encoding="utf-8") as fh:
        text = fh.read()
    tail = text.split("\n_CTX = None\n", 1)[1]
    for old, new in (("iquant_bridge_live", "iquant_bridge"),
                     ("[BRIDGE-LIVE]", "[BRIDGE]"),
                     ('"iquant-bridge-live"', '"iquant-bridge"'),
                     ("[AUDIT-LIVE]", "[AUDIT]")):
        tail = tail.replace(old, new)
    return tail


def test_two_bridge_files_are_mirror_copies():
    """两桥除顶部 config 区（PORT/ACCOUNT/TICK_TAG…）外必须逐字一致。

    两文件是独立自包含拷贝（QMT 按粘贴内容加载，无法 import 共享模块），逻辑改动
    必须手工同步；本用例把「漏同步一处」变成红灯，而不是运行期才发现。
    """
    assert _body_after_config("iquant_bridge.py") == _body_after_config("iquant_bridge_live.py")


def test_two_bridges_bind_different_accounts():
    """8790 与 8791 必须绑不同账号，否则多账号/多会话验证会串账。"""
    assert sim.ACCOUNT != live.ACCOUNT


