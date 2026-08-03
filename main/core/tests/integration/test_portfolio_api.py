"""组合策略管理 CRUD 接口测试（TDD）。

覆盖组合（PortfolioStrategy）+ 嵌套子策略（Strategy）的
GET 列表 / GET 详情 / POST 创建 / PUT 编辑 / DELETE 删除。
复用 test_formula_api.py 的 client fixture 模式：内存 SQLite + StaticPool +
按函数对象覆盖 get_db。PRAGMA foreign_keys=ON 让 CASCADE/RESTRICT 生效。

主从策略配置期校验（运行时联动不在 CRUD 范围）：
- role=slave → master_strategy_id 必填且指向同 portfolio 下 role=master 的策略
- role=master/independent → master_strategy_id 必须为 NULL
- 删 master 前 check 无 slave 引用

自引用时序：后端两步 commit —— 先 insert 全部子策略拿 id，再 UPDATE master_strategy_id。
"""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.main import app
from core.db import get_db
from core.models import (
    Base, PortfolioStrategy, Strategy, StockPool, Formula,
)


@pytest.fixture
def client(tmp_path):
    """内存 SQLite + TestClient，覆盖 get_db。
    StaticPool 共享单连接（sqlite :memory: 每连接独立库）。
    开 PRAGMA foreign_keys=ON 让 CASCADE/RESTRICT 生效（同 db.py 生产配置）。"""
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


def _seed_pool(db, code="TQCS", name="tq自选"):
    """建 StockPool（PortfolioStrategy FK 依赖）。"""
    p = StockPool(code=code, name=name)
    db.add(p); db.flush(); db.commit()
    return p.id


def _seed_formula(db, name="F1"):
    """建 Formula（Strategy.formula_id FK 依赖）。"""
    f = Formula(name=name, content="REF(CLOSE,1)")
    db.add(f); db.flush(); db.commit()
    return f.id


def _seed_portfolio(db, name="PS1", strategies=None):
    """建 PortfolioStrategy + 若干 Strategy，返回 pid。
    strategies: [(name, role, master_idx)]，master_idx 指向同批 strategies 的索引（从策略用）。
    为简化 seed，这里 master_strategy_id 留空（直接 ORM 建，不走两步 commit）。"""
    if strategies is None:
        strategies = [("S1", "independent", None)]
    pid_pool = _seed_pool(db)
    fid = _seed_formula(db)
    p = PortfolioStrategy(
        name=name, stock_pool_id=pid_pool,
        initial_capital=Decimal("100000"), max_drawdown=Decimal("0.2"),
        daily_loss_limit=Decimal("0.05"),
    )
    db.add(p); db.flush()
    for sname, role, _ in strategies:
        db.add(Strategy(
            portfolio_id=p.id, name=sname, formula_id=fid,
            period="1d", role=role,
        ))
    db.commit()
    return p.id


# ---------------------------------------------------------------------------
# GET /api/portfolios — 列表，每条附 strategies 子列表
# ---------------------------------------------------------------------------
def test_list_portfolios_empty(client):
    c, Session = client
    assert c.get("/api/portfolios").json() == {"code": 0, "data": []}


def test_list_portfolios_returns_seeded_with_strategies(client):
    c, Session = client
    db = Session()
    pid = _seed_portfolio(db, strategies=[("S1", "independent", None), ("S2", "independent", None)])
    db.close()

    body = c.get("/api/portfolios").json()
    assert body["code"] == 0
    assert len(body["data"]) == 1
    item = body["data"][0]
    assert item["name"] == "PS1"
    assert "strategies" in item
    assert len(item["strategies"]) == 2
    assert item["strategies"][0]["name"] == "S1"


# ---------------------------------------------------------------------------
# GET /api/portfolios/{pid} — 详情
# ---------------------------------------------------------------------------
def test_get_portfolio_detail(client):
    c, Session = client
    db = Session()
    pid = _seed_portfolio(db)
    db.close()

    body = c.get(f"/api/portfolios/{pid}").json()
    assert body["code"] == 0
    assert body["data"]["id"] == pid
    assert len(body["data"]["strategies"]) == 1


def test_get_portfolio_not_found(client):
    c, Session = client
    body = c.get("/api/portfolios/9999").json()
    assert body["code"] == 404


