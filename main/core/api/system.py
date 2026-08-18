from fastapi import APIRouter

from core.api.response import ok
from core.services.system_service import get_config, update_config

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/configs")
def get_config_route():
    return ok(get_config())


@router.put("/configs")
def update_config_route(data: dict):
    update_config(data)
    return ok()
