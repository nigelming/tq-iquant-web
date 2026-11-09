import logging
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.services import live_service as svc


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """每个用例独立：清空通道探活 TTL 缓存，避免跨用例复用上次结果。"""
    svc._PROBE_CACHE.clear()
    yield
    svc._PROBE_CACHE.clear()


def _cfg(ths_enabled=False, tq_trade=None):
    cfg = {
        "iquant_bridge": {
            "simulation": {"base_url": "http://127.0.0.1:8790"},
            "live": {"base_url": "http://127.0.0.1:8791"},
        },
        "ths_ui": {
            "enabled": ths_enabled,
            "window_title": "网上股票交易系统5.0",
            "client_exe_path": "D:/同花顺远航版/transaction/xiadan.exe",
            "driver": "clipboard",
        },
    }
    if tq_trade:
        cfg["tq_trade"] = tq_trade
    return cfg


def test_channel_defaults_to_configured_default_engine(monkeypatch):
    """无 session.engine 时按 default_engine（tq_trade.default_engine=tdx）。"""
    monkeypatch.setattr(
        svc, "load_config", lambda: _cfg(False, tq_trade={"default_engine": "tdx"}))
    data = svc.channel_status()
    assert data["active"] == "tdx_tq"
    assert data["label"].startswith("通达信")
    assert data["online"] is None
    assert data["bridge"]["simulation"] == "http://127.0.0.1:8790"


def test_channel_defaults_to_legacy_iquant_when_no_config(monkeypatch):
    """未配置 default_engine 且 ths 未启用 → 旧行为回退 iquant（桥已恢复接入）。"""
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    data = svc.channel_status()
    assert data["engine"] == "iquant"
    assert data["active"] == "iquant_bridge"
    assert data["label"].startswith("QMT 通道")


def test_channel_switches_to_ths_when_enabled_and_available(monkeypatch):
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(True))
    monkeypatch.setattr(svc, "ThsUiDispatcher", object)
    data = svc.channel_status()
    assert data["active"] == "ths_ui"
    assert data["ths_ui"]["enabled"] is True
    assert data["ths_ui"]["available"] is True
    assert data["ths_ui"]["window_title"] == "网上股票交易系统5.0"


def test_channel_falls_back_when_dependency_missing(monkeypatch):
    """THS 依赖缺失 → 回退 default_engine（tdx）。"""
    monkeypatch.setattr(
        svc, "load_config", lambda: _cfg(True, tq_trade={"default_engine": "tdx"}))
    monkeypatch.setattr(svc, "ThsUiDispatcher", None)
    data = svc.channel_status()
    assert data["active"] == "tdx_tq"
    assert data["ths_ui"]["enabled"] is True
    assert data["ths_ui"]["available"] is False


def test_test_channel_ths_without_dependency(monkeypatch):
    monkeypatch.setattr(svc, "ThsUiDispatcher", None)
    out = svc.test_channel("ths_ui")
    assert out["ok"] is False
    assert "pywinauto" in out["error"]


def test_test_channel_bridge_ping(monkeypatch):
    class FakeBridge:
        def __init__(self, base_url=None):
            self.base_url = base_url

        def heartbeat(self):
            return True

    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    monkeypatch.setattr(svc, "HttpBridgeDispatcher", FakeBridge)
    out = svc.test_channel("iquant_bridge", "live")
    assert out == {"ok": True, "channel": "iquant_bridge",
                   "base_url": "http://127.0.0.1:8791", "error": None}


