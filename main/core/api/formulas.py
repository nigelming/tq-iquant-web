from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.db import get_db

router = APIRouter(prefix="/api/formulas", tags=["formulas"])


@router.get("")
def list_formulas(db: Session = Depends(get_db)):
    return {"code": 0, "data": []}
