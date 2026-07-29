from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.db import get_db

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


@router.get("/records")
def list_records(db: Session = Depends(get_db)):
    return {"code": 0, "data": []}
