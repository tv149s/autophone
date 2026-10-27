"""边缘端 WSS 接入。边缘端带 device token 连上来(家里 NAT 后面主动连出),
服务器下推任务、接收状态/结果上报。"""
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from ..db import SessionLocal
from ..models import Device, Task
from ..ws_manager import hub

router = APIRouter()


def _now():
    return datetime.now(timezone.utc)


async def _auth_device(token: str) -> Device | None:
    async with SessionLocal() as db:
        d = (await db.execute(select(Device).where(Device.token == token))).scalar_one_or_none()
        return d


async def _mark(device_id: str, online: bool):
    async with SessionLocal() as db:
        d = await db.get(Device, device_id)
        if d:
            d.online = online
            d.last_seen = _now()
            await db.commit()


async def _push_pending(device_id: str):
    async with SessionLocal() as db:
        rows = (await db.execute(
            select(Task).where(Task.device_id == device_id, Task.status == "pending")
            .order_by(Task.created_at)
        )).scalars().all()
        for t in rows:
            await hub.send(device_id, {"type": "task", "task": {
                "id": t.id, "direction": t.direction, "card": t.card, "status": t.status,
            }})


async def _apply_report(device_id: str, msg: dict):
    task_id = msg.get("task_id")
    if not task_id:
        return
    async with SessionLocal() as db:
        t = await db.get(Task, task_id)
        if not t or t.device_id != device_id:
            return
        if "status" in msg:
            t.status = msg["status"]
        if "result" in msg:
            t.result = msg["result"]
        if "recording_path" in msg:
            t.recording_path = msg["recording_path"]
        await db.commit()


@router.websocket("/ws/edge")
async def edge_ws(ws: WebSocket, token: str = ""):
    device = await _auth_device(token)
    if device is None:
        await ws.close(code=4401)   # 未授权
        return
    device_id = device.id
    await ws.accept()
    await hub.connect(device_id, ws)
    await _mark(device_id, True)
    try:
        await ws.send_json({"type": "hello", "device_id": device_id})
        await _push_pending(device_id)
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            if kind == "ping":
                await ws.send_json({"type": "pong"})
            elif kind == "report":
                await _apply_report(device_id, msg)
            elif kind == "status":
                await _mark(device_id, True)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.disconnect(device_id, ws)
        await _mark(device_id, False)
