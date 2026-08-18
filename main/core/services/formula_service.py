"""公式 Service 层（P1 #9 续块）。

承接 core.api.formulas 的业务逻辑：公式序列化（附 signals 子列表）、
CRUD（含信号全量替换）、删除（含 RESTRICT 引用拦截）。

校验（_validate_signals + VALID_SIGNAL_TYPES/VALID_TRIGGER_VALUES）留路由（HTTP 400 语义）。
路由层仅剩 HTTP 入口 + 资源校验(404) + IntegrityError→409 翻译 + ok/err 包装。
"""
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.models import Formula, FormulaSignal


def serialize_formula(db: Session, f: Formula) -> dict:
    """Formula → dict，附 signals 子列表（显式二次查询，模型无 relationship）。"""
    sigs = (
        db.query(FormulaSignal)
        .filter(FormulaSignal.formula_id == f.id)
        .order_by(FormulaSignal.id)
        .all()
    )
    return {
        "id": f.id,
        "name": f.name,
        "content": f.content,
        "formula_count": f.formula_count,
        "created_at": f.created_at,
        "updated_at": f.updated_at,
        "signals": [
            {
                "id": s.id,
                "signal_name": s.signal_name,
                "signal_type": s.signal_type,
                "trigger_value": s.trigger_value,
            }
            for s in sigs
        ],
    }


def list_formulas(db: Session) -> list[dict]:
    formulas = db.query(Formula).order_by(Formula.id).all()
    return [serialize_formula(db, f) for f in formulas]


def get_formula(db: Session, formula_id: int) -> dict | None:
    """返回公式详情或 None（不存在）。"""
    f = db.query(Formula).filter(Formula.id == formula_id).first()
    if f is None:
        return None
    return serialize_formula(db, f)


def create_formula(db: Session, req) -> dict:
    """建公式 + 信号（事务）。req 含 name/content/formula_count/signals。"""
    f = Formula(name=req.name, content=req.content, formula_count=req.formula_count)
    db.add(f)
    db.flush()
    for sig in req.signals:
        db.add(FormulaSignal(
            formula_id=f.id, signal_name=sig.signal_name,
            signal_type=sig.signal_type, trigger_value=sig.trigger_value,
        ))
    db.commit()
    db.refresh(f)
    return serialize_formula(db, f)


def update_formula(db: Session, formula_id: int, req) -> dict | None:
    """更新公式 + 信号全量替换。None=不存在。"""
    f = db.query(Formula).filter(Formula.id == formula_id).first()
    if f is None:
        return None
    f.name = req.name
    f.content = req.content
    f.formula_count = req.formula_count
    # 信号全量替换：删旧建新（简单可靠）
    db.query(FormulaSignal).filter(FormulaSignal.formula_id == formula_id).delete()
    for sig in req.signals:
        db.add(FormulaSignal(
            formula_id=formula_id, signal_name=sig.signal_name,
            signal_type=sig.signal_type, trigger_value=sig.trigger_value,
        ))
    db.commit()
    db.refresh(f)
    return serialize_formula(db, f)


def delete_formula(db: Session, formula_id: int) -> bool:
    """删公式（FormulaSignal 随 ondelete=CASCADE 删）。False=不存在；
    被策略引用时 ondelete=RESTRICT 抛 IntegrityError（路由 catch→409）。"""
    f = db.query(Formula).filter(Formula.id == formula_id).first()
    if f is None:
        return False
    db.delete(f)
    db.commit()
    return True
