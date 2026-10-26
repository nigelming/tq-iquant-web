import pytest

from core.engine.live.alert_dispatcher import AlertDataDispatcher


class FakeQuote:
    def query_quote(self, code, period="1m", count=10):
        return [{"stime": "20261023150000", "close": 12.3}]

    def query_calendar(self):
        return ["20261023"]


def test_alert_dispatcher_delegates_quote_and_calendar(monkeypatch):
    monkeypatch.setattr(
        "core.engine.live.alert_dispatcher.TdxTqDispatcher",
        lambda **kw: FakeQuote(),
    )
    d = AlertDataDispatcher()
    assert d.channel == "alert"
    assert d.query_quote("000001.SZ", "1d", 5)[0]["close"] == 12.3
    assert d.query_calendar() == ["20261023"]


def test_alert_dispatcher_never_places_order(monkeypatch):
    monkeypatch.setattr(
        "core.engine.live.alert_dispatcher.TdxTqDispatcher",
        lambda **kw: FakeQuote(),
    )
    d = AlertDataDispatcher()
    with pytest.raises(RuntimeError):
        d.place_order(None)
    assert d.get_positions() == []
    assert d.get_asset() == {}
