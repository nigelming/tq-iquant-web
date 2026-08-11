from fastapi import APIRouter
import time

from core.api.response import ok

_start_time = time.time()

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/status")
def get_status():
    uptime_seconds = int(time.time() - _start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return ok({
        "core": {
            "online": True,
            "version": "1.0",
            "uptime": f"{hours}h{minutes}m",
        },
        "iguant_gateway": {
            "online": False,
            "version": "1.0",
            "uptime": "0h0m",
        },
    })
