from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool
from sqlalchemy import text
import os
import logging

logger = logging.getLogger("vynex.db")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./vynex.db")
_IS_PROD = os.getenv("BASE_URL", "").startswith("https://")

# Production must use Postgres. Railway's ephemeral filesystem would
# silently wipe a SQLite DB on every deploy, so fall-through is fatal.
if _IS_PROD and "sqlite" in DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL deve puntare a Postgres in produzione — "
        "SQLite su filesystem effimero perde tutti i dati al redeploy"
    )

engine_kwargs = {"echo": False}
if "asyncpg" in DATABASE_URL:
    engine_kwargs["connect_args"] = {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        # 15s query timeout: long AI-blocking queries are cut off so
        # misbehaving requests can't hold a connection indefinitely.
        "server_settings": {"statement_timeout": "15000"},
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
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS consent_accepted_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS consent_ip VARCHAR(45)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS consent_user_agent VARCHAR(500)",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS tokens_used INTEGER",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS generation_time_ms INTEGER",
    "CREATE INDEX IF NOT EXISTS ix_documents_user_id ON documents (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_documents_created_at ON documents (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_users_stripe_customer_id ON users (stripe_customer_id)",
]


async def _drop_orphan_sequences(conn) -> None:
    # A past deploy crashed between creating the sequence and the table,
    # leaving `*_id_seq` dangling. create_all would then collide on the
    # next SERIAL create. Drop any sequence whose owning table is missing.
    rows = await conn.execute(text(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind = 'S' AND n.nspname = 'public' "
        "AND c.relname LIKE '%\\_id\\_seq' ESCAPE '\\'"
    ))
    for (seq_name,) in rows.fetchall():
        tbl = seq_name[:-len("_id_seq")]
        exists = (await conn.execute(text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name=:t"
        ), {"t": tbl})).scalar()
        if not exists:
            logger.warning("dropping orphan sequence %s (table %s missing)", seq_name, tbl)
            await conn.execute(text(f'DROP SEQUENCE IF EXISTS public."{seq_name}" CASCADE'))


async def init_db():
    async with engine.begin() as conn:
        if "asyncpg" in DATABASE_URL:
            try:
                await _drop_orphan_sequences(conn)
            except Exception as exc:
                logger.warning("orphan sequence cleanup failed: %s", exc)

        await conn.run_sync(Base.metadata.create_all)

        if "asyncpg" in DATABASE_URL:
            for stmt in _PG_MIGRATIONS:
                try:
                    await conn.execute(text(stmt))
                except Exception as exc:
                    logger.warning("migration step failed: %s — %s", stmt, exc)