def test_test_channel_ths_requires_captcha_ocr(monkeypatch):
    cfg = _cfg(True)
    cfg["ths_ui"]["captcha_ocr"] = False
    monkeypatch.setattr(svc, "load_config", lambda: cfg)

    class FakeThs:
        channel = "ths_ui"
        last_error = ""

        def __init__(self, **kwargs):
            pass

        def connect(self):
            return True

        def query_positions(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr(svc, "ThsUiDispatcher", FakeThs)
    out = svc.test_channel("ths_ui")
    assert out["ok"] is False
    assert "验证码 OCR" in out["error"]


def test_ths_dispatcher_reports_channel_and_error(monkeypatch):
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(True))
    seen = {}

    class FakeThs:
        channel = "ths_ui"
        last_error = ""

        def __init__(self, window_title=None, client_exe_path=None, captcha_ocr=False,
                     execute_mode=None):
            seen["window_title"] = window_title
            seen["client_exe_path"] = client_exe_path
            seen["captcha_ocr"] = captcha_ocr
            seen["execute_mode"] = execute_mode

        def connect(self):
            return False

        def query_positions(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr(svc, "ThsUiDispatcher", FakeThs)
    out = svc.test_channel("ths_ui")
    assert out["ok"] is False
    assert seen["window_title"] == "网上股票交易系统5.0"
    assert seen["client_exe_path"].endswith("xiadan.exe")
    # 只读探测不得启文件交接执行器（否则 data/ths/0 会挂消费线程）
    assert seen["execute_mode"] == "direct"


# ---------------------------------------------------------------- 四引擎可选

def test_engine_status_lists_four_engines(monkeypatch):
    """引擎清单（QMT 恢复接入）：掘金/通达信/同花顺/QMT 通道。"""
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    data = svc.engine_status("goldminer")
    assert [e["key"] for e in data["engines"]] == [
        "goldminer", "tdx", "ths", "iquant"]
    assert data["engine"] == "goldminer"
    assert data["active"] == "goldminer_bridge"
    assert data["engines"][-1]["key"] == "iquant"


def test_engine_status_iquant_channel_and_detail(monkeypatch):
    """QMT 项：通道键 iquant_bridge，detail 带仿真/实盘双桥地址。"""
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    data = svc.engine_status("iquant")
    item = {e["key"]: e for e in data["engines"]}["iquant"]
    assert item["channel"] == "iquant_bridge"
    assert item["label"] == "QMT 通道"
    assert item["configured"] is True
    assert "http://127.0.0.1:8790" in item["detail"]
    assert "http://127.0.0.1:8791" in item["detail"]
    assert data["active"] == "iquant_bridge"


def test_channel_status_uses_session_engine(monkeypatch):
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    data = svc.channel_status(engine_key="tdx", engines={})
    assert data["engine"] == "tdx"
    assert data["active"] == "tdx_tq"
    assert data["label"].startswith("通达信")


def test_channel_no_running_engine_reports_undetected(monkeypatch):
    """引擎未运行 → online=None + engine_running=False，且不再做客户端探活。

    历史缺陷（2026-11-04）：无引擎时 `_probe_channel_online` 另建 dispatcher
    做 TQ 心跳，只要通达信客户端开着就返回 True，后端重启后工作台仍显示「在线」。
    """
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))

    class _Boom:
        def __init__(self, **kw):
            raise AssertionError("引擎未运行不应构造 dispatcher 探活")

    monkeypatch.setattr(svc, "TdxTqDispatcher", _Boom)
    data = svc.channel_status(engine_key="tdx", engines={})
    assert data["online"] is None
    assert data["engine_running"] is False


def test_channel_running_engine_reports_real_heartbeat(monkeypatch):
    """无引擎状态替身 → 回退探活（30s TTL 缓存内复用，清缓存后如实反映离线）。"""
    import types

    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    engine = types.SimpleNamespace(
        dispatcher=types.SimpleNamespace(channel="tdx_tq", heartbeat=lambda: True))
    data = svc.channel_status(engine_key="tdx", engines={1: engine})
    assert data["engine_running"] is True
    assert data["online"] is True

    engine.dispatcher.heartbeat = lambda: False
    svc._PROBE_CACHE.clear()   # TTL 缓存内会复用上次结果，清空后重新探活
    data = svc.channel_status(engine_key="tdx", engines={1: engine})
    assert data["engine_running"] is True
    assert data["online"] is False


