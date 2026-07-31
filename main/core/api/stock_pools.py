from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from core.db import get_db
from core.models import StockPool, StockPoolStock
from core.tq.data import TQData
from core.tq.utils import TDXConnectionError

router = APIRouter(prefix="/api/stock-pools", tags=["stock_pools"])


class SyncReq(BaseModel):
    code: str


def _serialize_pool(db: Session, p: StockPool) -> dict:
    """本地 StockPool → dict，含 stock_count（显式二次查询，模型无 relationship）。"""
    count = db.query(StockPoolStock).filter(StockPoolStock.pool_id == p.id).count()
    return {
        "id": p.id,
        "code": p.code,
        "name": p.name,
        "synced_at": p.synced_at,
        "stock_count": count,
    }


# ---------------------------------------------------------------------------
# GET /api/stock-pools — 本地已同步池（供组合策略引用）
# ---------------------------------------------------------------------------
@router.get("")
def list_local_pools(db: Session = Depends(get_db)):
    pools = db.query(StockPool).order_by(StockPool.id).all()
    return {"code": 0, "data": [_serialize_pool(db, p) for p in pools]}


# ---------------------------------------------------------------------------
# GET /api/stock-pools/tdx — 通达信用户板块 + 本地残留合并 + synced 标记
# ---------------------------------------------------------------------------
@router.get("/tdx")
def list_tdx_pools(db: Session = Depends(get_db)):
    """直读通达信用户板块（get_user_sector），合并本地残留（通达信已删但本地还在的）。

    返回 [{code, name, synced, exists_in_tdx, stock_count}]：
    - synced: 本地是否已同步
    - exists_in_tdx: 通达信是否还有此板块（False=本地残留）
    - stock_count: 本地成分股数（未同步为 0）
    """
    try:
        tdx_pools = TQData().get_stock_pools()
    except TDXConnectionError:
        return {"code": 500, "message": "通达信未启动或连接失败"}

    local_pools = db.query(StockPool).all()
    local_by_code = {p.code: p for p in local_pools}
    tdx_codes = {t["code"] for t in tdx_pools}

    result = []
    # 通达信板块
    for t in tdx_pools:
        p = local_by_code.get(t["code"])
        count = (
            db.query(StockPoolStock).filter(StockPoolStock.pool_id == p.id).count()
            if p else 0
        )
        result.append({
            "code": t["code"],
            "name": t["name"],
            "synced": p is not None,
            "exists_in_tdx": True,
            "stock_count": count,
        })
    # 本地残留（通达信已删）
    for p in local_pools:
        if p.code not in tdx_codes:
            count = db.query(StockPoolStock).filter(StockPoolStock.pool_id == p.id).count()
            result.append({
                "code": p.code,
                "name": p.name,
                "synced": True,
                "exists_in_tdx": False,
                "stock_count": count,
            })
    return {"code": 0, "data": result}


# ---------------------------------------------------------------------------
# GET /api/stock-pools/tdx/{code}/stocks — 通达信成分股实时
# ---------------------------------------------------------------------------
@router.get("/tdx/{code}/stocks")
def list_tdx_stocks(code: str, db: Session = Depends(get_db)):
    """实时成分股。板块在通达信不存在 → 404；通达信不可达 → 500。"""
    try:
        tdx = TQData()
        sectors = tdx.get_stock_pools()
        if not any(s["code"] == code for s in sectors):
            return {"code": 404, "message": "板块不存在"}
        stocks = tdx.get_pool_stocks(code)
    except TDXConnectionError:
        return {"code": 500, "message": "通达信未启动或连接失败"}
    return {"code": 0, "data": stocks}


# ---------------------------------------------------------------------------
# POST /api/stock-pools/sync — upsert 本地池 + 全量替换成分股
# ---------------------------------------------------------------------------
@router.post("/sync")
def sync_pool(req: SyncReq, db: Session = Depends(get_db)):
    """按 code upsert：查通达信拿 name + 成分股，本地有则更新无则新建，全量替换成分股。"""
    try:
        tdx = TQData()
        sectors = tdx.get_stock_pools()
        sector = next((s for s in sectors if s["code"] == req.code), None)
        if sector is None:
            return {"code": 404, "message": "板块不存在"}
        tdx_stocks = tdx.get_pool_stocks(req.code)
    except TDXConnectionError:
        return {"code": 500, "message": "通达信未启动或连接失败"}

    p = db.query(StockPool).filter(StockPool.code == req.code).first()
    if p is None:
        p = StockPool(code=req.code, name=sector["name"])
        db.add(p)
        db.flush()
    else:
        p.name = sector["name"]

    # 全量替换成分股
    db.query(StockPoolStock).filter(StockPoolStock.pool_id == p.id).delete()
    for s in tdx_stocks:
        code = s.get("stock_code")
        if code:
            db.add(StockPoolStock(
                pool_id=p.id,
                stock_code=code,
                stock_name=s.get("stock_name"),
            ))
    p.synced_at = func.now()
    db.commit()
    db.refresh(p)
    return {"code": 0, "data": _serialize_pool(db, p)}


# ---------------------------------------------------------------------------
# DELETE /api/stock-pools/{id} — 删本地池（CASCADE 删成分股，RESTRICT 阻止被引用）
# ---------------------------------------------------------------------------
@router.delete("/{pool_id}")
def delete_pool(pool_id: int, db: Session = Depends(get_db)):
    p = db.query(StockPool).filter(StockPool.id == pool_id).first()
    if not p:
        return {"code": 404, "message": "股票池不存在"}
    try:
        db.delete(p)  # StockPoolStock 随 ondelete=CASCADE 删；被策略引用时 ondelete=RESTRICT 抛 IntegrityError
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"code": 409, "message": "该股票池被组合策略引用，无法删除"}
    return {"code": 0, "data": None}
