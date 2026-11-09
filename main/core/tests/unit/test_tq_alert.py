from contextlib import nullcontext

from core.tq.alert import TQAlert, WarnItem


class FakeTQ:
    def __init__(self, error_id="0", has_warn=True):
        self.calls = []
        self.error_id = error_id
        if has_warn:
            self.send_warn = self._send_warn

    def _send_warn(self, **kwargs):
        self.calls.append(kwargs)
        return {"ErrorId": self.error_id, "Error": "", "run_id": str(len(self.calls))}

    def send_message(self, msg):
        self.calls.append({"message": msg})
        return {"ErrorId": "0"}


def _patch(monkeypatch, fake):
    monkeypatch.setattr("core.tq.alert.get_tq", lambda: fake)
    monkeypatch.setattr("core.tq.alert.get_tdx_lock", lambda: nullcontext())


def test_send_warn_chunks_50_and_truncates_reason(monkeypatch):
    fake = FakeTQ()
    _patch(monkeypatch, fake)
    items = [
        WarnItem(stock_code="000001.SZ", time="20261023090000",
                 price="12.34", close="12.34", bs_flag="0",
                 reason="很长的公式名-很长的信号名-超过二十五个汉字需要截断")
        for _ in range(51)
    ]
    result = TQAlert().send_warn(items)
    assert result["ok"] is True
    assert result["batches"] == 2
    assert fake.calls[0]["count"] == 50
    assert fake.calls[1]["count"] == 1
    assert len(fake.calls[0]["reason_list"][0]) == 25
    assert fake.calls[0]["warn_type_list"][0] == "0"


def test_send_warn_returns_failure_on_errorid(monkeypatch):
    fake = FakeTQ(error_id="7")
    _patch(monkeypatch, fake)
    result = TQAlert().send_warn([
        WarnItem(stock_code="000001.SZ", time="20261023090000",
                 price="1", close="1", bs_flag="1", reason="x"),
    ])
    assert result["ok"] is False
    assert result["ErrorId"] == "7"


def test_preflight_reports_missing_send_warn(monkeypatch):
    fake = FakeTQ(has_warn=False)
    _patch(monkeypatch, fake)
    assert "send_warn" in TQAlert().preflight()


def test_empty_send_is_skipped(monkeypatch):
    fake = FakeTQ()
    _patch(monkeypatch, fake)
    assert TQAlert().send_warn([])["skipped"] is True
    assert fake.calls == []
