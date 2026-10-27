from core.services import live_service as svc


def _cfg(ths_enabled=False, tq_trade=None, qmt=None):
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
    if qmt:
        cfg["qmt"] = qmt
    return cfg


def test_channel_defaults_to_iquant_bridge(monkeypatch):
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    data = svc.channel_status()
    assert data["active"] == "iquant_bridge"
    assert data["label"].startswith("iQuant")
    assert data["online"] is None
    assert data["bridge"]["simulation"] == "http://127.0.0.1:8790"


def test_channel_switches_to_ths_when_enabled_and_available(monkeypatch):
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(True))
    monkeypatch.setattr(svc, "ThsUiDispatcher", object)
    data = svc.channel_status()
    assert data["active"] == "ths_ui"
    assert data["ths_ui"]["enabled"] is True
    assert data["ths_ui"]["available"] is True
    assert data["ths_ui"]["window_title"] == "网上股票交易系统5.0"


def test_channel_falls_back_when_dependency_missing(monkeypatch):
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(True))
    monkeypatch.setattr(svc, "ThsUiDispatcher", None)
    data = svc.channel_status()
    assert data["active"] == "iquant_bridge"
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

        def __init__(self, window_title=None, client_exe_path=None, captcha_ocr=False):
            seen["window_title"] = window_title
            seen["client_exe_path"] = client_exe_path
            seen["captcha_ocr"] = captcha_ocr

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


# ---------------------------------------------------------------- 三引擎可选

def test_engine_status_lists_four_engines(monkeypatch):
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    data = svc.engine_status("iquant")
    assert [e["key"] for e in data["engines"]] == ["iquant", "tdx", "ths", "qmt"]
    assert data["engine"] == "iquant"
    assert data["active"] == "iquant_bridge"
    # 默认未配置通达信账号/QMT 路径 → qmt 标记为未配置，前端下拉禁用
    qmt = [e for e in data["engines"] if e["key"] == "qmt"][0]
    assert qmt["configured"] is False


def test_channel_status_uses_session_engine(monkeypatch):
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))
    data = svc.channel_status(engine_key="tdx")
    assert data["engine"] == "tdx"
    assert data["active"] == "tdx_tq"
    assert data["label"].startswith("通达信")


def test_make_dispatcher_by_engine(monkeypatch):
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(
        False,
        tq_trade={"account": "A1", "account_type": "stock", "price_mode": "latest"},
        qmt={"path": "C:/qmt", "account": "8888"},
    ))
    seen = {}

    class FakeTdx:
        def __init__(self, **kw):
            seen["tdx"] = kw

    class FakeQmt:
        def __init__(self, **kw):
            seen["qmt"] = kw

    monkeypatch.setattr(svc, "TdxTqDispatcher", FakeTdx)
    monkeypatch.setattr(svc, "QmtDispatcher", FakeQmt)
    assert isinstance(svc.make_dispatcher("tdx"), FakeTdx)
    assert seen["tdx"]["account"] == "A1"
    assert seen["tdx"]["price_mode"] == "latest"
    assert isinstance(svc.make_dispatcher("qmt", session_id=7), FakeQmt)
    assert seen["qmt"]["path"] == "C:/qmt"
    assert seen["qmt"]["session_id"] == 7


def test_make_dispatcher_raises_when_dependency_missing(monkeypatch):
    import pytest

    monkeypatch.setattr(svc, "TdxTqDispatcher", None)
    monkeypatch.setattr(svc, "QmtDispatcher", None)
    with pytest.raises(RuntimeError):
        svc.make_dispatcher("tdx")
    with pytest.raises(RuntimeError):
        svc.make_dispatcher("qmt")


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


def test_test_channel_qmt_ok(monkeypatch):
    monkeypatch.setattr(svc, "load_config", lambda: _cfg(False))

    class FakeQmt:
        channel = "qmt"
        last_error = ""

        def __init__(self, **kw):
            pass

        def heartbeat(self):
            return True

        def query_positions(self):
            return [{"instrument": "000001"}]

        def close(self):
            pass

    monkeypatch.setattr(svc, "QmtDispatcher", FakeQmt)
    assert svc.test_channel("qmt") == {"ok": True, "channel": "qmt", "positions": 1}


def test_create_session_persists_engine(db_session):
    data = svc.create_session(db_session, {"name": "s1", "engine": "tdx"}, "simulation")
    assert data["engine"] == "tdx"
    listed = svc.list_sessions(db_session)
    assert listed[0]["engine"] == "tdx"


# ---------------- A-6：create_session 默认引擎（tq_trade.default_engine 覆盖） ----------------

def test_default_engine_prefers_tq_trade_override(monkeypatch):
    """A-6：tq_trade.default_engine 优先（用户主通道 tdx）。"""
    monkeypatch.setattr(
        svc, "load_config", lambda: _cfg(False, tq_trade={"default_engine": "tdx"}))
    assert svc.default_engine() == "tdx"
    monkeypatch.setattr(
        svc, "load_config", lambda: _cfg(False, tq_trade={"default_engine": "qmt"}))
    assert svc.default_engine() == "qmt"


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