def test_channel_running_goldminer_engine_reports_running(monkeypatch):
    """掘金通道也必须在映射表内：有运行引擎 → engine_running=True + 真实心跳。

    缺陷（2026-11-05）：`_ENGINE_CHANNEL` 只覆盖 tdx/ths/iquant，漏了 goldminer，
    会话 #3（engine=goldminer）实际 running 时仍返回 engine_running=False、
    online=None，前端通道卡片误显示「未检测」。
    """
    import types

    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    engine = types.SimpleNamespace(
        dispatcher=types.SimpleNamespace(
            channel="goldminer_bridge", heartbeat=lambda: True))
    data = svc.channel_status(engine_key="goldminer", engines={3: engine})
    assert data["active"] == "goldminer_bridge"
    assert data["engine_running"] is True
    assert data["online"] is True   # 不得出现 None 的「未检测」态


def test_channel_running_iquant_engine_reports_running(monkeypatch):
    """QMT 恢复后同通道映射必须生效：运行中 → engine_running=True。"""
    import types

    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    engine = types.SimpleNamespace(
        bridge_online=True,
        dispatcher=types.SimpleNamespace(
            channel="iquant_bridge", heartbeat=lambda: True))
    data = svc.channel_status(engine_key="iquant", engines={5: engine})
    assert data["active"] == "iquant_bridge"
    assert data["engine_running"] is True
    assert data["online"] is True


def test_channel_goldminer_no_running_engine_stays_undetected(monkeypatch):
    """无掘金引擎在跑时保持原语义：engine_running=False、online=None（未检测）。"""
    import types

    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    tdx = types.SimpleNamespace(
        dispatcher=types.SimpleNamespace(channel="tdx_tq", heartbeat=lambda: True))
    data = svc.channel_status(engine_key="goldminer", engines={1: tdx})
    assert data["engine_running"] is False
    assert data["online"] is None


def test_channel_ignores_engines_of_other_channels(monkeypatch):
    """注册表里是别的通道（如 ths）时，tdx 仍应按「未运行」处理。"""
    import types

    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    ths = types.SimpleNamespace(
        dispatcher=types.SimpleNamespace(channel="ths_ui", heartbeat=lambda: True))
    data = svc.channel_status(engine_key="tdx", engines={7: ths})
    assert data["engine_running"] is False
    assert data["online"] is None


def test_channel_status_prefers_engine_state_without_probe(monkeypatch):
    """有运行引擎 → 只读引擎最近已知在线态，绝不新发 heartbeat（慢桥不阻塞页面）。

    实证 2026-11-05：掘金桥 /ping 耗时 7.7s（桥内全局锁串行），状态总览整页卡住。
    """
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    probed = []
    engine = SimpleNamespace(
        bridge_online=True,
        _last_tick_at=datetime.now(),
        dispatcher=SimpleNamespace(
            channel="goldminer_bridge", heartbeat=lambda: probed.append(1) or True))
    data = svc.channel_status(engine_key="goldminer", engines={3: engine})
    assert data["engine_running"] is True
    assert data["online"] is True
    assert data["online_source"] == "engine"
    assert data["stale"] is False
    assert data["checked_at"] is not None
    assert probed == []   # 关键：页面请求未再打桥


def test_channel_status_engine_state_stale_when_tick_old(monkeypatch):
    """引擎主循环长时间未 tick（> max(180s, 3×poll)）→ 状态标 stale。"""
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    engine = SimpleNamespace(
        bridge_online=False,
        _last_tick_at=datetime.now() - timedelta(seconds=400),
        _poll_interval=60,
        dispatcher=SimpleNamespace(channel="goldminer_bridge", heartbeat=lambda: True))
    data = svc.channel_status(engine_key="goldminer", engines={3: engine})
    assert data["online"] is False
    assert data["stale"] is True
    assert data["checked_at"] is not None


