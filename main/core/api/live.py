from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.db import get_db

router = APIRouter(prefix="/api/live", tags=["live"])


@router.get("/sessions")
def list_sessions(db: Session = Depends(get_db)):
    return {"code": 0, "data": []}
