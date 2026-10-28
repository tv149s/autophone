from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import init_models
from .routers import auth, contacts, devices, memories, phones, souls, tasks, ws
from .routers import settings as settings_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_models()
    yield


app = FastAPI(title="AutoPhoneAgent API", version="0.1.0", lifespan=lifespan)

# 管理后台在 app. 子域,API 在 api. 子域,需放行跨子域调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://app.autophoneagent.com",
        "http://localhost:5173",   # 本地开发
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(devices.router)
app.include_router(phones.router)
app.include_router(contacts.router)
app.include_router(memories.router)
app.include_router(souls.router)
app.include_router(settings_router.router)
app.include_router(tasks.router)
app.include_router(ws.router)


@app.get("/health")
async def health():
    return {"ok": True, "env": settings.env, "service": "autophoneagent-api"}