# ---------------------------------------------------------------------------
# POST /api/portfolios — 创建
# ---------------------------------------------------------------------------
def _create_payload(**overrides):
    """构造最小可用的创建请求。"""
    base = {
        "name": "NEW_PS",
        "stock_pool_id": 1,  # 测试里先 seed pool id=1
        "initial_capital": 100000,
        "max_drawdown": 0.2,
        "daily_loss_limit": 0.05,
        "max_holdings": 10,
        "trading_session": "full",
        "status": "active",
        "strategies": [
            {"name": "S1", "formula_id": 1, "period": "1d", "role": "independent",
             "capital_ratio": 0.6, "max_positions": 5},
        ],
    }
    base.update(overrides)
    return base


def test_create_portfolio_with_strategies(client):
    c, Session = client
    db = Session()
    _seed_pool(db); _seed_formula(db)
    db.close()

    body = c.post("/api/portfolios", json=_create_payload()).json()
    assert body["code"] == 0
    data = body["data"]
    assert data["id"] is not None
    assert data["name"] == "NEW_PS"
    assert len(data["strategies"]) == 1
    assert data["strategies"][0]["role"] == "independent"

    # 落库校验
    db = Session()
    assert db.query(PortfolioStrategy).count() == 1
    assert db.query(Strategy).count() == 1
    db.close()


def test_create_portfolio_pool_not_found(client):
    c, Session = client
    db = Session()
    _seed_formula(db)  # 不 seed pool
    db.close()

    body = c.post("/api/portfolios", json=_create_payload(stock_pool_id=999)).json()
    assert body["code"] == 400
    assert "股票池" in body["message"]


def test_create_portfolio_formula_not_found(client):
    c, Session = client
    db = Session()
    _seed_pool(db)  # 不 seed formula
    db.close()

    payload = _create_payload()
    payload["strategies"][0]["formula_id"] = 999
    body = c.post("/api/portfolios", json=payload).json()
    assert body["code"] == 400
    assert "公式" in body["message"]


def test_create_portfolio_invalid_period(client):
    c, Session = client
    db = Session()
    _seed_pool(db); _seed_formula(db)
    db.close()

    payload = _create_payload()
    payload["strategies"][0]["period"] = "15m"  # 文档无 15m
    body = c.post("/api/portfolios", json=payload).json()
    assert body["code"] == 400
    assert "period" in body["message"]


def test_create_portfolio_invalid_trading_session(client):
    c, Session = client
    db = Session()
    _seed_pool(db); _seed_formula(db)
    db.close()

    body = c.post("/api/portfolios", json=_create_payload(trading_session="morning")).json()
    assert body["code"] == 400
    assert "trading_session" in body["message"]


def test_create_portfolio_capital_ratio_out_of_range(client):
    c, Session = client
    db = Session()
    _seed_pool(db); _seed_formula(db)
    db.close()

    payload = _create_payload()
    payload["strategies"][0]["capital_ratio"] = 1.5  # >1
    body = c.post("/api/portfolios", json=payload).json()
    assert body["code"] == 400


# ---------------------------------------------------------------------------
# POST — 主从策略配置（两步 commit + 配置期校验）
# ---------------------------------------------------------------------------
def test_create_portfolio_master_slave_two_step_commit(client):
    """主+从同批提交：从策略 master_strategy_id 指向同批主策略。
    后端两步 commit 后，从策略 master_strategy_id 应正确落库为 master 的真实 id。"""
    c, Session = client
    db = Session()
    _seed_pool(db); _seed_formula(db)
    db.close()

    payload = _create_payload(strategies=[
        {"name": "MASTER", "formula_id": 1, "period": "1d", "role": "master",
         "capital_ratio": 0.6, "max_positions": 5, "master_strategy_id": None},
        {"name": "SLAVE", "formula_id": 1, "period": "1d", "role": "slave",
         "capital_ratio": 0.4, "max_positions": 5, "master_strategy_id": 0},  # 0=指向本批第0个
    ])
    body = c.post("/api/portfolios", json=payload).json()
    assert body["code"] == 0
    strategies = body["data"]["strategies"]
    assert len(strategies) == 2
    master = next(s for s in strategies if s["name"] == "MASTER")
    slave = next(s for s in strategies if s["name"] == "SLAVE")
    assert master["role"] == "master"
    assert slave["role"] == "slave"
    # 从策略 master_strategy_id 指向 master 的真实 id（非 0）
    assert slave["master_strategy_id"] == master["id"]

    # 落库校验
    db = Session()
    db_slave = db.query(Strategy).filter(Strategy.name == "SLAVE").first()
    db_master = db.query(Strategy).filter(Strategy.name == "MASTER").first()
    assert db_slave.master_strategy_id == db_master.id
    db.close()


