from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import settings
from .db import init_models
from .routers import auth, devices, tasks, ws


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_models()
    yield


app = FastAPI(title="AutoPhoneAgent API", version="0.1.0", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(devices.router)
app.include_router(tasks.router)
app.include_router(ws.router)


@app.get("/health")
async def health():
    return {"ok": True, "env": settings.env, "service": "autophoneagent-api"}
