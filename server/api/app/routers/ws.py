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


async def _sync_bundle(device_id: str) -> dict:
    """给边缘端下发:该用户的联系人(含绑定soul+最近记忆)+ 全部soul,供本地来电匹配与档案注入。"""
    from ..models import Contact, ContactMemory, Soul, User
    async with SessionLocal() as db:
        d = await db.get(Device, device_id)
        if not d:
            return {"contacts": [], "souls": [], "default_greeting": ""}
        u = await db.get(User, d.user_id)
        contacts = (await db.execute(select(Contact).where(Contact.user_id == d.user_id))).scalars().all()
        souls = (await db.execute(select(Soul).where(Soul.user_id == d.user_id))).scalars().all()
        mems = (await db.execute(
            select(ContactMemory).where(ContactMemory.user_id == d.user_id)
            .order_by(ContactMemory.created_at.desc())
        )).scalars().all()
        by_contact: dict[str, list] = {}
        for m in mems:
            by_contact.setdefault(m.contact_id, [])
            if len(by_contact[m.contact_id]) < 5:        # 每人最近 5 条
                by_contact[m.contact_id].append(m.summary)
        return {
            "contacts": [{
                "phone": c.phone, "name": c.name, "channel": c.channel, "title": c.title,
                "relationship": c.relationship, "birthday": c.birthday, "preferences": c.preferences,
                "notes": c.notes, "org": c.org, "soul_id": c.soul_id, "memories": by_contact.get(c.id, []),
            } for c in contacts if c.phone or c.name],
            "souls": [{"id": s.id, "name": s.name, "body": s.body, "meta": s.meta or {}} for s in souls],
            "default_greeting": (u.default_greeting if u else "") or "",
        }


async def _upsert_phones(device_id: str, phones: list, registry=None):
    """边缘端上报它控制的电话(状态+渠道)+ 可切换电话清单,按 (device, pkey) upsert。"""
    from ..models import Phone
    from datetime import datetime, timezone
    async with SessionLocal() as db:
        d = await db.get(Device, device_id)
        if not d:
            return
        if registry is not None:
            d.phone_registry = registry
        seen_pkeys = []
        for ph in phones or []:
            pkey = ph.get("pkey") or ph.get("udid") or ph.get("label") or "phone"
            seen_pkeys.append(pkey)
            p = (await db.execute(
                select(Phone).where(Phone.device_id == device_id, Phone.pkey == pkey)
            )).scalar_one_or_none()
            if p is None:
                p = Phone(user_id=d.user_id, device_id=device_id, pkey=pkey)
                db.add(p)
            p.label = ph.get("label", p.label or pkey)
            p.platform = ph.get("platform", "ios")
            p.udid = ph.get("udid", "") or p.udid
            p.number = ph.get("number", "") or p.number
            p.wda_ready = bool(ph.get("wda_ready", False))
            p.locked = bool(ph.get("locked", False))
            p.front_app = ph.get("front_app", "") or ""
            ch = ph.get("channels", []) or []
            ov = p.overrides or {}                 # 用户覆盖盖在边缘上报之上
            for c in ch:
                o = ov.get(c.get("kind"))
                if o:
                    c.update({k: v for k, v in o.items() if v is not None})
            p.channels = ch
            p.last_report = datetime.now(timezone.utc)
        # 清掉本次未上报的僵尸电话行(历史 active 切换/旧 pkey 规则残留,否则页面越积越多、出现重复)
        if phones:
            stale = (await db.execute(
                select(Phone).where(Phone.device_id == device_id, Phone.pkey.notin_(seen_pkeys))
            )).scalars().all()
            for p in stale:
                await db.delete(p)
        await db.commit()


async def _save_memory(device_id: str, msg: dict):
    """通话后把要点写进对应联系人的记忆时间线;陌生来电自动建最小联系人(越用越全)。
    电话按归一化号码匹配;微信等 App 来电按 (渠道, 名字) 匹配。"""
    from ..models import Contact, ContactMemory
    from ..phoneutil import normalize
    channel = (msg.get("channel") or "").strip()
    name = (msg.get("name") or "").strip()
    summary = (msg.get("summary") or "").strip()
    phone = normalize(msg.get("phone", "")) if channel in ("", "cellular") else ""
    if not summary:
        return
    async with SessionLocal() as db:
        d = await db.get(Device, device_id)
        if not d:
            return
        c = None
        if phone:
            c = (await db.execute(select(Contact).where(
                Contact.user_id == d.user_id, Contact.phone == phone))).scalar_one_or_none()
        elif channel and name:              # App 来电:按 (渠道,名字) 认人
            c = (await db.execute(select(Contact).where(
                Contact.user_id == d.user_id, Contact.channel == channel,
                Contact.name == name))).scalar_one_or_none()
        else:
            return
        if c is None:                       # 自动建最小联系人
            c = Contact(user_id=d.user_id, phone=phone, phone_raw=msg.get("phone", ""),
                        name=name, channel=channel if phone == "" else "", source="call")
            db.add(c)
            await db.flush()
        db.add(ContactMemory(user_id=d.user_id, contact_id=c.id,
                             call_id=msg.get("call_id"), summary=summary))
        await db.commit()


async def _import_contacts(device_id: str, items: list) -> dict:
    from .contacts import _upsert
    async with SessionLocal() as db:
        d = await db.get(Device, device_id)
        if not d:
            return {"created": 0, "merged": 0}
        created = merged = 0
        for it in items or []:
            r = await _upsert(db, d.user_id, it, source="device")
            created += r == "created"
            merged += r == "merged"
        await db.commit()
        return {"created": created, "merged": merged, "total": len(items or [])}


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
            elif kind == "contacts_import":
                res = await _import_contacts(device_id, msg.get("contacts", []))
                await ws.send_json({"type": "contacts_import_done", **res})
            elif kind == "sync_pull":
                await ws.send_json({"type": "sync", **await _sync_bundle(device_id)})
            elif kind == "call_memory":
                await _save_memory(device_id, msg)
            elif kind == "phone_info":
                await _upsert_phones(device_id, msg.get("phones", []), msg.get("registry"))
            elif kind == "status":
                await _mark(device_id, True)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        # 只有当移除的是当前连接(未被更新的重连替换)才标离线,避免重连竞态
        if await hub.disconnect(device_id, ws):
            await _mark(device_id, False)