def test_create_portfolio_slave_without_master_rejected(client):
    """role=slave 但 master_strategy_id=None → 400。"""
    c, Session = client
    db = Session()
    _seed_pool(db); _seed_formula(db)
    db.close()

    payload = _create_payload(strategies=[
        {"name": "S1", "formula_id": 1, "period": "1d", "role": "slave",
         "capital_ratio": 0.4, "max_positions": 5, "master_strategy_id": None},
    ])
    body = c.post("/api/portfolios", json=payload).json()
    assert body["code"] == 400
    assert "master" in body["message"].lower() or "主策略" in body["message"]


def test_create_portfolio_master_with_master_id_rejected(client):
    """role=master 但带 master_strategy_id → 400。"""
    c, Session = client
    db = Session()
    _seed_pool(db); _seed_formula(db)
    db.close()

    payload = _create_payload(strategies=[
        {"name": "M", "formula_id": 1, "period": "1d", "role": "master",
         "capital_ratio": 0.6, "max_positions": 5, "master_strategy_id": 0},
    ])
    body = c.post("/api/portfolios", json=payload).json()
    assert body["code"] == 400


def test_create_portfolio_slave_master_not_master_role(client):
    """slave 的 master 指向本批另一个 slave（非 master 角色）→ 400。"""
    c, Session = client
    db = Session()
    _seed_pool(db); _seed_formula(db)
    db.close()

    payload = _create_payload(strategies=[
        {"name": "S1", "formula_id": 1, "period": "1d", "role": "independent",
         "capital_ratio": 0.6, "max_positions": 5, "master_strategy_id": None},
        {"name": "S2", "formula_id": 1, "period": "1d", "role": "slave",
         "capital_ratio": 0.4, "max_positions": 5, "master_strategy_id": 0},  # 指向 independent
    ])
    body = c.post("/api/portfolios", json=payload).json()
    assert body["code"] == 400


# ---------------------------------------------------------------------------
# PUT /api/portfolios/{pid} — 编辑（子表全量替换）
# ---------------------------------------------------------------------------
def test_update_portfolio_replaces_strategies(client):
    c, Session = client
    db = Session()
    pid = _seed_portfolio(db, strategies=[("S1", "independent", None), ("S2", "independent", None)])
    db.close()

    # 编辑为 3 个全新子策略
    payload = _create_payload(name="PS1", stock_pool_id=1, strategies=[
        {"name": "NEW1", "formula_id": 1, "period": "1d", "role": "independent",
         "capital_ratio": 0.6, "max_positions": 5},
        {"name": "NEW2", "formula_id": 1, "period": "5m", "role": "independent",
         "capital_ratio": 0.4, "max_positions": 5},
        {"name": "NEW3", "formula_id": 1, "period": "30m", "role": "independent",
         "capital_ratio": 0.5, "max_positions": 5},
    ])
    body = c.put(f"/api/portfolios/{pid}", json=payload).json()
    assert body["code"] == 0
    strategies = body["data"]["strategies"]
    assert len(strategies) == 3
    names = {s["name"] for s in strategies}
    assert names == {"NEW1", "NEW2", "NEW3"}

    # 旧的 S1/S2 删干净
    db = Session()
    assert db.query(Strategy).filter(Strategy.portfolio_id == pid).count() == 3
    assert db.query(Strategy).filter(Strategy.name.in_(["S1", "S2"])).count() == 0
    db.close()


def test_update_portfolio_not_found(client):
    c, Session = client
    db = Session()
    _seed_pool(db); _seed_formula(db)
    db.close()

    body = c.put("/api/portfolios/9999", json=_create_payload()).json()
    assert body["code"] == 404


# ---------------------------------------------------------------------------
# DELETE /api/portfolios/{pid} — 删除（CASCADE + master 引用检查）
# ---------------------------------------------------------------------------
def test_delete_portfolio_cascades_strategies(client):
    c, Session = client
    db = Session()
    pid = _seed_portfolio(db, strategies=[("S1", "independent", None)])
    db.close()

    body = c.delete(f"/api/portfolios/{pid}").json()
    assert body["code"] == 0

    db = Session()
    assert db.get(PortfolioStrategy, pid) is None
    assert db.query(Strategy).filter(Strategy.portfolio_id == pid).count() == 0
    db.close()


def test_delete_portfolio_not_found(client):
    c, Session = client
    body = c.delete("/api/portfolios/9999").json()
    assert body["code"] == 404


