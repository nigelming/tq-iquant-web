"""公式 Service 层（P1 #9 续块）。

承接 core.api.formulas 的业务逻辑：公式序列化（附 signals 子列表）、
CRUD（含信号全量替换）、删除（含 RESTRICT 引用拦截）、通达信导入三步
（tdx_list 列表+已导入标记 / tdx_info 元数据 / import_from_tdx 落库）。

校验（_validate_signals + VALID_SIGNAL_TYPES/VALID_TRIGGER_VALUES）留路由（HTTP 400 语义）。
路由层仅剩 HTTP 入口 + 资源校验(404) + IntegrityError→409 翻译 + ok/err 包装。
"""
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.models import Formula, FormulaSignal
from core.tq.formula import TQFormula


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
        "engine": f.engine,
        "no_leak_cross": bool(f.no_leak_cross),
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
    f = Formula(name=req.name, content=req.content, formula_count=req.formula_count,
                engine=req.engine,
                no_leak_cross=bool(getattr(req, "no_leak_cross", False)))
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
    f.engine = req.engine
    f.no_leak_cross = bool(getattr(req, "no_leak_cross", False))
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


# ---------------------------------------------------------------------------
# 通达信公式导入（三步流程）：列表 → 选库 → 配信号
# ---------------------------------------------------------------------------
def tdx_list(db: Session, user_only: bool = False, formula_type: int = 0) -> list[dict]:
    """通达信公式列表（type=0 技术指标），按 acCode==name 标记库内是否已导入。

    user_only=True 只回自编公式（isSys != 1）。
    """
    raw = TQFormula().get_formula_list(formula_type=formula_type)
    if user_only:
        raw = [x for x in raw if x.get("isSys") != 1]
    by_name = {f.name: f.id for f in db.query(Formula).all()}
    return [
        {
            "acCode": x.get("acCode", ""),
            "acName": x.get("acName", ""),
            "isSys": x.get("isSys"),
            "imported": x.get("acCode", "") in by_name,
            "formula_id": by_name.get(x.get("acCode", "")),
        }
        for x in raw
    ]


def tdx_info(formula_type: int, ac_code: str) -> dict | None:
    """通达信单公式元数据（Para 参数表 + Line 输出线名）。查无 → None。"""
    info = TQFormula().get_formula_info(formula_type=formula_type, formula_code=ac_code)
    if not info or not info.get("acCode"):
        return None
    return info


def import_from_tdx(db: Session, ac_code: str, formula_type: int = 0) -> dict | None:
    """把通达信公式落库：Formula(name=acCode, content='', formula_count=200)，0 信号。

    返回 serialize 结果；通达信查无此公式 → None（路由→404）；
    重名冲突 → ValueError（路由→409）。
    """
    if tdx_info(formula_type, ac_code) is None:
        return None
    if db.query(Formula).filter(Formula.name == ac_code).first() is not None:
        raise ValueError(f"公式 {ac_code} 已存在")
    f = Formula(name=ac_code, content="", formula_count=200)
    db.add(f)
    db.commit()
    db.refresh(f)
    return serialize_formula(db, f)
