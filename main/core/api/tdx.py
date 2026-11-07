"""通达信工具 REST 接口（/api/tdx）：自选股板块 / 除权除息 / 交易日历。

薄路由层：TQ 1.3.1 包装（core/tq/data.py）的 REST 暴露。全部同步路由（def）——
TQ 调用是阻塞 DLL 调用，FastAPI 自动丢线程池。TDXConnectionError 统一
err(503, str(e))（消息已中文）；SDK 返回 dict 的 ErrorId 语义由本层判定
（无 ErrorId 视为成功，空 dict = wrapper 归一的 SDK 失败 → 503）。
"""
import json
import logging

import pandas as pd
from fastapi import APIRouter
from pydantic import BaseModel

from core.api.response import err, ok
from core.tq.data import TQData
from core.tq.utils import TDXConnectionError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tdx", tags=["tdx"])


class UserBlockReq(BaseModel):
    block_code: str
    block_name: str = ""
    stock_codes: list[str]
    show: bool = False
    mode: str = "replace"  # replace=覆盖写（默认，先清空板块再写入）| append=追加


def _sdk_err_msg(d: dict, default: str) -> str | None:
    """SDK dict 的 ErrorId 判定：空 dict（SDK 失败，wrapper 归一 None→{}）或
    ErrorId != "0" → 错误文案（Error/Msg 字段优先，兜底 default）；"0" → None。"""
    if not d:
        return default
    if str(d.get("ErrorId", "0")) != "0":
        return str(d.get("Error") or d.get("Msg") or default)
    return None


def _sector_already_exists(msg: str) -> bool:
    """ensure_sector 对「板块已存在」的重复创建报 ErrorId≠0——幂等豁免：
    错误文案含「已存在」或 "exist"（大小写不敏感）即视为板块已在，继续写成分股。"""
    return "已存在" in msg or "exist" in msg.lower()


@router.post("/user-block")
def create_user_block(req: UserBlockReq):
    """写自选股板块：ensure_sector（不存在则创建）→ send_user_block 写入成分股。
    幂等：ensure_sector 报「板块已存在」视为成功，继续写入（同名板块重推不 503）。

    mode=replace（默认）：SDK send_user_block 为追加语义，先 delete_sector 清空
    板块成分股再写入 = 覆盖；delete best-effort——结果忽略、异常 warning 不阻断。
    mode=append：保持 SDK 原生追加语义（ensure → send）。"""
    if not req.block_code.strip():
        return err(400, "block_code 不能为空")
    if not req.stock_codes:
        return err(400, "stock_codes 不能为空")
    if req.mode not in ("replace", "append"):
        return err(400, "mode 仅允许 replace/append")
    try:
        if req.mode == "replace":
            # 覆写的正确性完全依赖「先清空成功」：SDK delete 失败常态是返回
            # None/ErrorId≠0（不是抛异常），静默放过会让旧成分股残留并与新
            # 成分股追加合并，随后 sync_pool 会把残留票同步进策略股票池。
            # 板块本就不存在（删除报「不存在」）属正常，继续 ensure 创建。
            try:
                del_ret = TQData().delete_sector(req.block_code)
            except Exception as e:
                logger.warning("delete_sector(%s) 失败：%s", req.block_code, e)
                return err(503, "清空通达信板块失败：%s" % e)
            del_dict: dict = {}
            if isinstance(del_ret, dict):
                del_dict = del_ret
            elif isinstance(del_ret, str) and del_ret.strip():
                try:
                    parsed = json.loads(del_ret)
                    del_dict = parsed if isinstance(parsed, dict) else {}
                except ValueError:
                    del_dict = {}
            del_msg = _sdk_err_msg(del_dict, "清空通达信板块失败（SDK 无返回）")
            # _sdk_err_msg 成功时返回 None（ErrorId=0），不能直接 in/upper。
            absent = bool(del_msg) and (
                "不存在" in del_msg or "not exist" in del_msg.lower())
            if del_msg and not absent:
                return err(503, del_msg)
        ensured = TQData().ensure_sector(
            req.block_code, req.block_name or req.block_code)
        msg = _sdk_err_msg(ensured, "创建通达信板块失败")
        if msg and not _sector_already_exists(msg):
            return err(503, msg)
        sent = TQData().send_user_block(
            req.block_code, req.stock_codes, show=req.show)
        msg = _sdk_err_msg(sent, "写入通达信自选股失败")
        if msg:
            return err(503, msg)
    except TDXConnectionError as e:
        return err(503, str(e))
    return ok({"block_code": req.block_code, "count": len(req.stock_codes),
               "mode": req.mode})


@router.get("/divid-factors")
def get_divid_factors(stock_code: str = "", start: str = "", end: str = ""):
    """除权除息数据：Date 索引还原为普通列；NaN→null（Starlette 禁 NaN 字面量）。
    空 events 可能是无数据或通达信不可用（SDK 层不可分）。"""
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
    """交易日历（market 固定 SH——SDK 对 market 取值宽松，各值返回一致）。

    strict=True 取数：异常上抛 → 503（区别于真空）；SDK 打印型失败返回的
    空列表仍按成功透传（SDK 层无法区分）。"""
    if not start.strip() or not end.strip():
        return err(400, "start 与 end 均必填（YYYYMMDD）")
    try:
        dates = TQData().get_trading_dates(start=start, end=end, strict=True)
    except TDXConnectionError as e:
        return err(503, str(e))
    except Exception as e:
        return err(503, "交易日历获取失败：%s" % e)
    return ok({"dates": dates})