def test_delete_master_strategy_blocked_by_slave(client):
    """独立删除被从策略引用的主策略 → 400（防孤儿引用）。"""
    c, Session = client
    db = Session()
    pid_pool = _seed_pool(db)
    fid = _seed_formula(db)
    p = PortfolioStrategy(
        name="ps", stock_pool_id=pid_pool, initial_capital=Decimal("100000"),
        max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"),
    )
    db.add(p); db.flush()
    master = Strategy(portfolio_id=p.id, name="M", formula_id=fid, period="1d", role="master")
    db.add(master); db.flush()
    slave = Strategy(portfolio_id=p.id, name="S", formula_id=fid, period="1d",
                     role="slave", master_strategy_id=master.id)
    db.add(slave)
    db.commit()
    master_id = master.id
    pid = p.id
    db.close()

    body = c.delete(f"/api/portfolios/{pid}/strategies/{master_id}").json()
    assert body["code"] == 400
    assert "引用" in body["message"] or "从策略" in body["message"]

    # 主策略仍在
    db = Session()
    assert db.get(Strategy, master_id) is not None
    db.close()


# ===========================================================================
# 独立子策略 CRUD — /api/portfolios/{pid}/strategies
# 两层设计：组合与子策略分开管理。新建/编辑/删除单个子策略。
# ===========================================================================
def _strategy_payload(**overrides):
    """单个子策略请求体。"""
    base = {
        "name": "S1", "formula_id": 1, "period": "1d", "role": "independent",
        "master_strategy_id": None, "capital_ratio": 0.6, "max_positions": 5,
        "single_open_ratio": 0.1, "stop_loss_ratio": 0.05, "take_profit_ratio": 0.15,
        "trailing_stop_ratio": 0.03, "add_position_threshold": 0.05, "max_add_count": 2,
        "add_position_ratio": 0.1, "reduce_position_ratio": 0.3,
    }
    base.update(overrides)
    return base


def test_list_strategies(client):
    """GET /api/portfolios/{pid}/strategies — 列出某组合的子策略。"""
    c, Session = client
    db = Session()
    pid = _seed_portfolio(db, strategies=[("S1", "independent", None), ("S2", "independent", None)])
    db.close()

    body = c.get(f"/api/portfolios/{pid}/strategies").json()
    assert body["code"] == 0
    assert len(body["data"]) == 2


def test_list_strategies_portfolio_not_found(client):
    c, Session = client
    assert c.get("/api/portfolios/9999/strategies").json()["code"] == 404


def test_create_strategy(client):
    """POST — 在某组合下新建单个子策略。"""
    c, Session = client
    db = Session()
    pid = _seed_portfolio(db, strategies=[])  # 空子策略
    db.close()

    body = c.post(f"/api/portfolios/{pid}/strategies", json=_strategy_payload(name="NEW_S")).json()
    assert body["code"] == 0
    assert body["data"]["name"] == "NEW_S"
    assert body["data"]["portfolio_id"] == pid

    db = Session()
    assert db.query(Strategy).filter(Strategy.portfolio_id == pid).count() == 1
    db.close()


def test_create_strategy_portfolio_not_found(client):
    c, Session = client
    db = Session()
    _seed_formula(db)
    db.close()

    body = c.post("/api/portfolios/9999/strategies", json=_strategy_payload()).json()
    assert body["code"] == 404


def test_create_strategy_invalid_period(client):
    c, Session = client
    db = Session()
    pid = _seed_portfolio(db, strategies=[])
    db.close()

    body = c.post(f"/api/portfolios/{pid}/strategies", json=_strategy_payload(period="15m")).json()
    assert body["code"] == 400


def test_create_strategy_formula_not_found(client):
    c, Session = client
    db = Session()
    pid = _seed_portfolio(db, strategies=[])
    db.close()

    body = c.post(f"/api/portfolios/{pid}/strategies", json=_strategy_payload(formula_id=999)).json()
    assert body["code"] == 400


def test_create_strategy_slave_with_master(client):
    """独立新建 slave：master_strategy_id 指向已存在的同组合 master id。"""
    c, Session = client
    db = Session()
    pid = _seed_portfolio(db, strategies=[("M", "master", None)])
    db.close()

    # 取 master id
    db = Session()
    master = db.query(Strategy).filter(Strategy.portfolio_id == pid, Strategy.role == "master").first()
    master_id = master.id
    db.close()

    body = c.post(f"/api/portfolios/{pid}/strategies", json=_strategy_payload(
        name="SLAVE", role="slave", master_strategy_id=master_id,
    )).json()
    assert body["code"] == 0
    assert body["data"]["master_strategy_id"] == master_id


