from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import new_device_token
from ..db import get_db
from ..deps import current_user
from ..models import Device, User
from ..ws_manager import hub, manual_status

router = APIRouter(prefix="/devices", tags=["devices"])


class DeviceIn(BaseModel):
    name: str = ""
    kind: str = "mac"


class RegisterIn(BaseModel):
    name: str = ""
    kind: str = "mac"
    hostname: str = ""


def _view(d: Device) -> dict:
    return {
        "id": d.id, "name": d.name, "kind": d.kind, "hostname": d.hostname,
        "online": d.online, "last_seen": d.last_seen, "created_at": d.created_at,
        "phone_registry": d.phone_registry or [],
    }


class RenameIn(BaseModel):
    name: str


@router.get("")
async def list_devices(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Device).where(Device.user_id == user.id))).scalars().all()
    return [_view(d) for d in rows]


@router.post("")
async def create_device(body: DeviceIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    d = Device(user_id=user.id, name=body.name, kind=body.kind, token=new_device_token())
    db.add(d)
    await db.commit()
    # token 只在创建时返回一次,边缘端用它连 WSS
    return {**_view(d), "token": d.token}


@router.post("/register")
async def register_self(body: RegisterIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """客户端用自己的登录身份自助注册设备(登录即生效)。按 (user, hostname) 幂等,
    同一台机器重复注册复用同一设备与令牌,避免重复堆积。"""
    dev = None
    if body.hostname:
        dev = (await db.execute(
            select(Device).where(Device.user_id == user.id, Device.hostname == body.hostname)
        )).scalar_one_or_none()
    if dev is None:
        dev = Device(user_id=user.id, name=body.name or body.hostname or "设备",
                     kind=body.kind, hostname=body.hostname, token=new_device_token())
        db.add(dev)
    elif body.name:
        dev.name = body.name
    await db.commit()
    return {**_view(dev), "token": dev.token}


class SwitchPhoneIn(BaseModel):
    phone_id: str


@router.post("/{device_id}/switch-phone")
async def switch_phone(device_id: str, body: SwitchPhoneIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    d = await db.get(Device, device_id)
    if not d or d.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "设备不存在")
    if not d.online:
        raise HTTPException(status.HTTP_409_CONFLICT, "设备离线,无法切换")
    ok = await hub.send(device_id, {"type": "switch_phone", "phone_id": body.phone_id})
    if not ok:
        raise HTTPException(status.HTTP_409_CONFLICT, "设备未连接")
    return {"ok": True, "switching_to": body.phone_id}


class SetMonitorIn(BaseModel):
    phones: list[str]          # 监控集合(最多 2 台);1 台=单机,2 台=双机监控


@router.post("/{device_id}/set-monitor")
async def set_monitor(device_id: str, body: SetMonitorIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    d = await db.get(Device, device_id)
    if not d or d.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "设备不存在")
    if not d.online:
        raise HTTPException(status.HTTP_409_CONFLICT, "设备离线,无法设置")
    ids = [p for p in body.phones if p][:2]
    if not ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "至少选一台电话")
    ok = await hub.send(device_id, {"type": "set_monitor", "phones": ids})
    if not ok:
        raise HTTPException(status.HTTP_409_CONFLICT, "设备未连接")
    return {"ok": True, "monitor": ids}


class ManualSessionIn(BaseModel):
    action: str                    # start / stop
    mode: str = "listen"           # listen=纯聆听 / speak=可发言(后续阶段)
    requirement: str = ""          # 用户填的"要求"(总结/纪要的指令)
    soul: str = ""                 # 可选身份(soul 名)


@router.post("/{device_id}/manual-session")
async def manual_session(device_id: str, body: ManualSessionIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """人工接入节点:手动音频会话。start 下发开始聆听,stop 结束并生成纪要。"""
    d = await db.get(Device, device_id)
    if not d or d.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "设备不存在")
    if not d.online:
        raise HTTPException(status.HTTP_409_CONFLICT, "设备离线,无法操作")
    ok = await hub.send(device_id, {
        "type": "manual_session", "action": body.action, "mode": body.mode,
        "requirement": body.requirement, "soul": body.soul,
    })
    if not ok:
        raise HTTPException(status.HTTP_409_CONFLICT, "设备未连接")
    return {"ok": True, "action": body.action}


@router.get("/{device_id}/manual-status")
async def manual_status_get(device_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    d = await db.get(Device, device_id)
    if not d or d.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "设备不存在")
    return manual_status.get(device_id) or {"state": "idle"}


@router.post("/{device_id}/rotate-token")
async def rotate_token(device_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    d = await db.get(Device, device_id)
    if not d or d.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "设备不存在")
    d.token = new_device_token()
    await db.commit()
    return {**_view(d), "token": d.token}


@router.patch("/{device_id}")
async def rename_device(device_id: str, body: RenameIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    d = await db.get(Device, device_id)
    if not d or d.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "设备不存在")
    d.name = body.name
    await db.commit()
    return _view(d)


@router.delete("/{device_id}")
async def delete_device(device_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    d = await db.get(Device, device_id)
    if not d or d.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "设备不存在")
    await db.delete(d)
    await db.commit()
    return {"ok": True}
