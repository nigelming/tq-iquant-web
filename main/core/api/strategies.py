from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.db import get_db

router = APIRouter(prefix="/api/portfolios", tags=["portfolios"])


@router.get("")
def list_portfolios(db: Session = Depends(get_db)):
    return {"code": 0, "data": []}
