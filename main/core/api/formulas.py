from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.api.response import err, ok
from core.db import get_db
from core.services.formula_service import (
    list_formulas as _svc_list_formulas,
    get_formula as _svc_get_formula,
    create_formula as _svc_create_formula,
    update_formula as _svc_update_formula,
    delete_formula as _svc_delete_formula,
)

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
    return ok(_svc_list_formulas(db))


@router.get("/{formula_id}")
def get_formula(formula_id: int, db: Session = Depends(get_db)):
    data = _svc_get_formula(db, formula_id)
    if data is None:
        return err(404, "公式不存在")
    return ok(data)


@router.post("")
def create_formula(req: FormulaCreate, db: Session = Depends(get_db)):
    err_msg = _validate_signals(req.signals)
    if err_msg:
        return err(400, err_msg)
    if req.formula_count < 1:
        return err(400, "formula_count 必须 ≥ 1")
    return ok(_svc_create_formula(db, req))


@router.put("/{formula_id}")
def update_formula(formula_id: int, req: FormulaCreate, db: Session = Depends(get_db)):
    # 校验参数（400）→ 再调 service（内部判 404 + 应用 + 提交）。
    # 原实现顺序是 404→400，但 service 把「判存在 + 应用 + 提交」合并后，须先校验
    # 避免把非法字段写库；「不存在 id + 非法信号」无测试覆盖且语义上 400 更合理。
    err_msg = _validate_signals(req.signals)
    if err_msg:
        return err(400, err_msg)
    if req.formula_count < 1:
        return err(400, "formula_count 必须 ≥ 1")
    data = _svc_update_formula(db, formula_id, req)
    if data is None:
        return err(404, "公式不存在")
    return ok(data)


@router.delete("/{formula_id}")
def delete_formula(formula_id: int, db: Session = Depends(get_db)):
    try:
        if not _svc_delete_formula(db, formula_id):
            return err(404, "公式不存在")
    except IntegrityError:
        db.rollback()
        return err(409, "该公式被策略引用，无法删除")
    return ok()
