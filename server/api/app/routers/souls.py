from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import current_user
from ..models import Soul, User

router = APIRouter(prefix="/souls", tags=["souls"])

# 新用户首次访问时播下的起步身份(可在网页改/删/加)
SEED = [
    {"name": "接线员", "body": "You are the phone receptionist for the owner. Be warm, concise and professional. "
                              "Find out who is calling and why, take a message, and let them know the owner will get back to them.",
     "meta": {"role": "receptionist", "reply_language": "follow", "tier": "free",
              "tools": ["set_category", "take_message", "end_call"]}},
    {"name": "老友寒暄", "body": "This caller is a personal friend. Be relaxed, warm and casual. "
                               "Chat naturally, catch up briefly, and offer to pass along a message to the owner.",
     "meta": {"role": "receptionist", "reply_language": "follow", "tier": "free",
              "tools": ["take_message", "end_call"]}},
    {"name": "同事协作", "body": "This caller is a work colleague. Be professional and to the point. "
                               "Understand the technical or work matter, capture the key points, and arrange follow-up.",
     "meta": {"role": "receptionist", "reply_language": "follow", "tier": "free",
              "tools": ["take_message", "end_call"]}},
]


class SoulIn(BaseModel):
    name: str
    body: str = ""
    meta: dict = {}


def _view(s: Soul) -> dict:
    return {"id": s.id, "name": s.name, "body": s.body, "meta": s.meta or {}, "created_at": s.created_at}


@router.get("")
async def list_souls(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Soul).where(Soul.user_id == user.id).order_by(Soul.created_at))).scalars().all()
    if not rows:                      # 首次:播下起步身份
        for s in SEED:
            db.add(Soul(user_id=user.id, name=s["name"], body=s["body"], meta=s["meta"]))
        await db.commit()
        rows = (await db.execute(select(Soul).where(Soul.user_id == user.id).order_by(Soul.created_at))).scalars().all()
    return [_view(s) for s in rows]


@router.post("")
async def create_soul(body: SoulIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    s = Soul(user_id=user.id, name=body.name, body=body.body, meta=body.meta)
    db.add(s)
    await db.commit()
    return _view(s)


@router.patch("/{sid}")
async def update_soul(sid: str, body: SoulIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    s = await db.get(Soul, sid)
    if not s or s.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "身份不存在")
    s.name, s.body, s.meta = body.name, body.body, body.meta
    await db.commit()
    return _view(s)


@router.delete("/{sid}")
async def delete_soul(sid: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    s = await db.get(Soul, sid)
    if not s or s.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "身份不存在")
    await db.delete(s)
    await db.commit()
    return {"ok": True}
