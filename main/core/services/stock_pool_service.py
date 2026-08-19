"""股票池 Service 层（P1 #9 续块）。

承接 core.api.stock_pools 的业务逻辑：本地池序列化、通达信板块合并查询、
成分股实时查询、按 code upsert + 全量替换成分股、删除（含 RESTRICT 引用拦截）。

异常约定（路由层翻译为 HTTP 状态码）：
- TDXConnectionError：通达信不可达 → 路由 err(500)
- LookupError：板块在通达信不存在 → 路由 err(404)
- IntegrityError：池被组合策略引用（ondelete=RESTRICT）→ 路由 err(409)

路由层仅剩 HTTP 入口 + 资源校验(404) + 异常翻译 + ok/err 包装。
"""
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from core.models import StockPool, StockPoolStock
from core.tq.data import TQData
from core.tq.utils import TDXConnectionError


def serialize_pool(db: Session, p: StockPool) -> dict:
    """本地 StockPool → dict，含 stock_count（显式二次查询，模型无 relationship）。"""
    count = db.query(StockPoolStock).filter(StockPoolStock.pool_id == p.id).count()
    return {
        "id": p.id,
        "code": p.code,
        "name": p.name,
        "synced_at": p.synced_at,
        "stock_count": count,
    }


def list_local_pools(db: Session) -> list[dict]:
    pools = db.query(StockPool).order_by(StockPool.id).all()
    return [serialize_pool(db, p) for p in pools]


def list_tdx_pools(db: Session) -> list[dict]:
    """直读通达信用户板块（get_user_sector），合并本地残留（通达信已删但本地还在的）。

    返回 [{code, name, synced, exists_in_tdx, stock_count}]：
    - synced: 本地是否已同步
    - exists_in_tdx: 通达信是否还有此板块（False=本地残留）
    - stock_count: 本地成分股数（未同步为 0）

    抛 TDXConnectionError：通达信不可达。
    """
    tdx_pools = TQData().get_stock_pools()  # 抛 TDXConnectionError

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
    return result


def list_tdx_stocks(code: str) -> list[dict]:
    """实时成分股。

    抛 LookupError：板块在通达信不存在。
    抛 TDXConnectionError：通达信不可达。
    """
    tdx = TQData()
    sectors = tdx.get_stock_pools()
    if not any(s["code"] == code for s in sectors):
        raise LookupError("板块不存在")
    return tdx.get_pool_stocks(code)


def sync_pool(db: Session, code: str) -> dict:
    """按 code upsert：查通达信拿 name + 成分股，本地有则更新无则新建，全量替换成分股。

    抛 LookupError：板块在通达信不存在。
    抛 TDXConnectionError：通达信不可达。
    """
    tdx = TQData()
    sectors = tdx.get_stock_pools()
    sector = next((s for s in sectors if s["code"] == code), None)
    if sector is None:
        raise LookupError("板块不存在")
    tdx_stocks = tdx.get_pool_stocks(code)

    p = db.query(StockPool).filter(StockPool.code == code).first()
    if p is None:
        p = StockPool(code=code, name=sector["name"])
        db.add(p)
        db.flush()
    else:
        p.name = sector["name"]

    # 全量替换成分股
    db.query(StockPoolStock).filter(StockPoolStock.pool_id == p.id).delete()
    for s in tdx_stocks:
        stock_code = s.get("stock_code")
        if stock_code:
            db.add(StockPoolStock(
                pool_id=p.id,
                stock_code=stock_code,
                stock_name=s.get("stock_name"),
            ))
    p.synced_at = func.now()
    db.commit()
    db.refresh(p)
    return serialize_pool(db, p)


def delete_pool(db: Session, pool_id: int) -> bool:
    """删本地池（StockPoolStock 随 ondelete=CASCADE 删）。

    False=不存在；被组合策略引用时 ondelete=RESTRICT 抛 IntegrityError（路由 catch→409）。
    """
    p = db.query(StockPool).filter(StockPool.id == pool_id).first()
    if p is None:
        return False
    db.delete(p)
    db.commit()
    return True