def test_channel_probe_times_out_within_one_second(monkeypatch):
    """无引擎状态的慢心跳：1s 硬超时返回（判离线 + stale），不拖住 HTTP 请求。"""
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))

    class SlowDispatcher:
        channel = "tdx_tq"

        def heartbeat(self):
            time.sleep(5)
            return True

    engine = SimpleNamespace(dispatcher=SlowDispatcher())
    t0 = time.monotonic()
    data = svc.channel_status(engine_key="tdx", engines={1: engine})
    elapsed = time.monotonic() - t0
    assert elapsed < 1.5
    assert data["online"] is False
    assert data["stale"] is True
    assert data["online_source"] == "probe"


def test_channel_probe_reuses_ttl_cache(monkeypatch):
    """探活结果 30s 内复用：连续两次 channel_status 只打一次心跳。"""
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    probed = []

    class CountingDispatcher:
        channel = "tdx_tq"

        def heartbeat(self):
            probed.append(1)
            return True

    engine = SimpleNamespace(dispatcher=CountingDispatcher())
    svc.channel_status(engine_key="tdx", engines={1: engine})
    svc.channel_status(engine_key="tdx", engines={1: engine})
    assert len(probed) == 1


def test_bridge_status_reads_engine_state_without_probe():
    """session 详情/bridge-status 也走引擎已知态，不新发 /ping。"""
    def _boom():
        raise AssertionError("不应在页面请求里探活")

    engine = SimpleNamespace(
        bridge_online=False,
        dispatcher=SimpleNamespace(heartbeat=_boom))
    assert svc.bridge_status(1, "running", engine) == {"online": False, "status": "running"}


def test_make_dispatcher_by_engine(monkeypatch):
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(
        False,
        tq_trade={"account": "A1", "account_type": "stock", "price_mode": "latest"},
    ))
    seen = {}

    class FakeTdx:
        def __init__(self, **kw):
            seen["tdx"] = kw

    monkeypatch.setattr(svc, "TdxTqDispatcher", FakeTdx)
    assert isinstance(svc.make_dispatcher("tdx"), FakeTdx)
    assert seen["tdx"]["account"] == "A1"
    assert seen["tdx"]["price_mode"] == "latest"


def test_make_dispatcher_raises_when_dependency_missing(monkeypatch):
    import pytest

    monkeypatch.setattr(svc, "TdxTqDispatcher", None)
    with pytest.raises(RuntimeError):
        svc.make_dispatcher("tdx")


def test_make_dispatcher_iquant_picks_bridge_by_mode(monkeypatch):
    """iquant：simulation→8790 / live→8791 / 未知 mode 兜底仿真桥。"""
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    seen = {}

    class FakeBridge:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(svc, "HttpBridgeDispatcher", FakeBridge)

    svc.make_dispatcher("iquant", "simulation", 1)
    assert seen["base_url"] == "http://127.0.0.1:8790"
    assert seen["channel"] == "iquant_bridge"

    svc.make_dispatcher("iquant", "live", 2)
    assert seen["base_url"] == "http://127.0.0.1:8791"

    # 未知 mode 不得落到真实资金桥（8791），兜底仿真桥
    svc.make_dispatcher("iquant", "unknown", 3)
    assert seen["base_url"] == "http://127.0.0.1:8790"


def test_make_dispatcher_iquant_reads_timeouts_from_config(monkeypatch):
    """超时可配置：config.iquant_bridge 三键生效；缺省/非法回退合理默认。"""
    cfg = _cfg(False)
    cfg["iquant_bridge"].update({
        "request_timeout_seconds": "12",
        "slow_response_seconds": 6,
        "slow_warn_interval_seconds": 120,
    })
    monkeypatch.setattr(svc, "load_config", lambda: cfg)
    seen = {}

    class FakeBridge:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(svc, "HttpBridgeDispatcher", FakeBridge)
    svc.make_dispatcher("iquant", "simulation")
    assert seen["timeout"] == 12.0
    assert seen["slow_threshold"] == 6.0
    assert seen["slow_warn_interval"] == 120.0

    # 未配置 → 默认（本机 loopback 桥：10s 超时 / 5s 慢阈值 / 300s 冷却窗）
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    svc.make_dispatcher("iquant", "simulation")
    assert seen["timeout"] == 10.0
    assert seen["slow_threshold"] == 5.0
    assert seen["slow_warn_interval"] == 300.0


