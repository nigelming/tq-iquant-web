"""公式管理 CRUD 接口测试（TDD）。

覆盖 GET 列表 / GET 详情 / POST 创建 / PUT 编辑 / DELETE 删除。
复用 test_backtest_api.py 的 client fixture 模式：内存 SQLite + StaticPool +
按函数对象覆盖 get_db。seed 只需 Formula + FormulaSignal（无其他表 FK 依赖）。
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.main import app
from core.db import get_db
from core.models import Base, Formula, FormulaSignal, Strategy, PortfolioStrategy, StockPool


@pytest.fixture
def client(tmp_path):
    """内存 SQLite + TestClient，覆盖 get_db。
    用 StaticPool 共享单连接（sqlite :memory: 每连接独立库）。
    开 PRAGMA foreign_keys=ON 让 FormulaSignal.ondelete=CASCADE 生效（同 db.py 生产配置）。"""
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


def _seed_formula(db, name="MACROSSPRO", signals=None):
    """建一个 Formula + 若干 FormulaSignal，返回 formula_id。"""
    if signals is None:
        signals = [
            ("开仓", "OPEN", 1),
            ("加仓", "ADD", 1),
            ("减仓", "REDUCE", 1),
            ("平仓", "CLOSE", 1),
        ]
    f = Formula(name=name, content="REF(CLOSE,1)")
    db.add(f)
    db.flush()
    for sig_name, sig_type, trig in signals:
        db.add(FormulaSignal(
            formula_id=f.id, signal_name=sig_name,
            signal_type=sig_type, trigger_value=trig,
        ))
    db.commit()
    return f.id


# ---------------------------------------------------------------------------
# GET /api/formulas — 列表，每条附 signals 子列表
# ---------------------------------------------------------------------------
def test_list_formulas_returns_seeded_with_signals(client):
    """seed 公式+4 信号 → GET 列表返回 1 条，含 signals 子列表。"""
    c, Session = client
    db = Session()
    fid = _seed_formula(db)
    db.close()

    resp = c.get("/api/formulas")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert isinstance(data, list)
    assert len(data) == 1
    item = data[0]
    assert item["id"] == fid
    assert item["name"] == "MACROSSPRO"
    assert item["content"] == "REF(CLOSE,1)"
    # signals 子列表
    assert "signals" in item
    assert len(item["signals"]) == 4
    sig_types = {s["signal_type"] for s in item["signals"]}
    assert sig_types == {"OPEN", "ADD", "REDUCE", "CLOSE"}
    # 每个 signal 含完整字段
    s0 = item["signals"][0]
    assert set(s0.keys()) >= {"signal_name", "signal_type", "trigger_value"}


def test_list_formulas_empty(client):
    """无公式 → 空列表（code 0）。"""
    c, Session = client
    resp = c.get("/api/formulas")
    assert resp.status_code == 200
    assert resp.json() == {"code": 0, "message": "ok", "data": []}


# ---------------------------------------------------------------------------
# GET /api/formulas/{id} — 详情 + signals
# ---------------------------------------------------------------------------
def test_get_formula_detail(client):
    """GET 单公式详情含 signals。"""
    c, Session = client
    db = Session()
    fid = _seed_formula(db)
    db.close()

    resp = c.get(f"/api/formulas/{fid}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    item = body["data"]
    assert item["id"] == fid
    assert item["name"] == "MACROSSPRO"
    assert len(item["signals"]) == 4


def test_get_formula_not_found(client):
    """不存在的 id → code 404 + message。"""
    c, Session = client
    resp = c.get("/api/formulas/9999")
    assert resp.status_code == 200  # 信封式 404，HTTP 仍 200
    body = resp.json()
    assert body["code"] == 404
    assert "message" in body


# ---------------------------------------------------------------------------
# POST /api/formulas — 创建公式 + 信号（事务）
# ---------------------------------------------------------------------------
def test_create_formula_empty_signal_name_rejected(client):
    """signal_name 为空 → 400。"""
    c, Session = client
    payload = {
        "name": "EMPTYSIG",
        "content": "MA(CLOSE,5);",
        "signals": [{"signal_name": "  ", "signal_type": "OPEN", "trigger_value": 1}],
    }
    resp = c.post("/api/formulas", json=payload)
    assert resp.status_code == 200
    assert resp.json()["code"] == 400


def test_create_formula_empty_name_rejected(client):
    """公式名为空 → 400。"""
    c, Session = client
    payload = {"name": "  ", "content": "MA(CLOSE,5);", "signals": []}
    resp = c.post("/api/formulas", json=payload)
    assert resp.status_code == 200
    assert resp.json()["code"] == 400


def test_create_formula_with_signals(client):
    """POST 创建公式 + 2 信号 → DB 落库 Formula + 2 FormulaSignal。"""
    c, Session = client
    payload = {
        "name": "NEW_FORMULA",
        "content": "MA(CLOSE,5);",
        "signals": [
            {"signal_name": "开仓", "signal_type": "OPEN", "trigger_value": 1},
            {"signal_name": "平仓", "signal_type": "CLOSE", "trigger_value": -1},
        ],
    }
    resp = c.post("/api/formulas", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    new_id = body["data"]["id"]
    assert new_id > 0

    # DB 校验
    db = Session()
    f = db.get(Formula, new_id)
    assert f is not None
    assert f.name == "NEW_FORMULA"
    assert f.content == "MA(CLOSE,5);"
    sigs = db.query(FormulaSignal).filter_by(formula_id=new_id).all()
    assert len(sigs) == 2
    assert {s.signal_type for s in sigs} == {"OPEN", "CLOSE"}
    db.close()


def test_create_formula_no_signals(client):
    """POST 公式不带信号 → 只建 Formula，0 信号（合法，信号可后续编辑加）。"""
    c, Session = client
    resp = c.post("/api/formulas", json={"name": "EMPTY", "content": "X", "signals": []})
    assert resp.status_code == 200
    assert resp.json()["code"] == 0
    new_id = resp.json()["data"]["id"]
    db = Session()
    assert db.get(Formula, new_id) is not None
    assert db.query(FormulaSignal).filter_by(formula_id=new_id).count() == 0
    db.close()


def test_create_formula_invalid_signal_type(client):
    """signal_type 不在 {OPEN,ADD,REDUCE,CLOSE} → code 400。"""
    c, Session = client
    payload = {
        "name": "BAD", "content": "X",
        "signals": [{"signal_name": "x", "signal_type": "BOGUS", "trigger_value": 1}],
    }
    resp = c.post("/api/formulas", json=payload)
    assert resp.status_code == 200
    assert resp.json()["code"] == 400


def test_create_formula_invalid_trigger_value(client):
    """trigger_value 不在 {-2,-1,1,2} → code 400。"""
    c, Session = client
    payload = {
        "name": "BAD", "content": "X",
        "signals": [{"signal_name": "x", "signal_type": "OPEN", "trigger_value": 5}],
    }
    resp = c.post("/api/formulas", json=payload)
    assert resp.status_code == 200
    assert resp.json()["code"] == 400


def test_create_formula_trigger_zero_rejected(client):
    """trigger_value=0 被拒：0 是公式无信号常态值，配置后每根 bar 都会匹配。"""
    c, Session = client
    payload = {
        "name": "BADZERO", "content": "X",
        "signals": [{"signal_name": "x", "signal_type": "OPEN", "trigger_value": 0}],
    }
    resp = c.post("/api/formulas", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 400
    assert "0" in body["message"]


# ---------------------------------------------------------------------------
# PUT /api/formulas/{id} — 编辑公式 + 信号全量替换
# ---------------------------------------------------------------------------
def test_update_formula_replaces_signals(client):
    """PUT 改名称+内容，信号从 4 个全量替换为 2 个。"""
    c, Session = client
    db = Session()
    fid = _seed_formula(db)  # 4 信号
    db.close()

    payload = {
        "name": "MACROSSPRO_V2",
        "content": "MA(CLOSE,10);",
        "signals": [
            {"signal_name": "买", "signal_type": "OPEN", "trigger_value": 1},
            {"signal_name": "卖", "signal_type": "CLOSE", "trigger_value": 1},
        ],
    }
    resp = c.put(f"/api/formulas/{fid}", json=payload)

    assert resp.status_code == 200
    assert resp.json()["code"] == 0

    db = Session()
    f = db.get(Formula, fid)
    assert f.name == "MACROSSPRO_V2"
    assert f.content == "MA(CLOSE,10);"
    sigs = db.query(FormulaSignal).filter_by(formula_id=fid).all()
    assert len(sigs) == 2  # 旧的 4 个已删，新的 2 个
    assert {s.signal_name for s in sigs} == {"买", "卖"}
    db.close()


def test_update_formula_not_found(client):
    """PUT 不存在 id → code 404。"""
    c, Session = client
    resp = c.put("/api/formulas/9999", json={"name": "X", "content": "Y", "signals": []})
    assert resp.status_code == 200
    assert resp.json()["code"] == 404


# ---------------------------------------------------------------------------
# W4：已移除未来函数关键字预检（strict_future_check）。公式内容原样入库，
# 是否使用未来函数由用户自行判断。
# ---------------------------------------------------------------------------
def test_create_formula_no_future_check(client):
    """含 ZIG 的内容原样入库，不再做未来函数拦截（含旧 strict_future_check 键也忽略）。"""
    c, Session = client
    payload = {
        "name": "FUT_OK", "content": "A:ZIG(3,5);", "signals": [],
        "strict_future_check": True,
    }
    resp = c.post("/api/formulas", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["content"] == "A:ZIG(3,5);"


# ---------------------------------------------------------------------------
# formula_count 字段（任务 #27：注入历史根数，按公式配）
# ---------------------------------------------------------------------------
def test_create_formula_with_formula_count(client):
    """POST 带 formula_count=500 → 落库 + 序列化返回。"""
    c, Session = client
    payload = {
        "name": "MA255_F", "content": "MA(CLOSE,255);",
        "signals": [{"signal_name": "开仓", "signal_type": "OPEN", "trigger_value": 1}],
        "formula_count": 500,
    }
    resp = c.post("/api/formulas", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["formula_count"] == 500

    new_id = body["data"]["id"]
    db = Session()
    f = db.get(Formula, new_id)
    assert f.formula_count == 500
    db.close()


def test_create_formula_default_formula_count_200(client):
    """POST 不带 formula_count → 默认 200（Q4 决策4）。"""
    c, Session = client
    resp = c.post("/api/formulas", json={
        "name": "DEFAULT_F", "content": "X",
        "signals": [],
    })
    assert resp.json()["code"] == 0
    assert resp.json()["data"]["formula_count"] == 200

    db = Session()
    f = db.get(Formula, resp.json()["data"]["id"])
    assert f.formula_count == 200
    db.close()


def test_list_formula_includes_formula_count(client):
    """GET 列表每条含 formula_count（默认 200）。"""
    c, Session = client
    db = Session()
    fid = _seed_formula(db)  # 不带 formula_count → 默认 200
    db.close()

    resp = c.get("/api/formulas")
    item = resp.json()["data"][0]
    assert item["id"] == fid
    assert item["formula_count"] == 200


def test_update_formula_changes_formula_count(client):
    """PUT 改 formula_count → 落库更新。"""
    c, Session = client
    db = Session()
    fid = _seed_formula(db)
    db.close()

    payload = {
        "name": "UPDATED_F", "content": "MA(CLOSE,10);",
        "signals": [],
        "formula_count": 300,
    }
    resp = c.put(f"/api/formulas/{fid}", json=payload)
    assert resp.json()["code"] == 0
    assert resp.json()["data"]["formula_count"] == 300

    db = Session()
    assert db.get(Formula, fid).formula_count == 300
    db.close()


def test_formula_count_below_1_rejected(client):
    """formula_count < 1 → code 400。"""
    c, Session = client
    resp = c.post("/api/formulas", json={
        "name": "BAD", "content": "X", "signals": [], "formula_count": 0,
    })
    assert resp.json()["code"] == 400
    assert "formula_count" in resp.json()["message"]


# ---------------------------------------------------------------------------
# 通达信公式导入（三步流程）：GET tdx-list / GET tdx-info / POST import
# ---------------------------------------------------------------------------
class _FakeTQFormula:
    """mock TQFormula：列表/详情不连真实通达信。"""

    def get_formula_list(self, formula_type=0):
        return [
            {"acCode": "MA", "acName": "均线", "isSys": 1},
            {"acCode": "QZQ", "acName": "", "isSys": 0},
            {"acCode": "COSTLINE", "acName": "", "isSys": 0},
        ]

    def get_formula_info(self, formula_type=0, formula_code=""):
        if formula_code == "MISS":
            return {}
        return {
            "acCode": formula_code, "acName": "", "isSys": 0,
            "ParaNum": 0,
            "LineNum": 2,
            "Line": [{"LineName": "开仓"}, {"LineName": "平仓"}],
        }


@pytest.fixture
def fake_tq(monkeypatch):
    """把 service 层引用的 TQFormula 替换为 mock 类。"""
    from core.services import formula_service
    monkeypatch.setattr(formula_service, "TQFormula", _FakeTQFormula)


def test_tdx_list_user_only_marks_imported(client, fake_tq):
    """user_only=true 只回自编公式；与库内按 name 匹配出 imported/formula_id。"""
    c, Session = client
    db = Session()
    qzq_id = _seed_formula(db, name="QZQ")
    db.close()

    resp = c.get("/api/formulas/tdx-list", params={"user_only": "true"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    items = body["data"]
    # 系统公式 MA 被过滤，只剩 2 个自编
    assert [i["acCode"] for i in items] == ["QZQ", "COSTLINE"]
    by_code = {i["acCode"]: i for i in items}
    assert by_code["QZQ"]["imported"] is True
    assert by_code["QZQ"]["formula_id"] == qzq_id
    assert by_code["COSTLINE"]["imported"] is False
    assert by_code["COSTLINE"]["formula_id"] is None


def test_tdx_list_without_user_only_includes_system(client, fake_tq):
    """不带 user_only → 系统公式也返回。"""
    c, _ = client
    resp = c.get("/api/formulas/tdx-list")
    assert resp.json()["code"] == 0
    codes = [i["acCode"] for i in resp.json()["data"]]
    assert "MA" in codes and "QZQ" in codes


def test_tdx_info_returns_lines(client, fake_tq):
    """tdx-info 返回公式元数据（含 Line 线名，供信号预填）。"""
    c, _ = client
    resp = c.get("/api/formulas/tdx-info", params={"ac_code": "QZQ"})
    assert resp.json()["code"] == 0
    data = resp.json()["data"]
    assert data["acCode"] == "QZQ"
    assert data["LineNum"] == 2
    assert [l["LineName"] for l in data["Line"]] == ["开仓", "平仓"]


def test_tdx_info_not_found(client, fake_tq):
    """通达信查无此公式 → code 404。"""
    c, _ = client
    resp = c.get("/api/formulas/tdx-info", params={"ac_code": "MISS"})
    assert resp.json()["code"] == 404


def test_import_creates_formula_without_signals(client, fake_tq):
    """导入 COSTLINE → 建 Formula(name=COSTLINE, content='', formula_count=200)，0 信号。"""
    c, Session = client
    resp = c.post("/api/formulas/import", json={"ac_code": "COSTLINE"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    new_id = body["data"]["id"]
    assert body["data"]["name"] == "COSTLINE"
    assert body["data"]["content"] == ""
    assert body["data"]["formula_count"] == 200
    assert body["data"]["signals"] == []

    db = Session()
    f = db.get(Formula, new_id)
    assert f is not None
    assert f.name == "COSTLINE"
    db.close()


def test_import_existing_name_conflict(client, fake_tq):
    """导入已存在同名公式 → code 409。"""
    c, Session = client
    db = Session()
    _seed_formula(db, name="QZQ")
    db.close()

    resp = c.post("/api/formulas/import", json={"ac_code": "QZQ"})
    assert resp.json()["code"] == 409
    assert "已存在" in resp.json()["message"]


def test_import_unknown_ac_code(client, fake_tq):
    """通达信查无此公式 → code 404，不落库。"""
    c, Session = client
    resp = c.post("/api/formulas/import", json={"ac_code": "MISS"})
    assert resp.json()["code"] == 404

    db = Session()
    assert db.query(Formula).filter_by(name="MISS").count() == 0
    db.close()


# ---------------------------------------------------------------------------
# DELETE /api/formulas/{id} — 删公式（信号随 CASCADE 删）
# ---------------------------------------------------------------------------
def test_delete_formula_removes_signals(client):
    """DELETE 公式 → 公式+其下信号都没了。"""
    c, Session = client
    db = Session()
    fid = _seed_formula(db)  # 4 信号
    db.close()

    resp = c.delete(f"/api/formulas/{fid}")

    assert resp.status_code == 200
    assert resp.json()["code"] == 0

    db = Session()
    assert db.get(Formula, fid) is None
    assert db.query(FormulaSignal).filter_by(formula_id=fid).count() == 0
    db.close()


def test_delete_formula_not_found(client):
    """DELETE 不存在 id → code 404。"""
    c, Session = client
    resp = c.delete("/api/formulas/9999")
    assert resp.status_code == 200
    assert resp.json()["code"] == 404


def test_delete_formula_referenced_by_strategy(client):
    """DELETE 被策略引用的公式 → code 409（而非 500）。
    Strategy.formula_id ondelete=RESTRICT，删除应被拒，提示需先解除引用。"""
    from decimal import Decimal
    c, Session = client
    db = Session()
    # 建依赖链：StockPool → PortfolioStrategy → Formula → Strategy(引用该公式)
    pool = StockPool(code="TQCS", name="tq自选")
    db.add(pool); db.flush()
    fid = _seed_formula(db)
    ps = PortfolioStrategy(
        name="ps", stock_pool_id=pool.id, initial_capital=Decimal("100000"),
        max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"),
    )
    db.add(ps); db.flush()
    db.add(Strategy(
        portfolio_id=ps.id, name="s1", formula_id=fid,
        period="1d", role="independent",
    ))
    db.commit()
    db.close()

    resp = c.delete(f"/api/formulas/{fid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 409
    assert "引用" in body["message"]

    # 公式仍在（未被删）
    db = Session()
    assert db.get(Formula, fid) is not None
    db.close()


# ---------------------------------------------------------------------------
# engine 字段（内置公式子集解释器，默认 tq 行为不变）
# ---------------------------------------------------------------------------
def test_create_formula_engine_default_tq(client):
    """POST 不带 engine → 默认 "tq"。"""
    c, Session = client
    resp = c.post("/api/formulas", json={"name": "E1", "content": "X", "signals": []})
    assert resp.json()["code"] == 0
    assert resp.json()["data"]["engine"] == "tq"

    db = Session()
    assert db.get(Formula, resp.json()["data"]["id"]).engine == "tq"
    db.close()


def test_create_formula_with_engine_builtin(client):
    """POST engine=builtin → 落库。"""
    c, Session = client
    resp = c.post("/api/formulas", json={
        "name": "E2", "content": "SIG:IF(C>O,1,0);", "signals": [],
        "engine": "builtin",
    })
    assert resp.json()["code"] == 0
    assert resp.json()["data"]["engine"] == "builtin"

    db = Session()
    assert db.get(Formula, resp.json()["data"]["id"]).engine == "builtin"
    db.close()


def test_create_formula_invalid_engine_rejected(client):
    """engine 不在 {tq, builtin} → code 400。"""
    c, Session = client
    resp = c.post("/api/formulas", json={
        "name": "E3", "content": "X", "signals": [], "engine": "foo",
    })
    assert resp.json()["code"] == 400
    assert "engine" in resp.json()["message"]


def test_update_formula_changes_engine(client):
    """PUT 改 engine → 落库更新。"""
    c, Session = client
    db = Session()
    fid = _seed_formula(db)
    db.close()

    resp = c.put(f"/api/formulas/{fid}", json={
        "name": "E4", "content": "X", "signals": [], "engine": "builtin",
    })
    assert resp.json()["code"] == 0
    assert resp.json()["data"]["engine"] == "builtin"

    db = Session()
    assert db.get(Formula, fid).engine == "builtin"
    db.close()


def test_list_formula_includes_engine(client):
    """GET 列表每条含 engine（seed 不带 → 默认 tq）。"""
    c, Session = client
    db = Session()
    fid = _seed_formula(db)
    db.close()

    resp = c.get("/api/formulas")
    item = next(x for x in resp.json()["data"] if x["id"] == fid)
    assert item["engine"] == "tq"


# ---------------------------------------------------------------------------
# POST /api/formulas/precheck — 内置引擎语法预检（无数据干跑）
# ---------------------------------------------------------------------------
def test_precheck_ok(client):
    """合法公式 → ok=true，errors 空，outputs 含输出线名。"""
    c, _ = client
    resp = c.post("/api/formulas/precheck", json={
        "content": "SIG:IF(C>O,1,0),NODRAW;",
    })
    assert resp.json()["code"] == 0
    data = resp.json()["data"]
    assert data["ok"] is True
    assert data["errors"] == []
    assert data["outputs"] == ["SIG"]
    assert isinstance(data["issues"], list)


def test_precheck_future_function_error(client):
    """ZIG → future error 级 issue，ok=false。"""
    c, _ = client
    resp = c.post("/api/formulas/precheck", json={"content": "X:ZIG(C,5);"})
    data = resp.json()["data"]
    assert data["ok"] is False
    assert any(
        i["rule"] == "future" and i["level"] == "error" and i["name"] == "ZIG"
        for i in data["issues"]
    )


def test_precheck_syntax_error(client):
    """括号未闭合 → errors 首条为 lint 精确行列提示，解析错误仍含定义名。"""
    c, _ = client
    resp = c.post("/api/formulas/precheck", json={"content": "X:(C+1;"})
    data = resp.json()["data"]
    assert data["ok"] is False
    assert data["errors"]
    # lint 先报：精确到行列的人话提示
    assert "未闭合" in data["errors"][0]
    # 解析层错误仍在列表中（含定义名）
    assert any("X" in e for e in data["errors"])


def test_precheck_unimplemented_function(client):
    """内置引擎未实现函数 → 致命错误（词典层 unknown 仅 warning）。"""
    c, _ = client
    resp = c.post("/api/formulas/precheck", json={"content": "X:FOO(C,5);"})
    data = resp.json()["data"]
    assert data["ok"] is False
    assert any("FOO" in e for e in data["errors"])


# ---------------------------------------------------------------------------
# POST /api/formulas/normalize-preview — 0016 信号标准化向导预览（不落库）
# ---------------------------------------------------------------------------
def test_normalize_preview_cross_zero_pair(client):
    """合法预览 → `:` 线降级 `:=` + 尾部 XG/XD 双线，output_lines 含含义说明。"""
    c, _ = client
    resp = c.post("/api/formulas/normalize-preview", json={
        "content": "M:CLOSE;",
        "plans": [{"line": "M", "rule": "cross_zero", "params": {"direction": "both"}}],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["new_content"] == "M:=CLOSE;\nXG:CROSS(M,0);\nXD:CROSS(0,M);"
    assert [o["name"] for o in data["output_lines"]] == ["XG", "XD"]
    assert data["output_lines"][0]["source_line"] == "M"
    assert isinstance(data["warnings"], list)


def test_normalize_preview_bad_line_returns_400(client):
    """line 不是 `:` 输出线 → code 400 带原因。"""
    c, _ = client
    resp = c.post("/api/formulas/normalize-preview", json={
        "content": "M:CLOSE;",
        "plans": [{"line": "NOPE", "rule": "cross_zero", "params": {}}],
    })
    assert resp.json()["code"] == 400
    assert "NOPE" in resp.json()["message"]


def test_normalize_preview_empty_plans_returns_400(client):
    c, _ = client
    resp = c.post("/api/formulas/normalize-preview", json={"content": "M:CLOSE;", "plans": []})
    assert resp.json()["code"] == 400


def test_normalize_preview_then_create_signal_ready(client):
    """端到端：预览生成 → 保存为新公式（带预填信号）→ 源公式内容零变化。"""
    c, Session = client
    # 源公式落库
    r = c.post("/api/formulas", json={"name": "SRC", "content": "M:CLOSE;", "signals": []})
    assert r.json()["code"] == 0
    src_id = r.json()["data"]["id"]

    preview = c.post("/api/formulas/normalize-preview", json={
        "content": "M:CLOSE;",
        "plans": [{"line": "M", "rule": "cross_zero", "params": {"direction": "both"}}],
    }).json()["data"]

    r2 = c.post("/api/formulas", json={
        "name": "SRC-STD",
        "content": preview["new_content"],
        "signals": [{"signal_name": "XG", "signal_type": "OPEN", "trigger_value": 1}],
    })
    assert r2.json()["code"] == 0
    new_id = r2.json()["data"]["id"]
    assert new_id != src_id

    items = c.get("/api/formulas").json()["data"]
    src = next(x for x in items if x["id"] == src_id)
    std = next(x for x in items if x["id"] == new_id)
    # 源公式内容零变化（A3）
    assert src["content"] == "M:CLOSE;"
    # 新公式内容为标准化源码，信号可配置
    assert std["content"] == "M:=CLOSE;\nXG:CROSS(M,0);\nXD:CROSS(0,M);"
    assert std["signals"][0]["signal_name"] == "XG"
