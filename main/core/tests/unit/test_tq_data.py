"""TQData 板块/成分股解析单测（TDD）。

打 SDK 层（core.tq.data.get_tq 返回 fake tq），覆盖 _get_pools/_get_stocks 对
tqcenter 真实返回 dict 的解析——v1 因 monkeypatch 高层 TQData 导致这层从没被测，
线上 TypeError: unhashable type: 'dict'。

SDK 真实格式（探查实证）：
- get_user_sector() → [{"Code":..., "Name":...}]
- get_stock_list_in_sector(code, block_type=1, list_type=1) → [{"Code":..,"Name":..}]
"""
import pytest

from core.tq.data import TQData
import core.tq.data as tq_data


class FakeTq:
    """模拟 tqcenter.tq 的板块相关方法，返回真实 SDK dict 结构。"""

    def __init__(self, sectors, stocks_map):
        self._sectors = sectors          # [{"Code","Name"}]
        self._stocks = stocks_map        # {block_code: [{"Code","Name"}]}
        self.last_call = None            # 记录 get_stock_list_in_sector 调用参数

    def get_user_sector(self):
        return self._sectors

    def get_stock_list_in_sector(self, block_code, block_type=0, list_type=0):
        self.last_call = {
            "block_code": block_code,
            "block_type": block_type,
            "list_type": list_type,
        }
        return self._stocks.get(block_code, [])


@pytest.fixture
def patch_tq(monkeypatch):
    """返回一个工厂：设置 fake tq 并 patch core.tq.data.get_tq。"""
    def _setup(sectors, stocks_map=None):
        stocks_map = stocks_map or {}
        fake = FakeTq(sectors, stocks_map)
        monkeypatch.setattr(tq_data, "get_tq", lambda: fake)
        return fake
    return _setup


# ---------------------------------------------------------------------------
# get_stock_pools — 解析 get_user_sector 的 {Code,Name} dict
# ---------------------------------------------------------------------------
def test_get_pools_parses_user_sector(patch_tq):
    """get_user_sector 返 {Code,Name} dict 列表 → get_stock_pools 归一化为 [{code,name}]。"""
    patch_tq(sectors=[
        {"Code": "TQCS", "Name": "tq自选"},
        {"Code": "DEGP", "Name": "第二股票"},
    ])

    pools = TQData().get_stock_pools()

    assert pools == [
        {"code": "TQCS", "name": "tq自选"},
        {"code": "DEGP", "name": "第二股票"},
    ]


def test_get_pools_empty(patch_tq):
    """通达信无用户板块 → 返 []。"""
    patch_tq(sectors=[])
    assert TQData().get_stock_pools() == []


# ---------------------------------------------------------------------------
# get_pool_stocks — 传板块 Code，解析成分股 {Code,Name}
# ---------------------------------------------------------------------------
def test_get_stocks_parses_sector_stocks(patch_tq):
    """成分股返 {Code,Name} → 归一化为 [{stock_code,stock_name}]，调参 block_type=1,list_type=1。"""
    fake = patch_tq(
        sectors=[{"Code": "TQCS", "Name": "tq自选"}],
        stocks_map={"TQCS": [
            {"Code": "600000.SH", "Name": "浦发银行"},
            {"Code": "000001.SZ", "Name": "平安银行"},
        ]},
    )

    stocks = TQData().get_pool_stocks("TQCS")

    assert stocks == [
        {"stock_code": "600000.SH", "stock_name": "浦发银行"},
        {"stock_code": "000001.SZ", "stock_name": "平安银行"},
    ]
    # 确认调用 SDK 时传了正确的 block_type / list_type
    assert fake.last_call == {
        "block_code": "TQCS",
        "block_type": 1,
        "list_type": 1,
    }


def test_get_stocks_passes_block_code_not_name(patch_tq):
    """get_pool_stocks 入参是板块 Code（如 TQCS），不是 Name。"""
    fake = patch_tq(
        sectors=[{"Code": "TQCS", "Name": "tq自选"}],
        stocks_map={"TQCS": [{"Code": "600000.SH", "Name": "浦发银行"}]},
    )

    TQData().get_pool_stocks("TQCS")

    assert fake.last_call["block_code"] == "TQCS"


def test_get_stocks_empty(patch_tq):
    """板块存在但无成分股 → 返 []（不报错）。"""
    patch_tq(
        sectors=[{"Code": "TQCS", "Name": "tq自选"}],
        stocks_map={"TQCS": []},
    )
    assert TQData().get_pool_stocks("TQCS") == []


def test_get_stocks_unknown_code(patch_tq):
    """板块 Code 不在 stocks_map → 返 []（SDK 对未知板块返空）。"""
    patch_tq(
        sectors=[{"Code": "TQCS", "Name": "tq自选"}],
        stocks_map={"TQCS": [{"Code": "600000.SH", "Name": "浦发银行"}]},
    )
    assert TQData().get_pool_stocks("NOPE") == []
