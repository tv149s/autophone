"""工作记忆汇总:把所有联系人的通话记忆按时间倒序拉平,支持按渠道/名字筛选。
用于网页「记忆」页的时间流视图(单条点进可回到联系人)。"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import current_user
from ..models import Contact, ContactMemory, User

router = APIRouter(prefix="/memories", tags=["memories"])


@router.get("")
async def list_memories(channel: str = "", q: str = "", limit: int = 200,
                        user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    stmt = (select(ContactMemory, Contact)
            .join(Contact, Contact.id == ContactMemory.contact_id)
            .where(ContactMemory.user_id == user.id))
    if channel:
        # 蜂窝联系人 channel 存空字符串,前端用 "cellular" 表示
        stmt = stmt.where(Contact.channel == ("" if channel == "cellular" else channel))
    if q:
        stmt = stmt.where(Contact.name.ilike(f"%{q}%"))
    stmt = stmt.order_by(ContactMemory.created_at.desc()).limit(min(limit, 500))
    rows = (await db.execute(stmt)).all()
    return [{
        "id": m.id, "contact_id": m.contact_id, "call_id": m.call_id,
        "summary": m.summary, "created_at": m.created_at,
        "channel": c.channel or "cellular", "name": c.name, "phone": c.phone,
    } for m, c in rows]


@router.get("/channels")
async def memory_channels(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """有记忆的渠道列表(供前端生成筛选按钮),含每个渠道的记忆条数。"""
    rows = (await db.execute(
        select(Contact.channel, ContactMemory.id)
        .join(Contact, Contact.id == ContactMemory.contact_id)
        .where(ContactMemory.user_id == user.id)
    )).all()
    counts: dict[str, int] = {}
    for ch, _ in rows:
        key = ch or "cellular"
        counts[key] = counts.get(key, 0) + 1
    return counts
