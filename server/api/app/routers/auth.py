from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, timedelta, timezone

from ..auth import (
    decode_token, hash_password, make_token, new_refresh_token, new_totp_secret,
    totp_uri, verify_password, verify_totp,
)
from ..config import settings
from ..db import get_db
from ..models import RefreshToken, User

router = APIRouter(prefix="/auth", tags=["auth"])


async def issue_tokens(db: AsyncSession, user: User) -> dict:
    """签发一对令牌:短期 access(JWT) + 长效 refresh(存库)。登录/刷新都走这里。"""
    rt = RefreshToken(user_id=user.id, token=new_refresh_token(),
                      expires_at=datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_days))
    db.add(rt)
    await db.commit()
    return {"access_token": make_token(user.id, {"mfa_ok": True}),
            "refresh_token": rt.token, "token_type": "bearer"}


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


class GoogleIn(BaseModel):
    credential: str   # Google Identity Services 返回的 ID token


@router.post("/google")
async def google_login(body: GoogleIn, db: AsyncSession = Depends(get_db)):
    if not settings.google_client_id:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "服务端未配置 Google 登录")
    # 延迟导入,避免未配置时的额外依赖开销
    from google.auth.transport import requests as g_requests
    from google.oauth2 import id_token as g_id_token
    try:
        info = g_id_token.verify_oauth2_token(
            body.credential, g_requests.Request(), settings.google_client_id
        )
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Google 令牌无效")
    if not info.get("email_verified"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Google 邮箱未验证")
    email = info["email"].lower()
    sub = info.get("sub")
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        user = User(email=email, provider="google", google_sub=sub)
        db.add(user)
        await db.commit()
    elif not user.google_sub:
        user.google_sub = sub          # 已有邮箱账号首次用 Google 登录 → 关联
        await db.commit()
    # Google 已完成强认证,直接发正式令牌
    return await issue_tokens(db, user)


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
    return await issue_tokens(db, user)


@router.post("/login")
async def login(body: LoginIn, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if user and not user.password_hash:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该账号用 Google 登录,请点「用 Google 登录」")
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
    return await issue_tokens(db, user)


class RefreshIn(BaseModel):
    refresh_token: str


@router.post("/refresh")
async def refresh(body: RefreshIn, db: AsyncSession = Depends(get_db)):
    """用 refresh 令牌静默换新:校验未撤销未过期 → 撤销旧的(轮换)→ 发新的一对。"""
    rt = (await db.execute(
        select(RefreshToken).where(RefreshToken.token == body.refresh_token)
    )).scalar_one_or_none()
    if not rt or rt.revoked or rt.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh 令牌无效或过期")
    user = await db.get(User, rt.user_id)
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或被禁用")
    rt.revoked = True                     # 轮换:旧 refresh 用一次即失效
    await db.commit()
    return await issue_tokens(db, user)
