from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True, echo=False)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


async def init_models():
    # v1 用 create_all 建表;后续引入 Alembic 做迁移
    import asyncio
    from . import models  # noqa: F401
    last = None
    for _ in range(30):            # 等 Postgres 就绪,最多约 30 秒
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            return
        except Exception as e:     # 容器启动早于 db 接受连接时重试
            last = e
            await asyncio.sleep(1)
    raise last
