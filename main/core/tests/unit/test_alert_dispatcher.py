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


class FakeBatchQuote(FakeQuote):
    supports_batch_quote = True

    def __init__(self):
        self.batch_calls = []

    def query_quotes_batch(self, codes, period="1m", count=10):
        self.batch_calls.append((list(codes), period, count))
        return {c: [{"stime": "20261023150000", "close": 12.3}] for c in codes}


def test_alert_dispatcher_supports_batch_quote(monkeypatch):
    fake = FakeBatchQuote()
    monkeypatch.setattr(
        "core.engine.live.alert_dispatcher.TdxTqDispatcher",
        lambda **kw: fake,
    )
    d = AlertDataDispatcher()
    assert d.supports_batch_quote is True
    out = d.query_quotes_batch(["000001.SZ", "600000.SH"], "1m", 10)
    assert fake.batch_calls == [(["000001.SZ", "600000.SH"], "1m", 10)]
    assert set(out) == {"000001.SZ", "600000.SH"}
    assert out["600000.SH"][0]["close"] == 12.3


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
