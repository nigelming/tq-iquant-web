"""通达信工具 REST 接口（/api/tdx）：自选股板块 / 除权除息 / 交易日历。

薄路由层：TQ 1.3.1 包装（core/tq/data.py）的 REST 暴露。全部同步路由（def）——
TQ 调用是阻塞 DLL 调用，FastAPI 自动丢线程池。TDXConnectionError 统一
err(503, str(e))（消息已中文）；SDK 返回 dict 的 ErrorId 语义由本层判定
（无 ErrorId 视为成功，空 dict = wrapper 归一的 SDK 失败 → 503）。
"""
import pandas as pd
from fastapi import APIRouter
from pydantic import BaseModel

from core.api.response import err, ok
from core.tq.data import TQData
from core.tq.utils import TDXConnectionError

router = APIRouter(prefix="/api/tdx", tags=["tdx"])


class UserBlockReq(BaseModel):
    block_code: str
    block_name: str = ""
    stock_codes: list[str]
    show: bool = False


def _sdk_err_msg(d: dict, default: str) -> str | None:
    """SDK dict 的 ErrorId 判定：空 dict（SDK 失败，wrapper 归一 None→{}）或
    ErrorId != "0" → 错误文案（Error/Msg 字段优先，兜底 default）；"0" → None。"""
    if not d:
        return default
    if str(d.get("ErrorId", "0")) != "0":
        return str(d.get("Error") or d.get("Msg") or default)
    return None


@router.post("/user-block")
def create_user_block(req: UserBlockReq):
    """写自选股板块：ensure_sector（不存在则创建）→ send_user_block 写入成分股。"""
    if not req.block_code.strip():
        return err(400, "block_code 不能为空")
    if not req.stock_codes:
        return err(400, "stock_codes 不能为空")
    try:
        ensured = TQData().ensure_sector(
            req.block_code, req.block_name or req.block_code)
        msg = _sdk_err_msg(ensured, "创建通达信板块失败")
        if msg:
            return err(503, msg)
        sent = TQData().send_user_block(
            req.block_code, req.stock_codes, show=req.show)
        msg = _sdk_err_msg(sent, "写入通达信自选股失败")
        if msg:
            return err(503, msg)
    except TDXConnectionError as e:
        return err(503, str(e))
    return ok({"block_code": req.block_code, "count": len(req.stock_codes)})


@router.get("/divid-factors")
def get_divid_factors(stock_code: str = "", start: str = "", end: str = ""):
    """除权除息数据：Date 索引还原为普通列；NaN→null（Starlette 禁 NaN 字面量）。"""
    if not stock_code.strip():
        return err(400, "stock_code 不能为空")
    try:
        df = TQData().get_divid_factors(stock_code, start=start, end=end)
    except TDXConnectionError as e:
        return err(503, str(e))
    events = [
        {k: (None if pd.isna(v) else v) for k, v in row.items()}
        for row in df.reset_index().to_dict(orient="records")
    ]
    return ok({"events": events})


@router.get("/trading-dates")
def get_trading_dates(start: str = "", end: str = ""):
    """交易日历（market 固定 SH——SDK 对 market 取值宽松，各值返回一致）。"""
    if not start.strip() or not end.strip():
        return err(400, "start 与 end 均必填（YYYYMMDD）")
    try:
        dates = TQData().get_trading_dates(start=start, end=end)
    except TDXConnectionError as e:
        return err(503, str(e))
    return ok({"dates": dates})
