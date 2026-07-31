from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

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
    return {"code": 0, "data": [_serialize_formula(db, f) for f in formulas]}


@router.get("/{formula_id}")
def get_formula(formula_id: int, db: Session = Depends(get_db)):
    f = db.query(Formula).filter(Formula.id == formula_id).first()
    if not f:
        return {"code": 404, "message": "公式不存在"}
    return {"code": 0, "data": _serialize_formula(db, f)}


@router.post("")
def create_formula(req: FormulaCreate, db: Session = Depends(get_db)):
    err = _validate_signals(req.signals)
    if err:
        return {"code": 400, "message": err}
    f = Formula(name=req.name, content=req.content)
    db.add(f)
    db.flush()
    for sig in req.signals:
        db.add(FormulaSignal(
            formula_id=f.id, signal_name=sig.signal_name,
            signal_type=sig.signal_type, trigger_value=sig.trigger_value,
        ))
    db.commit()
    db.refresh(f)
    return {"code": 0, "data": _serialize_formula(db, f)}


@router.put("/{formula_id}")
def update_formula(formula_id: int, req: FormulaCreate, db: Session = Depends(get_db)):
    f = db.query(Formula).filter(Formula.id == formula_id).first()
    if not f:
        return {"code": 404, "message": "公式不存在"}
    err = _validate_signals(req.signals)
    if err:
        return {"code": 400, "message": err}
    f.name = req.name
    f.content = req.content
    # 信号全量替换：删旧建新（简单可靠）
    db.query(FormulaSignal).filter(FormulaSignal.formula_id == formula_id).delete()
    for sig in req.signals:
        db.add(FormulaSignal(
            formula_id=formula_id, signal_name=sig.signal_name,
            signal_type=sig.signal_type, trigger_value=sig.trigger_value,
        ))
    db.commit()
    db.refresh(f)
    return {"code": 0, "data": _serialize_formula(db, f)}


@router.delete("/{formula_id}")
def delete_formula(formula_id: int, db: Session = Depends(get_db)):
    f = db.query(Formula).filter(Formula.id == formula_id).first()
    if not f:
        return {"code": 404, "message": "公式不存在"}
    db.delete(f)  # FormulaSignal 随 ondelete=CASCADE 删
    db.commit()
    return {"code": 0, "data": None}
