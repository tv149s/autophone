from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import current_user
from ..models import Device, Task, User
from ..ws_manager import hub

router = APIRouter(prefix="/tasks", tags=["tasks"])


class TaskIn(BaseModel):
    device_id: str | None = None
    direction: str = "outbound"
    source: str = "web"
    card: dict = {}


def _view(t: Task) -> dict:
    return {
        "id": t.id, "device_id": t.device_id, "direction": t.direction,
        "source": t.source, "card": t.card, "status": t.status, "result": t.result,
        "recording_path": t.recording_path, "created_at": t.created_at, "updated_at": t.updated_at,
    }


@router.get("")
async def list_tasks(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(Task).where(Task.user_id == user.id).order_by(Task.created_at.desc()).limit(200)
    )).scalars().all()
    return [_view(t) for t in rows]


@router.post("")
async def create_task(body: TaskIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if body.device_id:
        d = await db.get(Device, body.device_id)
        if not d or d.user_id != user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "设备不存在")
    t = Task(user_id=user.id, device_id=body.device_id, direction=body.direction,
             source=body.source, card=body.card)
    db.add(t)
    await db.commit()
    # 若指定设备在线,立即下推
    if body.device_id and hub.is_online(body.device_id):
        await hub.send(body.device_id, {"type": "task", "task": _view(t)})
    return _view(t)


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    t = await db.get(Task, task_id)
    if not t or t.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    if t.status in ("done", "failed"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "任务已结束")
    t.status = "canceled"
    await db.commit()
    return _view(t)