def test_make_dispatcher_iquant_raises_when_dependency_missing(monkeypatch):
    import pytest

    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    monkeypatch.setattr(svc, "HttpBridgeDispatcher", None)
    with pytest.raises(RuntimeError):
        svc.make_dispatcher("iquant")


def test_bridge_probe_message_labels_iquant(monkeypatch):
    """QMT 通道的启动闸提示不能笼统写「桥」，要能区分是哪座桥。"""
    msg = svc.bridge_probe_message("iquant_bridge", "refused", "http://127.0.0.1:8790")
    assert "QMT 通道" in msg
    assert "端口无监听" in msg


def test_test_channel_tdx_reports_not_ready(monkeypatch):
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))

    class FakeTdx:
        channel = "tdx_tq"
        last_error = "请启动客户端"

        def __init__(self, **kw):
            pass

        def heartbeat(self):
            return False

        def query_positions(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr(svc, "TdxTqDispatcher", FakeTdx)
    out = svc.test_channel("tdx_tq")
    assert out["ok"] is False
    assert out["channel"] == "tdx_tq"
    assert "请启动客户端" in out["error"]


# ---------------- 持仓计数：0 股残行不算持仓 ----------------

def test_real_position_count_ignores_zero_volume():
    """通达信卖出后持仓表保留 0 股残行 → 3 行里只有 1 条真实持仓。"""
    assert svc.real_position_count([
        {"volume": 0, "available": 0},
        {"volume": 0, "available": 0},
        {"volume": 300, "available": 300},
    ]) == 1


def test_real_position_count_keeps_t1_locked_position():
    """当日买入 T+1 锁仓：available=0 但 volume>0 仍是真实持仓。"""
    assert svc.real_position_count([
        {"volume": 300, "available": 0},
        {"volume": 0, "available": 0},
    ]) == 1


def test_test_channel_tdx_counts_real_positions_only(monkeypatch):
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))

    class FakeTdx:
        channel = "tdx_tq"
        last_error = ""

        def __init__(self, **kw):
            pass

        def heartbeat(self):
            return True

        def query_positions(self):
            return [
                {"instrument": "002056", "volume": 0, "available": 0},
                {"instrument": "600888", "volume": 0, "available": 0},
                {"instrument": "600000", "volume": 300, "available": 300},
            ]

        def close(self):
            pass

    monkeypatch.setattr(svc, "TdxTqDispatcher", FakeTdx)
    out = svc.test_channel("tdx_tq")
    assert out["ok"] is True
    assert out["positions"] == 1


def test_test_channel_ths_counts_real_positions_only(monkeypatch):
    cfg = _cfg(True)
    cfg["ths_ui"]["captcha_ocr"] = True
    monkeypatch.setattr(svc, "load_config", lambda: cfg)

    class FakeThs:
        channel = "ths_ui"
        last_error = ""

        def __init__(self, **kwargs):
            pass

        def connect(self):
            return True

        def query_positions(self):
            return [
                {"instrument": "002056", "volume": 0, "available": 0},
                {"instrument": "600888", "volume": 0, "available": 0},
                {"instrument": "600000", "volume": 300, "available": 0},
            ]

        def close(self):
            pass

    monkeypatch.setattr(svc, "ThsUiDispatcher", FakeThs)
    out = svc.test_channel("ths_ui")
    assert out["ok"] is True
    assert out["positions"] == 1


# ---------------- 重放聚合：超卖流水钳制为 0、不展示负数 ----------------

