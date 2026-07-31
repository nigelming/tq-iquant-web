"""股票池 API 接口测试 v2（直读通达信模型，TDD）。

v1 monkeypatch 高层 TQData → 从没覆盖 _get_pools 对 SDK 原始 dict 的解析 → 线上炸。
v2 改打 SDK 层：monkeypatch core.tq.data.get_tq 返回 fake tq，fake 返真实 {Code,Name} 结构，
逼着代码穿过 _get_pools/_get_stocks 解析，避免重蹈覆辙。

覆盖：列表（通达信板块 + 本地残留合并 + synced 标记）/ tdx 不可达 / 详情实时 / 详情板块不存在 /
      sync 新建 / sync 更新（重同步）/ sync 未知 code / 本地列表 / 删除 CASCADE / 删除被引用 / 删除 404。
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.main import app
from core.db import get_db
from core.models import Base, StockPool, StockPoolStock, PortfolioStrategy
import core.api.stock_pools as sp_api
import core.tq.data as tq_data
from core.tq.utils import TDXConnectionError


# ---------------------------------------------------------------------------
# fake tq —— 模拟 tqcenter.tq 板块相关方法，返真实 SDK dict 结构
# ---------------------------------------------------------------------------
class FakeTq:
    def __init__(self, sectors, stocks_map=None):
        self._sectors = sectors          # [{"Code","Name"}]
        self._stocks = stocks_map or {}  # {block_code: [{"Code","Name"}]}

    def get_user_sector(self):
        return self._sectors

    def get_stock_list_in_sector(self, block_code, block_type=0, list_type=0):
        return self._stocks.get(block_code, [])


@pytest.fixture
def client(tmp_path):
    """内存 SQLite + TestClient，覆盖 get_db。
    StaticPool + PRAGMA foreign_keys=ON 让 CASCADE/RESTRICT 生效（同 db.py / test_formula_api.py）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c, Session
    app.dependency_overrides.clear()


def _patch_tq(monkeypatch, sectors, stocks_map=None):
    """patch core.tq.data.get_tq 返回 FakeTq（sectors/stocks 用真实 {Code,Name} 结构）。"""
    fake = FakeTq(sectors, stocks_map or {})
    monkeypatch.setattr(tq_data, "get_tq", lambda: fake)
    return fake


def _seed_pool(db, code, name, stocks=None, stock_names=None):
    """建本地 StockPool(code) + 成分股，返回 pool_id。
    stocks: list[str] 股票代码；stock_names: 对应名称（可选）。默认 2 只。"""
    if stocks is None:
        stocks = ["000001.SZ", "600519.SH"]
    p = StockPool(code=code, name=name)
    db.add(p)
    db.flush()
    for i, code_s in enumerate(stocks):
        nm = (stock_names[i] if stock_names and i < len(stock_names) else None)
        db.add(StockPoolStock(pool_id=p.id, stock_code=code_s, stock_name=nm))
    db.commit()
    return p.id


# ---------------------------------------------------------------------------
# GET /api/stock-pools/tdx — 通达信用户板块 + 本地残留合并 + synced 标记
# ---------------------------------------------------------------------------
def test_tdx_list_returns_user_sectors_with_synced_flag(client, monkeypatch):
    """通达信返 3 板块，DB 已同步 1 个 → 列表 3 条，已同步那条 synced=True + stock_count，其余 False/0。"""
    c, Session = client
    db = Session()
    _seed_pool(db, code="TQCS", name="tq自选", stocks=["600000.SH", "000001.SZ"])  # 已同步 2 只
    db.close()

    _patch_tq(monkeypatch, sectors=[
        {"Code": "TQCS", "Name": "tq自选"},
        {"Code": "DEGP", "Name": "第二股票"},
        {"Code": "ETF", "Name": "etf"},
    ])

    resp = c.get("/api/stock-pools/tdx")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert len(data) == 3
    by_code = {d["code"]: d for d in data}
    assert by_code["TQCS"] == {"code": "TQCS", "name": "tq自选", "synced": True, "exists_in_tdx": True, "stock_count": 2}
    assert by_code["DEGP"]["synced"] is False
    assert by_code["DEGP"]["stock_count"] == 0
    assert by_code["DEGP"]["exists_in_tdx"] is True


