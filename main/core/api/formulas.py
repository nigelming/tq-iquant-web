from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.api.response import err, ok
from core.db import get_db
from core.models import Formula, FormulaSignal

router = APIRouter(prefix="/api/formulas", tags=["formulas"])

VALID_SIGNAL_TYPES = {"OPEN", "ADD", "REDUCE", "CLOSE"}
VALID_TRIGGER_VALUES = {1, -1}


class SignalItem(BaseModel):
    signal_name: str
    signal_type: str  # OPEN|ADD|REDUCE|CLOSE
    trigger_value: int  # 1 或 -1


class FormulaCreate(BaseModel):
    name: str
    content: str
    signals: list[SignalItem]
    # Q4 决策4：注入历史根数（公式级字段，同公式 count 恒定 → C4 去重 key 无需 count）
    formula_count: int = 200


def _serialize_formula(db: Session, f: Formula) -> dict:
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


def _validate_signals(signals: list[SignalItem]) -> str | None:
    """返回错误消息（str）或 None（校验通过）。"""
    for sig in signals:
        if sig.signal_type not in VALID_SIGNAL_TYPES:
            return f"signal_type 必须为 OPEN/ADD/REDUCE/CLOSE，收到 {sig.signal_type}"
        if sig.trigger_value not in VALID_TRIGGER_VALUES:
            return f"trigger_value 必须为 1 或 -1，收到 {sig.trigger_value}"
    return None


@router.get("")
def list_formulas(db: Session = Depends(get_db)):
    formulas = db.query(Formula).order_by(Formula.id).all()
    return ok([_serialize_formula(db, f) for f in formulas])


@router.get("/{formula_id}")
def get_formula(formula_id: int, db: Session = Depends(get_db)):
    f = db.query(Formula).filter(Formula.id == formula_id).first()
    if not f:
        return err(404, "公式不存在")
    return ok(_serialize_formula(db, f))


@router.post("")
def create_formula(req: FormulaCreate, db: Session = Depends(get_db)):
    err_msg = _validate_signals(req.signals)
    if err_msg:
        return err(400, err_msg)
    if req.formula_count < 1:
        return err(400, "formula_count 必须 ≥ 1")
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
    return ok(_serialize_formula(db, f))


@router.put("/{formula_id}")
def update_formula(formula_id: int, req: FormulaCreate, db: Session = Depends(get_db)):
    f = db.query(Formula).filter(Formula.id == formula_id).first()
    if not f:
        return err(404, "公式不存在")
    err_msg = _validate_signals(req.signals)
    if err_msg:
        return err(400, err_msg)
    if req.formula_count < 1:
        return err(400, "formula_count 必须 ≥ 1")
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
    return ok(_serialize_formula(db, f))


@router.delete("/{formula_id}")
def delete_formula(formula_id: int, db: Session = Depends(get_db)):
    f = db.query(Formula).filter(Formula.id == formula_id).first()
    if not f:
        return err(404, "公式不存在")
    try:
        db.delete(f)  # FormulaSignal 随 ondelete=CASCADE 删
        db.commit()
    except IntegrityError:
        db.rollback()
        return err(409, "该公式被策略引用，无法删除")
    return ok()
