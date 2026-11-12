from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.models import (
    Base, Formula, FormulaSignal, LiveSession, LiveSessionPortfolio,
    PortfolioStrategy, StockPool, StockPoolStock, Strategy,
)
from core.services import live_service


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed_empty_pool(db, execution_mode="alert"):
    db.add(LiveSession(id=9, name="e", mode="simulation", status="stopped",
                       engine="tdx", execution_mode=execution_mode))
    pool = StockPool(code="E", name="e")
    db.add(pool)
    db.flush()
    f = Formula(id=9, name="momentum", content="C")
    db.add(f)
    db.flush()
    db.add(FormulaSignal(formula_id=9, signal_name="buy",
                         signal_type="OPEN", trigger_value=1))
    port = PortfolioStrategy(id=9, name="e", stock_pool_id=pool.id,
                             initial_capital=Decimal("100000"))
    db.add(port)
    db.add(LiveSessionPortfolio(session_id=9, portfolio_strategy_id=9))
    db.add(Strategy(id=9, portfolio_id=9, name="e1", formula_id=9,
                    period="1d", role="master", signal_frequency="1d"))
    db.commit()


def _seed(db, execution_mode="alert"):
    db.add(LiveSession(id=1, name="s", mode="simulation", status="stopped",
                       engine="tdx", execution_mode=execution_mode))
    pool = StockPool(code="P", name="p")
    db.add(pool)
    db.flush()
    db.add(StockPoolStock(pool_id=pool.id, stock_code="000001.SZ"))
    f = Formula(id=1, name="momentum", content="C")
    db.add(f)
    db.flush()
    db.add(FormulaSignal(formula_id=1, signal_name="buy",
                         signal_type="OPEN", trigger_value=1))
    port = PortfolioStrategy(id=1, name="p", stock_pool_id=pool.id,
                             initial_capital=Decimal("100000"))
    db.add(port)
    db.add(LiveSessionPortfolio(session_id=1, portfolio_strategy_id=1))
    db.add(Strategy(id=1, portfolio_id=1, name="s1", formula_id=1,
                    period="1d", role="master", signal_frequency="1w",
                    signal_at="09:00", signal_weekday=1,
                    signal_data_end="completed"))
    db.commit()


def test_create_session_defaults_to_hermes():
    db = _db()
    row = live_service.create_session(db, {"name": "s", "engine": "tdx"}, "simulation")
    assert row["execution_mode"] == "hermes"


def test_preflight_rejects_builtin():
    db = _db()
    _seed(db)
    db.query(Formula).filter_by(id=1).update({"engine": "builtin"})
    db.commit()
    status, msg = live_service.preflight_signal_config(db, 1, "alert")
    assert status == 503
    assert "通达信导入公式" in msg


def test_preflight_rejects_auto_custom_frequency():
    db = _db()
    _seed(db)
    status, msg = live_service.preflight_signal_config(db, 1, "auto")
    assert status == 503
    assert "P2" in msg


def test_preflight_alert_passes_with_fake_tq(monkeypatch):
    db = _db()
    _seed(db)
    monkeypatch.setattr(live_service, "_preflight_tq_alert", lambda: None)
    monkeypatch.setattr(live_service, "_smoke_formula_outputs",
                        lambda *a, **kw: None)
    assert live_service.preflight_signal_config(db, 1, "alert") is None


def test_preflight_alert_returns_503_when_formula_smoke_raises(monkeypatch):
    db = _db()
    _seed(db)
    monkeypatch.setattr(live_service, "_preflight_tq_alert", lambda: None)

    def boom(*args, **kwargs):
        raise RuntimeError("tq disconnected")

    monkeypatch.setattr(live_service.TQFormula, "compute", boom)
    status, msg = live_service.preflight_signal_config(db, 1, "alert")
    assert status == 503
    assert "momentum" in msg
    assert "试算失败" in msg


