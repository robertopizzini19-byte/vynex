from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool
from sqlalchemy import text
import os
import logging

logger = logging.getLogger("vynex.db")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./vynex.db")

engine_kwargs = {"echo": False}
if "asyncpg" in DATABASE_URL:
    engine_kwargs["connect_args"] = {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
    }
    engine_kwargs["poolclass"] = NullPool

engine = create_async_engine(DATABASE_URL, **engine_kwargs)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


# Idempotent ALTER statements run after create_all so that existing
# Supabase tables get extended without losing data. Postgres-only.
_PG_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_login_attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_status VARCHAR(30)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_current_period_end TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS tokens_used INTEGER",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS generation_time_ms INTEGER",
    "CREATE INDEX IF NOT EXISTS ix_documents_user_id ON documents (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_documents_created_at ON documents (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_users_stripe_customer_id ON users (stripe_customer_id)",
]


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        if "asyncpg" in DATABASE_URL:
            for stmt in _PG_MIGRATIONS:
                try:
                    await conn.execute(text(stmt))
                except Exception as exc:
                    logger.warning("migration step failed: %s — %s", stmt, exc)