def _replay_trade(qty, ttype, code="600888.SH", session_id=1):
    return SimpleNamespace(
        live_session_id=session_id, portfolio_strategy_id=1, strategy_id=1,
        stock_code=code, trade_type=ttype, price=10.0, quantity=qty,
        amount=float(qty) * 10.0, commission=0.0, stamp_duty=0.0,
        trade_time=datetime(2026, 11, 5, 9, 51),
    )


def test_replay_full_close_is_zero_not_negative():
    """买入 300 → 卖出 300：结果为 0（不展示行），不出现 -300。"""
    rows = svc.aggregate_positions_from_trades(
        [_replay_trade(300, "buy"), _replay_trade(300, "sell")])
    assert rows == []


def test_replay_sell_only_is_clamped_and_warned(caplog):
    """无期初/买入流水只有卖出（脏数据）：钳制为 0 + 日志告警。"""
    svc._OVERSELL_WARNED.clear()
    with caplog.at_level(logging.WARNING):
        rows = svc.aggregate_positions_from_trades([_replay_trade(300, "sell")])
    assert rows == []
    assert any("超卖流水" in r.getMessage() for r in caplog.records)


def test_create_session_persists_engine(db_session):
    data = svc.create_session(db_session, {"name": "s1", "engine": "tdx"}, "simulation")
    assert data["engine"] == "tdx"
    listed = svc.list_sessions(db_session)
    assert listed[0]["engine"] == "tdx"


# ---------------- 外部信号注入：signal_file_path 会话配置（0011） ----------------

def test_create_session_persists_signal_file_path(db_session):
    """create_session 透传存储 signal_file_path；get/list 序列化可见。"""
    data = svc.create_session(
        db_session, {"name": "ext", "engine": "tdx",
                     "signal_file_path": "D:/ths/alert.txt"}, "simulation")
    row = db_session.query(svc.LiveSession).filter_by(id=data["id"]).first()
    assert row.signal_file_path == "D:/ths/alert.txt"
    detail = svc.get_session(db_session, data["id"])
    assert detail["signal_file_path"] == "D:/ths/alert.txt"
    listed = svc.list_sessions(db_session)
    assert listed[0]["signal_file_path"] == "D:/ths/alert.txt"


def test_create_session_empty_signal_file_path_becomes_none(db_session):
    """空串/缺省 → None（不启用外部信号注入）。"""
    data = svc.create_session(
        db_session, {"name": "ext2", "engine": "tdx",
                     "signal_file_path": "   "}, "simulation")
    row = db_session.query(svc.LiveSession).filter_by(id=data["id"]).first()
    assert row.signal_file_path is None
    data2 = svc.create_session(db_session, {"name": "ext3", "engine": "tdx"}, "simulation")
    row2 = db_session.query(svc.LiveSession).filter_by(id=data2["id"]).first()
    assert row2.signal_file_path is None


# ---------------- A-6：create_session 默认引擎（tq_trade.default_engine 覆盖） ----------------

def test_default_engine_prefers_tq_trade_override(monkeypatch):
    """A-6：tq_trade.default_engine 优先（用户主通道 tdx）。"""
    monkeypatch.setattr(
        svc, "load_config", lambda: _cfg(False, tq_trade={"default_engine": "tdx"}))
    assert svc.default_engine() == "tdx"


def test_session_engine_qmt_falls_back_to_default(db_session, monkeypatch):
    """历史会话兼容：qmt 通道已移除，显式 'qmt' 不再进白名单，
    session_engine 回退全局默认（tq_trade.default_engine），不加迁移。"""
    monkeypatch.setattr(
        svc, "load_config", lambda: _cfg(False, tq_trade={"default_engine": "tdx"}))
    from core.models import LiveSession
    session = LiveSession(
        name="legacy_qmt", mode="live", status="stopped", engine="qmt")
    db_session.add(session)
    db_session.commit()
    assert svc.session_engine(session) == "tdx"


