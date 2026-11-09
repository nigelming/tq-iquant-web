from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.api.response import err, ok
from core.db import get_db
from core.models import Formula
from core.engine.live.timing import _to_int
from core.engine.tdx_formula import syntax_check
from core.engine.tdx_formula.normalizer import normalize_formula
from core.services import formula_service
from core.tq.formula import TQFormula
from core.tq.utils import TDXConnectionError
from core.services.formula_service import (
    list_formulas as _svc_list_formulas,
    get_formula as _svc_get_formula,
    create_formula as _svc_create_formula,
    update_formula as _svc_update_formula,
    delete_formula as _svc_delete_formula,
)

router = APIRouter(prefix="/api/formulas", tags=["formulas"])

VALID_SIGNAL_TYPES = {"OPEN", "ADD", "REDUCE", "CLOSE"}
# 0 故意排除：0 是通达信信号输出线的"无信号"常态值，配成触发值会导致每根 bar 都匹配。
VALID_TRIGGER_VALUES = {-2, -1, 1, 2}
# 计算引擎：tq=按名调通达信（默认，历史行为）/ builtin=内置公式子集解释器（仅回测）
VALID_ENGINES = {"tq", "builtin"}


class SignalItem(BaseModel):
    signal_name: str
    signal_type: str  # OPEN|ADD|REDUCE|CLOSE
    trigger_value: int  # ±1/±2（0 为无信号常态值，禁止配置）


class FormulaCreate(BaseModel):
    name: str
    content: str
    signals: list[SignalItem]
    # Q4 决策4：注入历史根数（公式级字段，同公式 count 恒定 → C4 去重 key 无需 count）
    formula_count: int = 200
    # 计算引擎（默认 tq 保持原有行为；builtin 仅回测接线）
    engine: str = "tq"
    # 批次 9a：跨周期引用无泄露开关（默认 false=TDX 语义，仅 builtin 回测链路生效）
    no_leak_cross: bool = False


class FormulaImport(BaseModel):
    ac_code: str  # 通达信公式名（acCode）


class FormulaPrecheck(BaseModel):
    content: str  # 公式源码（无需落库）


def _validate_signals(signals: list[SignalItem]) -> str | None:
    """返回错误消息（str）或 None（校验通过）。"""
    for sig in signals:
        if not sig.signal_name.strip():
            return "signal_name 不能为空"
        if sig.signal_type not in VALID_SIGNAL_TYPES:
            return f"signal_type 必须为 OPEN/ADD/REDUCE/CLOSE，收到 {sig.signal_type}"
        if sig.trigger_value == 0:
            return (
                "trigger_value 不能为 0：0 是公式输出线的无信号常态值，"
                "配置后会在每根 bar 都匹配，请改用实际信号值（±1/±2）"
            )
        if sig.trigger_value not in VALID_TRIGGER_VALUES:
            return f"trigger_value 必须为 -2,-1,1,2，收到 {sig.trigger_value}"
    return None


@router.get("")
def list_formulas(db: Session = Depends(get_db)):
    return ok(_svc_list_formulas(db))


# ---------------------------------------------------------------------------
# 通达信公式导入三步（静态路由必须在 /{formula_id} 之前声明）
# ---------------------------------------------------------------------------
@router.get("/tdx-list")
def tdx_list(user_only: bool = False, formula_type: int = 0, db: Session = Depends(get_db)):
    """第一步：通达信公式列表，附 imported/formula_id 已导入标记。"""
    return ok(formula_service.tdx_list(db, user_only=user_only, formula_type=formula_type))


@router.get("/tdx-info")
def tdx_info(formula_type: int = 0, ac_code: str = ""):
    """单公式元数据（Para 参数表 + Line 输出线名，第三步信号预填用）。"""
    data = formula_service.tdx_info(formula_type, ac_code)
    if data is None:
        return err(404, "通达信公式不存在或接口未返回")
    return ok(data)


