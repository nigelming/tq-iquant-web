from fastapi import APIRouter

from core.api.response import ok
from core.config import load_config, save_config

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/configs")
def get_config():
    cfg = load_config()
    return ok(cfg)


@router.put("/configs")
def update_config(data: dict):
    save_config(data)
    return ok()