def test_bridge_probe_message_three_tiers():
    """启动闸三档提示可区分：端口无监听 / 已监听但无响应 / 协议不符。"""
    url = "http://127.0.0.1:7801"
    refused = svc.bridge_probe_message("goldminer_bridge", "refused", url)
    timeout = svc.bridge_probe_message("goldminer_bridge", "timeout", url)
    protocol = svc.bridge_probe_message("goldminer_bridge", "protocol", url)
    assert "端口无监听" in refused and "桥进程未启动" in refused
    assert "端口已监听但无响应" in timeout and "终端显示“运行中”不代表查询可用" in timeout
    assert "应答格式不符" in protocol
    # 未知原因兜底仍给出 URL，不丢上下文
    assert url in svc.bridge_probe_message("goldminer_bridge", "unreachable", url)


def test_default_engine_empty_or_invalid_falls_back_to_legacy(monkeypatch):
    """A-6：未配置/空/非法 → 沿用旧行为（ths_ui.enabled → ths，否则 iquant）。"""
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    assert svc.default_engine() == "iquant"
    monkeypatch.setattr(
        svc, "load_config", lambda: _cfg(False, tq_trade={"default_engine": ""}))
    assert svc.default_engine() == "iquant"
    monkeypatch.setattr(
        svc, "load_config", lambda: _cfg(False, tq_trade={"default_engine": "xyz"}))
    assert svc.default_engine() == "iquant"
    monkeypatch.setattr(
        svc, "load_config", lambda: _cfg(True, tq_trade={"default_engine": ""}))
    monkeypatch.setattr(svc, "ThsUiDispatcher", object)
    assert svc.default_engine() == "ths"


def test_create_session_defaults_engine_to_iquant(db_session, monkeypatch):
    """无显式引擎且配置无 default_engine → 旧行为默认 iquant。"""
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    data = svc.create_session(db_session, {"name": "s2"}, "live")
    assert data["engine"] == "iquant"


def test_create_session_uses_configured_default_engine(db_session, monkeypatch):
    """A-6：create_session 未显式选引擎时用 tq_trade.default_engine。"""
    monkeypatch.setattr(
        svc, "load_config", lambda: _cfg(False, tq_trade={"default_engine": "tdx"}))
    data = svc.create_session(db_session, {"name": "s3"}, "live")
    assert data["engine"] == "tdx"


def test_config_defaults_carry_new_tq_trade_keys():
    """A-7：core.config._defaults 与 SystemConfig TQ 通道段新键一一对应。"""
    from core.config import _defaults
    tq = _defaults()["tq_trade"]
    assert tq["default_engine"] == "tdx"
    assert tq["cancel_unfilled_minutes"] == 3


# ---------------- THS 文件交接层（file_builtin 双模式）配置 ----------------

def test_config_defaults_carry_new_ths_ui_keys():
    """文件交接层三键默认值：file_builtin / 5s / data/ths。"""
    from core.config import _defaults
    ths = _defaults()["ths_ui"]
    assert ths["execute_mode"] == "file_builtin"
    assert ths["execute_interval_seconds"] == 5
    assert ths["work_dir"] == "data/ths"


def test_ths_ui_config_exposes_file_mode_keys(monkeypatch):
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(True))
    data = svc.ths_ui_config()
    assert data["execute_mode"] == "file_builtin"
    assert data["execute_interval_seconds"] == 5
    assert data["work_dir"] == "data/ths"


def test_make_dispatcher_ths_passes_file_mode_params(monkeypatch):
    """ths 引擎构造 dispatcher 带出执行模式/间隔/工作目录/session_id。"""
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(True))
    seen = {}

    class FakeThs:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(svc, "ThsUiDispatcher", FakeThs)
    svc.make_dispatcher("ths", "simulation", 7)
    assert seen["execute_mode"] == "file_builtin"
    assert seen["execute_interval_seconds"] == 5
    assert seen["work_dir"] == "data/ths"
    assert seen["session_id"] == 7
