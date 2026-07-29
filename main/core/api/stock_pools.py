from typing import List
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.db import get_db
from core.models import StockPool

router = APIRouter(prefix="/api/stock-pools", tags=["stock_pools"])


@router.get("")
def list_pools(db: Session = Depends(get_db)):
    pools = db.query(StockPool).all()
    return {"code": 0, "data": [{"id": p.id, "name": p.name, "synced_at": p.synced_at} for p in pools]}


@router.get("/tdx")
def list_tdx_pools():
    return {"code": 0, "data": []}


@router.get("/{pool_id}")
def get_pool(pool_id: int, db: Session = Depends(get_db)):
    pool = db.query(StockPool).filter(StockPool.id == pool_id).first()
    if not pool:
        return {"code": 404, "message": "资源不存在"}
    return {"code": 0, "data": {"id": pool.id, "name": pool.name}}