@router.post("/import")
def import_from_tdx(req: FormulaImport, db: Session = Depends(get_db)):
    """第二步：按 acCode 落库（content 留空，formula_count 默认 200，0 信号）。"""
    if not req.ac_code.strip():
        return err(400, "ac_code 不能为空")
    try:
        data = formula_service.import_from_tdx(db, req.ac_code.strip())
    except ValueError as e:
        return err(409, str(e))
    if data is None:
        return err(404, "通达信公式不存在或接口未返回")
    return ok(data)


@router.get("/{formula_id}")
def get_formula(formula_id: int, db: Session = Depends(get_db)):
    data = _svc_get_formula(db, formula_id)
    if data is None:
        return err(404, "公式不存在")
    return ok(data)


def _validate_engine(engine: str) -> str | None:
    """返回错误消息（str）或 None（校验通过）。"""
    if engine not in VALID_ENGINES:
        return f"engine 必须为 tq 或 builtin，收到 {engine}"
    return None


@router.post("")
def create_formula(req: FormulaCreate, db: Session = Depends(get_db)):
    if not req.name.strip():
        return err(400, "公式名称不能为空")
    err_msg = _validate_signals(req.signals)
    if err_msg:
        return err(400, err_msg)
    if req.formula_count < 1:
        return err(400, "formula_count 必须 ≥ 1")
    err_msg = _validate_engine(req.engine)
    if err_msg:
        return err(400, err_msg)
    return ok(_svc_create_formula(db, req))


@router.put("/{formula_id}")
def update_formula(formula_id: int, req: FormulaCreate, db: Session = Depends(get_db)):
    # 校验参数（400）→ 再调 service（内部判 404 + 应用 + 提交）。
    # 原实现顺序是 404→400，但 service 把「判存在 + 应用 + 提交」合并后，须先校验
    # 避免把非法字段写库；「不存在 id + 非法信号」无测试覆盖且语义上 400 更合理。
    if not req.name.strip():
        return err(400, "公式名称不能为空")
    err_msg = _validate_signals(req.signals)
    if err_msg:
        return err(400, err_msg)
    if req.formula_count < 1:
        return err(400, "formula_count 必须 ≥ 1")
    err_msg = _validate_engine(req.engine)
    if err_msg:
        return err(400, err_msg)
    data = _svc_update_formula(db, formula_id, req)
    if data is None:
        return err(404, "公式不存在")
    return ok(data)


@router.post("/precheck")
def precheck_formula(req: FormulaPrecheck):
    """内置引擎语法预检（无数据干跑，不落库）：词典闸门 + 语句/表达式解析。"""
    return ok(syntax_check(req.content))


class NormalizePlanItem(BaseModel):
    line: str  # 源公式的 `:` 输出线名
    rule: str  # cross_zero | above_threshold | map_values
    params: dict = {}


class NormalizePreviewRequest(BaseModel):
    content: str  # 公式源码（接受草稿，无需先落库）
    plans: list[NormalizePlanItem]


@router.post("/normalize-preview")
def normalize_preview(req: NormalizePreviewRequest):
    """0016 信号标准化向导预览（不落库）：`:` 输出线降级 `:=` + 尾部追加
    离散标识值（±1/±2/0）的标准化输出线。保存走 POST /api/formulas（新公式
    记录，源公式零改动）。"""
    try:
        return ok(normalize_formula(
            req.content,
            [{"line": p.line, "rule": p.rule, "params": p.params} for p in req.plans],
        ))
    except ValueError as e:
        return err(400, str(e))


# ---------------------------------------------------------------------------
# 公式源码试算（compute_by_src，不落库）
# ---------------------------------------------------------------------------
VALID_TEST_RUN_PERIODS = {"1m", "5m", "15m", "30m", "1h", "1d", "1w", "1mon"}