def test_preflight_alert_skips_tq_alert_probe_when_send_warn_disabled(monkeypatch):
    """send_warn 关闭 → alert 启动不再探测 send_warn 可用性（公式试算仍保留）。"""
    db = _db()
    _seed(db)
    monkeypatch.setattr(live_service, "_send_warn_enabled", lambda: False)

    def _no_probe():
        raise AssertionError("_preflight_tq_alert must be skipped")

    monkeypatch.setattr(live_service, "_preflight_tq_alert", _no_probe)
    monkeypatch.setattr(live_service, "_smoke_formula_outputs",
                        lambda *a, **kw: None)
    assert live_service.preflight_signal_config(db, 1, "alert") is None


def test_send_warn_enabled_reads_config(monkeypatch):
    monkeypatch.setattr(live_service, "load_config",
                        lambda: {"live": {"alert_send_warn_enabled": True}})
    assert live_service._send_warn_enabled() is True
    monkeypatch.setattr(live_service, "load_config", lambda: {})
    assert live_service._send_warn_enabled() is False


# ---------------- 0018：session 级 hermes 模式 ----------------

def test_preflight_accepts_hermes_and_skips_probe(monkeypatch):
    """hermes 模式：preflight 同 alert（公式试算保留，send_warn 探测按开关跳过）。"""
    db = _db()
    _seed(db)
    monkeypatch.setattr(live_service, "_send_warn_enabled", lambda: False)

    def _no_probe():
        raise AssertionError("_preflight_tq_alert must be skipped")

    monkeypatch.setattr(live_service, "_preflight_tq_alert", _no_probe)
    monkeypatch.setattr(live_service, "_smoke_formula_outputs",
                        lambda *a, **kw: None)
    assert live_service.preflight_signal_config(db, 1, "hermes") is None


def test_preflight_rejects_unknown_mode():
    db = _db()
    _seed(db)
    status, msg = live_service.preflight_signal_config(db, 1, "bogus")
    assert status == 400
    assert "alert/auto/hermes" in msg


def test_start_session_hermes_uses_alert_dispatcher(monkeypatch):
    """hermes 模式启动：AlertDataDispatcher（不下单），execution_mode 保持 hermes。"""
    factory = _factory()
    db = factory()
    _seed(db, execution_mode="hermes")

    class FakeDispatcher:
        channel = "alert"
        supports_remark = False
        supports_batch_quote = True

        def __init__(self, **kwargs):
            self.last_error = ""

        def heartbeat(self):
            return True

        def query_calendar(self):
            return []

        def query_quote(self, code, period="1m", count=10):
            return []

        def close(self):
            pass

    monkeypatch.setattr(live_service, "AlertDataDispatcher", FakeDispatcher)
    monkeypatch.setattr(live_service, "_preflight_tq_alert", lambda: None)
    monkeypatch.setattr(live_service, "_smoke_formula_outputs",
                        lambda *a, **kw: None)
    engine, session, error = live_service.start_session(
        db, 1, {}, "simulation", db_session_factory=factory)
    assert error is None
    assert engine.execution_mode == "hermes"
    assert engine.dispatcher.channel == "alert"
    db.close()


def test_create_session_accepts_hermes_mode():
    db = _db()
    row = live_service.create_session(
        db, {"name": "h", "engine": "tdx", "execution_mode": "hermes"},
        "simulation")
    assert row["execution_mode"] == "hermes"


def test_start_session_rejects_empty_stock_pool_alert():
    factory = _factory()
    db = factory()
    _seed_empty_pool(db, execution_mode="alert")
    engine, session, error = live_service.start_session(
        db, 9, {}, "simulation", db_session_factory=factory)
    assert engine is None
    assert error is not None
    assert error[0] == 503
    assert "股票池为空" in error[1]
    assert session.status == "stopped"
    db.close()


def test_start_session_rejects_empty_stock_pool_auto():
    factory = _factory()
    db = factory()
    _seed_empty_pool(db, execution_mode="auto")
    engine, session, error = live_service.start_session(
        db, 9, {}, "simulation", db_session_factory=factory)
    assert engine is None
    assert error is not None
    assert error[0] == 503
    assert "股票池为空" in error[1]
    db.close()


