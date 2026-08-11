from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import logging

from core.db import init_db
from core.api.stock_pools import router as stock_pools_router
from core.api.formulas import router as formulas_router
from core.api.strategies import router as strategies_router
from core.api.backtest import router as backtest_router
from core.api.live import router as live_router
from core.api.system import router as system_router
from core.api.status import router as status_router
from core.logging_config import setup_logging


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    init_db()
    yield


app = FastAPI(title="创懿量化交易平台", version="1.0.0", lifespan=lifespan)

app.include_router(stock_pools_router)
app.include_router(formulas_router)
app.include_router(strategies_router)
app.include_router(backtest_router)
app.include_router(live_router)
app.include_router(system_router)
app.include_router(status_router)


@app.exception_handler(Exception)
async def uncaught_exception_handler(request: Request, exc: Exception):
    """#13：未捕获异常 → 统一 envelope + HTTP 500（非 FastAPI 默认空 body）。

    仅注册 Exception，不注册 HTTPException → HTTPException 走 FastAPI 默认 handler
    （pass-through：真实 HTTP 状态码 + {"detail":...}），SSE 404 / backtest 409 行为不变。
    不暴露 str(exc) 给前端（内部细节不外泄），仅记日志。
    """
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"code": 500, "message": "服务器内部错误", "data": None},
    )


@app.get("/health")
async def health():
    return {"ok": True}
