import asyncio
import json
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from core.db import get_db
from core.models import LiveSession, LiveSessionPortfolio

router = APIRouter(prefix="/api/live", tags=["live"])


@router.get("/sessions")
def list_sessions(db: Session = Depends(get_db)):
    sessions = db.query(LiveSession).all()
    return {
        "code": 0,
        "data": [
            {
                "id": s.id,
                "name": s.name,
                "mode": s.mode,
                "status": s.status,
                "started_at": s.started_at,
                "stopped_at": s.stopped_at,
            }
            for s in sessions
        ],
    }


@router.post("/sessions")
def create_session(data: dict, db: Session = Depends(get_db)):
    session = LiveSession(
        name=data["name"],
        mode=data.get("mode", "simulation"),
        status="stopped",
    )
    db.add(session)
    db.flush()
    for pid in data.get("portfolio_ids", []):
        link = LiveSessionPortfolio(session_id=session.id, portfolio_strategy_id=pid)
        db.add(link)
    db.commit()
    return {"code": 0, "data": {"id": session.id, "status": session.status}}


@router.get("/sessions/{session_id}")
def get_session(session_id: int, db: Session = Depends(get_db)):
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return {"code": 404, "message": "资源不存在"}
    portfolios = (
        db.query(LiveSessionPortfolio)
        .filter(LiveSessionPortfolio.session_id == session_id)
        .all()
    )
    return {
        "code": 0,
        "data": {
            "id": session.id,
            "name": session.name,
            "mode": session.mode,
            "status": session.status,
            "portfolios": [
                {"portfolio_id": p.portfolio_strategy_id, "status": p.status}
                for p in portfolios
            ],
        },
    }


@router.post("/sessions/{session_id}/start")
def start_session(session_id: int, db: Session = Depends(get_db)):
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return {"code": 404, "message": "资源不存在"}
    session.status = "running"
    session.started_at = datetime.now()
    db.commit()
    return {"code": 0, "data": {"id": session.id, "status": "running"}}


@router.post("/sessions/{session_id}/stop")
def stop_session(session_id: int, db: Session = Depends(get_db)):
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return {"code": 404, "message": "资源不存在"}
    session.status = "stopped"
    session.stopped_at = datetime.now()
    db.commit()
    return {"code": 0, "data": {"id": session.id, "status": "stopped"}}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: int, db: Session = Depends(get_db)):
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return {"code": 404, "message": "资源不存在"}
    db.query(LiveSessionPortfolio).filter(
        LiveSessionPortfolio.session_id == session_id
    ).delete()
    db.delete(session)
    db.commit()
    return {"code": 0}


@router.get("/sessions/{session_id}/stream")
async def session_stream(session_id: int, request: Request, db: Session = Depends(get_db)):
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return {"code": 404, "message": "资源不存在"}

    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            yield f"event: ping\ndata: {json.dumps({'time': datetime.now().isoformat()})}\n\n"
            await asyncio.sleep(30)

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