def test_tdx_list_includes_local_orphans(client, monkeypatch):
    """通达信返 [TQCS]，DB 有 TQCS + DEGP（DEGP 通达信已删）→ 列表 2 条，DEGP exists_in_tdx=False。"""
    c, Session = client
    db = Session()
    _seed_pool(db, code="TQCS", name="tq自选", stocks=["600000.SH"])
    _seed_pool(db, code="DEGP", name="第二股票", stocks=["000001.SZ", "000002.SZ"])
    db.close()

    _patch_tq(monkeypatch, sectors=[{"Code": "TQCS", "Name": "tq自选"}])

    resp = c.get("/api/stock-pools/tdx")
    data = resp.json()["data"]
    assert len(data) == 2
    by_code = {d["code"]: d for d in data}
    assert by_code["TQCS"]["exists_in_tdx"] is True
    assert by_code["DEGP"]["exists_in_tdx"] is False   # 通达信已删
    assert by_code["DEGP"]["synced"] is True            # 本地还在
    assert by_code["DEGP"]["stock_count"] == 2


def test_tdx_list_tdx_unreachable(client, monkeypatch):
    """get_tq 抛 TDXConnectionError → {"code":500, message 含通达信未启动}。"""
    c, Session = client
    monkeypatch.setattr(tq_data, "get_tq", lambda: (_ for _ in ()).throw(TDXConnectionError("no tdx")))

    resp = c.get("/api/stock-pools/tdx")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 500
    assert "通达信" in body["message"] or "连接" in body["message"]


# ---------------------------------------------------------------------------
# GET /api/stock-pools/tdx/{code}/stocks — 通达信成分股实时
# ---------------------------------------------------------------------------
def test_tdx_stocks_returns_realtime(client, monkeypatch):
    """板块存在 → 返实时成分股 [{stock_code,stock_name}]。"""
    c, Session = client
    _patch_tq(monkeypatch,
        sectors=[{"Code": "TQCS", "Name": "tq自选"}],
        stocks_map={"TQCS": [
            {"Code": "600000.SH", "Name": "浦发银行"},
            {"Code": "000001.SZ", "Name": "平安银行"},
        ]},
    )

    resp = c.get("/api/stock-pools/tdx/TQCS/stocks")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == [
        {"stock_code": "600000.SH", "stock_name": "浦发银行"},
        {"stock_code": "000001.SZ", "stock_name": "平安银行"},
    ]


def test_tdx_stocks_sector_not_in_tdx(client, monkeypatch):
    """板块在通达信不存在 → {"code":404, 板块不存在}。"""
    c, Session = client
    _patch_tq(monkeypatch,
        sectors=[{"Code": "TQCS", "Name": "tq自选"}],
        stocks_map={"TQCS": []},
    )

    resp = c.get("/api/stock-pools/tdx/NOPE/stocks")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 404
    assert "板块" in body["message"]


def test_tdx_stocks_tdx_unreachable(client, monkeypatch):
    """get_tq 抛异常 → {"code":500}。"""
    c, Session = client
    monkeypatch.setattr(tq_data, "get_tq", lambda: (_ for _ in ()).throw(TDXConnectionError("no tdx")))

    resp = c.get("/api/stock-pools/tdx/TQCS/stocks")
    assert resp.json()["code"] == 500


