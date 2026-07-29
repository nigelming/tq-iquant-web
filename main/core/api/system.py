from fastapi import APIRouter

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/configs")
def get_config():
    return {"code": 0, "data": {}}
