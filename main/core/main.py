from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import logging

from core.db import init_db, SessionLocal
from core.api.stock_pools import router as stock_pools_router
from core.api.formulas import router as formulas_router
from core.api.strategies import router as strategies_router
from core.api.backtest import router as backtest_router
from core.api.live import router as live_router
from core.api.system import router as system_router
from core.api.status import router as status_router
from core.logging_config import setup_logging
from core.services.backtest_service import recover_stale_running_backtests


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    init_db()
    # #5: 清理上次进程崩溃/重启残留的僵尸 running 回测记录（同步内联执行，
    # 正常退出不残留 running；残留即崩溃）。统一标 failed + 附说明，避免前端
    # 永久显示"运行中"且无法重新发起（_BACKTEST_LOCK 是进程内锁，重启已释放）。
    with SessionLocal() as db:
        n = recover_stale_running_backtests(db)
        if n:
            logger.warning("启动恢复：%d 条僵尸 running 回测已标记 failed", n)
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


@app.get("/debug-path")
async def debug_path():
    import sys
    from pathlib import Path
    base = Path(sys.argv[0]).resolve().parent
    dist = base / "_internal" / "web" / "dist"
    if not dist.exists():
        dist = Path(__file__).resolve().parent.parent.parent / "web" / "dist"
    return {
        "frozen": hasattr(sys, 'frozen'),
        "argv0": sys.argv[0],
        "base": str(base),
        "dist": str(dist),
        "exists": dist.exists(),
    }


from fastapi.staticfiles import StaticFiles
from pathlib import Path as _Path
import sys as _sys

def _is_frozen() -> bool:
    return hasattr(_sys, 'frozen') or (_sys.argv[0] and _sys.argv[0].endswith('.exe'))

if _is_frozen():
    _base = _Path(_sys.argv[0]).resolve().parent
    _dist = _base / "_internal" / "web" / "dist"
else:
    _dist = _Path(__file__).resolve().parent.parent.parent / "web" / "dist"
logger.info("Static dist path: %s (exists=%s)", _dist, _dist.exists())
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8888)