# ---------------------------------------------------------------------------
# POST /api/stock-pools/sync — upsert 本地池 + 全量替换成分股
# ---------------------------------------------------------------------------
def test_sync_creates_new_pool(client, monkeypatch):
    """sync 新板块 → DB 新建 StockPool(code=TQCS) + 2 成分股 + synced_at 非 None，返回 id。"""
    c, Session = client
    _patch_tq(monkeypatch,
        sectors=[{"Code": "TQCS", "Name": "tq自选"}],
        stocks_map={"TQCS": [
            {"Code": "600000.SH", "Name": "浦发银行"},
            {"Code": "000001.SZ", "Name": "平安银行"},
        ]},
    )

    resp = c.post("/api/stock-pools/sync", json={"code": "TQCS"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    new_id = body["data"]["id"]
    assert new_id > 0
    assert body["data"]["code"] == "TQCS"
    assert body["data"]["name"] == "tq自选"
    assert body["data"]["stock_count"] == 2
    assert body["data"]["synced_at"] is not None

    db = Session()
    p = db.get(StockPool, new_id)
    assert p.code == "TQCS"
    assert p.name == "tq自选"
    assert p.synced_at is not None
    stocks = db.query(StockPoolStock).filter_by(pool_id=new_id).all()
    assert {s.stock_code for s in stocks} == {"600000.SH", "000001.SZ"}
    assert {s.stock_name for s in stocks} == {"浦发银行", "平安银行"}
    db.close()


def test_sync_updates_existing_pool(client, monkeypatch):
    """已同步池重同步 → id 不变，name/成分股更新，synced_at 刷新。"""
    c, Session = client
    db = Session()
    old_id = _seed_pool(db, code="TQCS", name="旧名", stocks=["OLD1.SH", "OLD2.SH", "OLD3.SH"])
    db.close()

    _patch_tq(monkeypatch,
        sectors=[{"Code": "TQCS", "Name": "新名"}],
        stocks_map={"TQCS": [
            {"Code": "600000.SH", "Name": "浦发银行"},
            {"Code": "000001.SZ", "Name": "平安银行"},
        ]},
    )

    resp = c.post("/api/stock-pools/sync", json={"code": "TQCS"})

    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["id"] == old_id          # id 不变（upsert）
    assert body["data"]["name"] == "新名"         # name 更新
    assert body["data"]["stock_count"] == 2       # 旧的 3 只换成新的 2 只

    db = Session()
    p = db.get(StockPool, old_id)
    assert p.name == "新名"
    stocks = db.query(StockPoolStock).filter_by(pool_id=old_id).all()
    assert len(stocks) == 2
    assert {s.stock_code for s in stocks} == {"600000.SH", "000001.SZ"}
    db.close()


def test_sync_unknown_code(client, monkeypatch):
    """sync 通达信不存在的 code → {"code":404, 板块不存在}。"""
    c, Session = client
    _patch_tq(monkeypatch, sectors=[{"Code": "TQCS", "Name": "tq自选"}])

    resp = c.post("/api/stock-pools/sync", json={"code": "NOPE"})

    assert resp.json()["code"] == 404
    assert "板块" in resp.json()["message"]


def test_sync_tdx_unreachable(client, monkeypatch):
    """sync 时通达信不可达 → {"code":500}。"""
    c, Session = client
    monkeypatch.setattr(tq_data, "get_tq", lambda: (_ for _ in ()).throw(TDXConnectionError("no tdx")))

    resp = c.post("/api/stock-pools/sync", json={"code": "TQCS"})
    assert resp.json()["code"] == 500


# ---------------------------------------------------------------------------
# GET /api/stock-pools — 本地已同步池（供组合策略引用）
# ---------------------------------------------------------------------------
def test_list_local_pools(client):
    """本地 2 个已同步池 → GET /stock-pools 返 [{id,code,name,synced_at,stock_count}]。"""
    c, Session = client
    db = Session()
    _seed_pool(db, code="TQCS", name="tq自选", stocks=["600000.SH"])
    _seed_pool(db, code="ETF", name="etf", stocks=["510300.SH", "510500.SH"])
    db.close()

    resp = c.get("/api/stock-pools")

    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert len(data) == 2
    codes = {d["code"] for d in data}
    assert codes == {"TQCS", "ETF"}
    for d in data:
        assert {"id", "code", "name", "synced_at", "stock_count"} <= set(d.keys())


def test_list_local_pools_empty(client):
    """无本地池 → 空列表。"""
    c, Session = client
    resp = c.get("/api/stock-pools")
    assert resp.json() == {"code": 0, "data": []}


# ---------------------------------------------------------------------------
# DELETE /api/stock-pools/{id} — 删本地池（CASCADE 删成分股）
# ---------------------------------------------------------------------------
def test_delete_pool_cascades_stocks(client):
    """DELETE 池 → 池 + 成分股都没了。"""
    c, Session = client
    db = Session()
    pid = _seed_pool(db, code="TQCS", name="tq自选", stocks=["600000.SH", "000001.SZ"])
    db.close()

    resp = c.delete(f"/api/stock-pools/{pid}")

    assert resp.status_code == 200
    assert resp.json()["code"] == 0

    db = Session()
    assert db.get(StockPool, pid) is None
    assert db.query(StockPoolStock).filter_by(pool_id=pid).count() == 0
    db.close()


def test_delete_pool_referenced_by_strategy(client):
    """池被组合策略引用（ondelete=RESTRICT）→ {"code":409}。"""
    c, Session = client
    db = Session()
    pid = _seed_pool(db, code="TQCS", name="tq自选", stocks=["600000.SH"])
    ps = PortfolioStrategy(name="ps", stock_pool_id=pid)
    db.add(ps)
    db.commit()
    db.close()

    resp = c.delete(f"/api/stock-pools/{pid}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 409
    assert "引用" in body["message"] or "无法删除" in body["message"]

    # 池仍在
    db = Session()
    assert db.get(StockPool, pid) is not None
    db.close()


def test_delete_pool_not_found(client):
    """DELETE 不存在 id → {"code":404}。"""
    c, Session = client
    resp = c.delete("/api/stock-pools/9999")
    assert resp.json()["code"] == 404
