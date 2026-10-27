from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import current_user
from ..models import Device, Phone, User
from ..ws_manager import hub

router = APIRouter(prefix="/phones", tags=["phones"])


def _view(p: Phone, device_name: str = "") -> dict:
    return {
        "id": p.id, "device_id": p.device_id, "device_name": device_name,
        "label": p.label, "platform": p.platform, "udid": p.udid, "number": p.number,
        "wda_ready": p.wda_ready, "locked": p.locked, "auto_answer": p.auto_answer,
        "channels": p.channels or [], "last_report": p.last_report,
    }


@router.get("")
async def list_phones(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    phones = (await db.execute(select(Phone).where(Phone.user_id == user.id))).scalars().all()
    devs = {d.id: d.name for d in (await db.execute(select(Device).where(Device.user_id == user.id))).scalars().all()}
    return [_view(p, devs.get(p.device_id, "")) for p in phones]


class AutoAnswerIn(BaseModel):
    auto_answer: bool


@router.patch("/{pid}")
async def set_auto_answer(pid: str, body: AutoAnswerIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(Phone, pid)
    if not p or p.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "电话不存在")
    p.auto_answer = body.auto_answer
    await db.commit()
    return _view(p)


class ChannelIn(BaseModel):
    kind: str
    enabled: bool | None = None
    asr: str | None = None
    reply_language: str | None = None


@router.patch("/{pid}/channel")
async def set_channel(pid: str, body: ChannelIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(Phone, pid)
    if not p or p.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "电话不存在")
    patch = {k: v for k, v in body.model_dump(exclude={"kind"}).items() if v is not None}
    ov = dict(p.overrides or {})
    ov[body.kind] = {**ov.get(body.kind, {}), **patch}
    p.overrides = ov
    # 立刻把覆盖应用到展示用的 channels
    ch = [dict(c) for c in (p.channels or [])]
    for c in ch:
        if c.get("kind") == body.kind:
            c.update(patch)
    p.channels = ch
    await db.commit()
    # 推给边缘端写 channels.json(在线才推;离线时下次连上由边缘按需要同步)
    pushed = await hub.send(p.device_id, {"type": "channel_config", "channels": {body.kind: patch}})
    return {**_view(p), "pushed": pushed}
