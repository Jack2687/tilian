from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import text
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
if settings.database_url.startswith("sqlite"):
    Path("data").mkdir(parents=True, exist_ok=True)

engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    echo=False,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def create_schema() -> None:
    from . import models  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        statements = [
            "ALTER TABLE quota_cards_v2 ADD COLUMN IF NOT EXISTS fusion_disabled BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE quota_cards_v2 ADD COLUMN IF NOT EXISTS code_value VARCHAR(160) NOT NULL DEFAULT ''",
            "ALTER TABLE card_fusions_v2 ADD COLUMN IF NOT EXISTS new_card_value VARCHAR(160) NOT NULL DEFAULT ''",
            "ALTER TABLE card_fusions_v2 ADD COLUMN IF NOT EXISTS source_values JSONB NOT NULL DEFAULT '[]'::jsonb",
            "ALTER TABLE quota_cards_v2 ADD COLUMN IF NOT EXISTS remark VARCHAR(160) NOT NULL DEFAULT ''",
            "ALTER TABLE quota_cards_v2 ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ NULL",
            "ALTER TABLE tasks_v2 ADD COLUMN IF NOT EXISTS owner_value VARCHAR(160) NOT NULL DEFAULT ''",
            "ALTER TABLE tasks_v2 ADD COLUMN IF NOT EXISTS user_id INTEGER NULL",
            "ALTER TABLE quota_cards_v2 ADD COLUMN IF NOT EXISTS owner_user_id INTEGER NULL",
            "ALTER TABLE quota_cards_v2 ADD COLUMN IF NOT EXISTS bound_at TIMESTAMPTZ NULL",
        ] if settings.database_url.startswith("postgresql") else [
            "ALTER TABLE quota_cards_v2 ADD COLUMN fusion_disabled BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE quota_cards_v2 ADD COLUMN code_value VARCHAR(160) NOT NULL DEFAULT ''",
            "ALTER TABLE card_fusions_v2 ADD COLUMN new_card_value VARCHAR(160) NOT NULL DEFAULT ''",
            "ALTER TABLE card_fusions_v2 ADD COLUMN source_values JSON NOT NULL DEFAULT '[]'",
            "ALTER TABLE quota_cards_v2 ADD COLUMN remark VARCHAR(160) NOT NULL DEFAULT ''",
            "ALTER TABLE quota_cards_v2 ADD COLUMN expires_at DATETIME NULL",
            "ALTER TABLE tasks_v2 ADD COLUMN owner_value VARCHAR(160) NOT NULL DEFAULT ''",
            "ALTER TABLE tasks_v2 ADD COLUMN user_id INTEGER NULL",
            "ALTER TABLE quota_cards_v2 ADD COLUMN owner_user_id INTEGER NULL",
            "ALTER TABLE quota_cards_v2 ADD COLUMN bound_at DATETIME NULL",
        ]
        for statement in statements:
            try:
                await connection.execute(text(statement))
            except Exception:
                pass


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
