from fastapi import FastAPI
from contextlib import asynccontextmanager

from core.db import init_db
from core.api.stock_pools import router as stock_pools_router
from core.api.formulas import router as formulas_router
from core.api.strategies import router as strategies_router
from core.api.backtest import router as backtest_router
from core.api.live import router as live_router
from core.api.system import router as system_router
from core.api.status import router as status_router
from core.logging_config import setup_logging


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


@app.get("/health")
async def health():
    return {"ok": True}
