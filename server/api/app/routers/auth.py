from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    decode_token, hash_password, make_token, new_totp_secret,
    totp_uri, verify_password, verify_totp,
)
from ..db import get_db
from ..models import User

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: EmailStr
    password: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class EnableIn(BaseModel):
    email: EmailStr
    password: str
    code: str


class CodeIn(BaseModel):
    code: str


@router.post("/register")
async def register(body: RegisterIn, db: AsyncSession = Depends(get_db)):
    exists = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "邮箱已注册")
    if len(body.password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "密码至少 8 位")
    secret = new_totp_secret()
    user = User(email=body.email, password_hash=hash_password(body.password), totp_secret=secret)
    db.add(user)
    await db.commit()
    # 返回 2FA 录入信息,用户在验证器 App 扫码后调用 /auth/2fa/enable 启用
    return {"user_id": user.id, "totp_secret": secret, "totp_uri": totp_uri(secret, body.email)}


@router.post("/2fa/enable")
async def enable_2fa(body: EnableIn, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "邮箱或密码错误")
    if not verify_totp(user.totp_secret, body.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证码错误")
    user.totp_enabled = True
    await db.commit()
    return {"access_token": make_token(user.id, {"mfa_ok": True}), "token_type": "bearer"}


@router.post("/login")
async def login(body: LoginIn, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "邮箱或密码错误")
    if not user.totp_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "请先启用二次验证(/auth/2fa/enable)")
    # 第一步只发短期 mfa 令牌,需再过 TOTP 才换正式令牌
    return {"mfa_required": True, "mfa_token": make_token(user.id, {"mfa_ok": False})}


@router.post("/2fa/verify")
async def verify_2fa(
    body: CodeIn,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少 mfa 令牌")
    try:
        payload = decode_token(authorization.split(" ", 1)[1])
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "mfa 令牌无效")
    user = await db.get(User, payload.get("sub"))
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在")
    if not verify_totp(user.totp_secret, body.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证码错误")
    return {"access_token": make_token(user.id, {"mfa_ok": True}), "token_type": "bearer"}