def test_start_session_non_empty_pool_proceeds(monkeypatch):
    factory = _factory()
    db = factory()
    _seed(db)

    class FakeDispatcher:
        channel = "alert"
        supports_remark = False
        supports_batch_quote = True

        def __init__(self, **kwargs):
            self.last_error = ""

        def heartbeat(self):
            return True

        def query_calendar(self):
            return []

        def query_quote(self, code, period="1m", count=10):
            return []

        def close(self):
            pass

    monkeypatch.setattr(live_service, "AlertDataDispatcher", FakeDispatcher)
    monkeypatch.setattr(live_service, "_preflight_tq_alert", lambda: None)
    monkeypatch.setattr(live_service, "_smoke_formula_outputs",
                        lambda *a, **kw: None)
    engine, session, error = live_service.start_session(
        db, 1, {}, "simulation", db_session_factory=factory)
    assert error is None
    assert engine is not None
    assert engine.execution_mode == "alert"
    db.close()


# ---------------- THS 文件交接层：OCR 启动闸仅 direct 模式 ----------------

class _FakeThsDispatcher:
    channel = "ths_ui"
    supports_remark = False

    def __init__(self, **kwargs):
        self.execute_mode = kwargs.get("execute_mode", "file_builtin")
        self.file_handoff_enabled = self.execute_mode == "file_builtin"
        self._work_dir = kwargs.get("work_dir", "data/ths")
        self.last_error = ""

    def heartbeat(self):
        return True

    def query_calendar(self):
        return []

    def query_quote(self, code, period="1m", count=10):
        return []

    def query_positions(self):
        return []

    def close(self):
        pass


def _seed_ths_session(db):
    # auto 模式：build_engine 才走 make_dispatcher（alert 固定 AlertDataDispatcher）；
    # 策略频率对齐 period 过 auto preflight。
    _seed(db)
    db.query(LiveSession).filter_by(id=1).update(
        {"engine": "ths", "execution_mode": "auto"})
    db.query(Strategy).filter_by(id=1).update({"signal_frequency": "1d"})
    db.commit()


def _patch_ths_cfg(monkeypatch):
    """隔离本机 config.yaml（真机 captcha_ocr=True 会穿透 OCR 闸）。"""
    monkeypatch.setattr(live_service, "load_config",
                        lambda: {"ths_ui": {"enabled": True, "captcha_ocr": False}})


def test_start_session_ths_file_mode_allows_missing_ocr(monkeypatch, tmp_path):
    """file_builtin 模式：OCR 未启用不阻断启动（守卫在执行器，失败落 rejected 回执）。"""
    factory = _factory()
    db = factory()
    _seed_ths_session(db)
    _patch_ths_cfg(monkeypatch)
    monkeypatch.setattr(live_service, "make_dispatcher",
                        lambda *a, **kw: _FakeThsDispatcher(
                            execute_mode="file_builtin", work_dir=str(tmp_path)))
    monkeypatch.setattr(live_service, "_preflight_tq_alert", lambda: None)
    monkeypatch.setattr(live_service, "_smoke_formula_outputs",
                        lambda *a, **kw: None)
    engine, session, error = live_service.start_session(
        db, 1, {}, "simulation", db_session_factory=factory)
    assert error is None
    assert engine is not None
    assert engine._file_handoff is not None
    db.close()


def test_start_session_ths_direct_mode_still_requires_ocr(monkeypatch, tmp_path):
    """direct 模式回归：OCR 未启用仍 503 拒启动。"""
    factory = _factory()
    db = factory()
    _seed_ths_session(db)
    _patch_ths_cfg(monkeypatch)
    monkeypatch.setattr(live_service, "make_dispatcher",
                        lambda *a, **kw: _FakeThsDispatcher(
                            execute_mode="direct", work_dir=str(tmp_path)))
    engine, session, error = live_service.start_session(
        db, 1, {}, "simulation", db_session_factory=factory)
    assert engine is None
    assert error is not None
    assert error[0] == 503
    assert "验证码 OCR" in error[1]
    db.close()
