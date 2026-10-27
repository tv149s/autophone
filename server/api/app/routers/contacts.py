import csv
import io
import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import current_user
from ..models import Contact, User
from ..phoneutil import normalize

router = APIRouter(prefix="/contacts", tags=["contacts"])


class ContactIn(BaseModel):
    name: str = ""
    phone: str = ""
    title: str = ""
    relationship: str = ""
    birthday: str | None = None
    age_range: str = ""
    preferences: str = ""
    notes: str = ""
    org: str = ""
    tags: list[str] = []
    soul_id: str | None = None


class ImportIn(BaseModel):
    kind: str          # "vcard" | "csv"
    text: str


def _view(c: Contact) -> dict:
    return {
        "id": c.id, "name": c.name, "phone": c.phone, "phone_raw": c.phone_raw,
        "title": c.title, "relationship": c.relationship, "birthday": c.birthday,
        "age_range": c.age_range, "preferences": c.preferences, "notes": c.notes,
        "org": c.org, "tags": c.tags or [], "soul_id": c.soul_id, "source": c.source,
        "updated_at": c.updated_at,
    }


@router.get("")
async def list_contacts(q: str = "", user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    stmt = select(Contact).where(Contact.user_id == user.id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Contact.name.ilike(like), Contact.phone.ilike(like), Contact.org.ilike(like)))
    stmt = stmt.order_by(Contact.name).limit(1000)
    rows = (await db.execute(stmt)).scalars().all()
    return [_view(c) for c in rows]


async def _upsert(db, user_id, data: dict, source: str):
    """按 (user, 归一化号码) 去重合并;有号码则合并,无号码则新建。返回 ('created'|'merged')。"""
    phone = normalize(data.get("phone", ""))
    existing = None
    if phone:
        existing = (await db.execute(
            select(Contact).where(Contact.user_id == user_id, Contact.phone == phone)
        )).scalar_one_or_none()
    if existing:
        # 只补空字段,不覆盖已有(保护手填资料)
        for k in ("name", "title", "relationship", "birthday", "age_range", "org"):
            if not getattr(existing, k) and data.get(k):
                setattr(existing, k, data[k])
        for k in ("preferences", "notes"):
            if data.get(k) and data[k] not in (getattr(existing, k) or ""):
                existing.__setattr__(k, (getattr(existing, k) + "\n" + data[k]).strip() if getattr(existing, k) else data[k])
        return "merged"
    c = Contact(user_id=user_id, phone=phone, phone_raw=data.get("phone", ""), source=source,
                name=data.get("name", ""), title=data.get("title", ""),
                relationship=data.get("relationship", ""), birthday=data.get("birthday"),
                age_range=data.get("age_range", ""), preferences=data.get("preferences", ""),
                notes=data.get("notes", ""), org=data.get("org", ""), tags=data.get("tags", []))
    db.add(c)
    return "created"


@router.post("")
async def create_contact(body: ContactIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    phone = normalize(body.phone)
    if phone:
        dup = (await db.execute(select(Contact).where(Contact.user_id == user.id, Contact.phone == phone))).scalar_one_or_none()
        if dup:
            raise HTTPException(status.HTTP_409_CONFLICT, "该号码已在通讯录中")
    c = Contact(user_id=user.id, phone=phone, phone_raw=body.phone, source="manual",
                **body.model_dump(exclude={"phone"}))
    db.add(c)
    await db.commit()
    return _view(c)


@router.patch("/{cid}")
async def update_contact(cid: str, body: ContactIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    c = await db.get(Contact, cid)
    if not c or c.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "联系人不存在")
    for k, v in body.model_dump(exclude={"phone"}).items():
        setattr(c, k, v)
    if body.phone:
        c.phone = normalize(body.phone)
        c.phone_raw = body.phone
    await db.commit()
    return _view(c)


@router.delete("/{cid}")
async def delete_contact(cid: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    c = await db.get(Contact, cid)
    if not c or c.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "联系人不存在")
    await db.delete(c)
    await db.commit()
    return {"ok": True}


def _parse_vcard(text: str) -> list[dict]:
    # 先做续行折叠(以空格/制表符开头的行接上一行)
    lines = []
    for ln in text.splitlines():
        if ln[:1] in (" ", "\t") and lines:
            lines[-1] += ln[1:]
        else:
            lines.append(ln)
    out, cur = [], None
    for ln in lines:
        u = ln.strip()
        if u.upper() == "BEGIN:VCARD":
            cur = {"tags": []}
        elif u.upper() == "END:VCARD":
            if cur:
                out.append(cur); cur = None
        elif cur is not None and ":" in u:
            key, val = u.split(":", 1)
            k = key.split(";")[0].upper()
            val = val.strip()
            if k == "FN":
                cur["name"] = val
            elif k == "N" and not cur.get("name"):
                parts = [p for p in val.split(";") if p]
                cur["name"] = " ".join(reversed(parts[:2])) if parts else val
            elif k == "TEL":
                cur.setdefault("phone", val)        # 取第一个号码
            elif k == "BDAY":
                cur["birthday"] = val[:10]
            elif k == "ORG":
                cur["org"] = val.replace(";", " ").strip()
            elif k == "TITLE":
                cur.setdefault("age_range", "")      # TITLE 不入关键字段,忽略
            elif k == "NOTE":
                cur["notes"] = val
    return [c for c in out if c.get("phone") or c.get("name")]


def _parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    out = []
    for row in reader:
        low = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}

        def pick(*keys):
            for key in keys:
                for col, val in low.items():
                    if key in col and val:
                        return val
            return ""
        name = pick("name", "姓名", "名字") or (pick("first") + " " + pick("last")).strip()
        phone = pick("phone", "mobile", "tel", "电话", "手机", "号码")
        if not (name or phone):
            continue
        out.append({
            "name": name, "phone": phone,
            "org": pick("organization", "company", "org", "公司"),
            "birthday": pick("birthday", "生日")[:10],
            "notes": pick("note", "备注"),
            "tags": [],
        })
    return out


@router.post("/import")
async def import_contacts(body: ImportIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    items = _parse_vcard(body.text) if body.kind == "vcard" else _parse_csv(body.text)
    if not items:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没解析出联系人,请检查文件格式")
    created = merged = 0
    for it in items:
        r = await _upsert(db, user.id, it, source=body.kind)
        created += r == "created"
        merged += r == "merged"
    await db.commit()
    return {"total": len(items), "created": created, "merged": merged}