def test_create_strategy_slave_without_master_rejected(client):
    c, Session = client
    db = Session()
    pid = _seed_portfolio(db, strategies=[])
    db.close()

    body = c.post(f"/api/portfolios/{pid}/strategies", json=_strategy_payload(
        role="slave", master_strategy_id=None,
    )).json()
    assert body["code"] == 400


def test_create_strategy_slave_master_wrong_portfolio(client):
    """slave 的 master 指向另一组合的策略 → 400。"""
    c, Session = client
    db = Session()
    pid1 = _seed_portfolio(db, strategies=[("M", "master", None)])
    pid2 = _seed_portfolio(db, strategies=[])
    # 取 pid1 的 master id
    master_id = db.query(Strategy).filter(Strategy.portfolio_id == pid1).first().id
    db.close()

    body = c.post(f"/api/portfolios/{pid2}/strategies", json=_strategy_payload(
        role="slave", master_strategy_id=master_id,
    )).json()
    assert body["code"] == 400


def test_update_strategy(client):
    """PUT — 编辑单个子策略。"""
    c, Session = client
    db = Session()
    pid = _seed_portfolio(db, strategies=[("S1", "independent", None)])
    sid = db.query(Strategy).filter(Strategy.portfolio_id == pid).first().id
    db.close()

    body = c.put(f"/api/portfolios/{pid}/strategies/{sid}", json=_strategy_payload(
        name="RENAMED", capital_ratio=0.8,
    )).json()
    assert body["code"] == 0
    assert body["data"]["name"] == "RENAMED"
    assert body["data"]["capital_ratio"] == 0.8


def test_update_strategy_not_found(client):
    c, Session = client
    db = Session()
    pid = _seed_portfolio(db, strategies=[])
    db.close()

    body = c.put(f"/api/portfolios/{pid}/strategies/9999", json=_strategy_payload()).json()
    assert body["code"] == 404


def test_delete_strategy(client):
    """DELETE — 删除单个子策略（无引用时成功）。"""
    c, Session = client
    db = Session()
    pid = _seed_portfolio(db, strategies=[("S1", "independent", None)])
    sid = db.query(Strategy).filter(Strategy.portfolio_id == pid).first().id
    db.close()

    body = c.delete(f"/api/portfolios/{pid}/strategies/{sid}").json()
    assert body["code"] == 0

    db = Session()
    assert db.get(Strategy, sid) is None
    db.close()


def test_delete_strategy_not_found(client):
    c, Session = client
    db = Session()
    pid = _seed_portfolio(db, strategies=[])
    db.close()

    body = c.delete(f"/api/portfolios/{pid}/strategies/9999").json()
    assert body["code"] == 404


def test_delete_strategy_referenced_by_backtest_trade(client):
    """删被回测交易引用的子策略 → 不应 500，应给出可读错误或先清引用。

    backtest_trades.strategy_id 无 ondelete（默认 RESTRICT），直删触发 IntegrityError。
    修复后端点应捕获并返回 400/409 可读消息，而非 500。"""
    from core.models import BacktestRecord, BacktestTrade
    from datetime import datetime
    c, Session = client
    db = Session()
    pid = _seed_portfolio(db, strategies=[("S1", "independent", None)])
    sid = db.query(Strategy).filter(Strategy.portfolio_id == pid).first().id
    # 建一条回测记录 + 交易引用该策略
    rec = BacktestRecord(
        portfolio_strategy_id=pid, name="bt1",
        start_date=datetime(2026, 7, 1).date(), end_date=datetime(2026, 7, 31).date(),
        status="completed", progress=100,
    )
    db.add(rec); db.flush()
    db.add(BacktestTrade(
        backtest_record_id=rec.id, strategy_id=sid,
        signal_name="open_sig", signal_type="OPEN",
        stock_code="000001.SZ", trade_type="BUY",
        price=Decimal("10"), quantity=100, amount=Decimal("1000"),
        commission=Decimal("5"), stamp_duty=Decimal("0"),
        bar_time=datetime(2026, 7, 2, 9, 30),
    ))
    db.commit()
    db.close()

    resp = c.delete(f"/api/portfolios/{pid}/strategies/{sid}")
    # 修复前：500（IntegrityError 未捕获上抛）。修复后：可读 400/409。
    assert resp.status_code != 500
    body = resp.json()
    assert body["code"] != 0  # 不是成功