class TestRunRequest(BaseModel):
    content: str = ""  # 公式源码（不落库试算）；空 = 按 formula_id 走公式名计算（导入公式）
    formula_id: int | None = None  # content 为空时的必填项（平台已存公式）
    stock_codes: list[str]  # 1~20 只
    period: str = "1d"
    count: int = 20  # 注入历史根数
    dividend_type: int = 1  # 0=不复权 1=前复权 2=后复权
    formula_type: int = 0  # 0=指标 1=选股


def _summarize_line(values: list) -> dict:
    """输出线摘要：n=根数 / ones=1 信号个数（与实盘引擎信号判定同构：'1'、
    1.0、'1.00' 均计 1；'0.00'/'0'/None 不计——真机实证 SDK 返回 '1.00' 形态
    字符串，str 比对会系统性漏计）/ last_values=末 3 个值的字符串形态
    （None 保留为 "None"；元素兼容 {Date,Value} 字典与裸值两种形态）。"""
    vals = [
        item.get("Value") if isinstance(item, dict) else item
        for item in values
    ]
    return {
        "n": len(vals),
        "ones": sum(1 for v in vals if _to_int(v) == 1),
        "last_values": [str(v) for v in vals[-3:]],
    }


@router.post("/test-run")
def test_run_formula(req: TestRunRequest, db: Session = Depends(get_db)):
    """公式试算：逐股逐线返回摘要（n/ones/last_values）而非全量序列。

    双路径：content 非空 → formula_by_src 源码试算（不落库，编辑器即时验证）；
    content 空 + formula_id → **按公式名 compute**（与回测/实盘同一条路径——
    通达信导入公式平台库不存源码，只能按名计算）。部分股票无返回附 warning。
    """
    if not req.content.strip() and req.formula_id is None:
        return err(400, "content 与 formula_id 至少提供一个")
    if req.content.strip() and req.formula_id is not None:
        return err(400, "content 与 formula_id 只能二选一")
    if not 1 <= len(req.stock_codes) <= 20:
        return err(400, "试算股票数须 1-20")
    if req.period not in VALID_TEST_RUN_PERIODS:
        return err(400, f"period 必须为 {'、'.join(sorted(VALID_TEST_RUN_PERIODS))} 之一，收到 {req.period}")
    if not 1 <= req.count <= 500:
        return err(400, "count 必须为 1-500")
    try:
        if req.content.strip():
            res = TQFormula().compute_by_src(
                req.content, req.stock_codes, period=req.period, count=req.count,
                dividend_type=req.dividend_type, formula_type=req.formula_type,
            )
        else:
            formula = db.query(Formula).filter(Formula.id == req.formula_id).first()
            if formula is None:
                return err(404, "公式不存在")
            if formula.engine != "tq":
                return err(400, "builtin 公式不支持试算（内置解释器仅回测）——请用回测验证")
            # 按公式名计算：与回测/实盘同一条 compute 路径（导入公式平台库
            # 不存源码，源码在通达信客户端）
            res = TQFormula().compute(
                formula.name, "", req.stock_codes, period=req.period,
                count=req.count, dividend_type=req.dividend_type,
            )
    except TDXConnectionError as e:
        return err(503, str(e))
    if str(res.get("ErrorId", "1")) != "0":
        return err(503, str(res.get("Error") or "公式试算失败"))
    results = {
        code: {line: _summarize_line(vals) for line, vals in lines.items()}
        for code, lines in res.items()
        if code not in ("ErrorId", "Error") and isinstance(lines, dict)
    }
    data: dict = {"results": results}
    # Task1 评审：SDK 在 ErrorId="0" 但 "Error" 非空时表示部分股票失败；
    # compute_by_src 已剥 Error 键，返回股票数 < 请求数是可靠的兜底判据。
    if len(results) < len(req.stock_codes) or res.get("Error"):
        data["warning"] = f"部分股票无返回（{len(results)}/{len(req.stock_codes)}）"
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
