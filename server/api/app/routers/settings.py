from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import current_user
from ..models import User

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingsIn(BaseModel):
    default_greeting: str


@router.get("")
async def get_settings(user: User = Depends(current_user)):
    return {"default_greeting": user.default_greeting or "", "email": user.email}


@router.patch("")
async def update_settings(body: SettingsIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    user.default_greeting = body.default_greeting
    await db.commit()
    return {"default_greeting": user.default_greeting}
