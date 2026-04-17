"""
Scheduler per il motore di acquisizione.

APScheduler async in-process. Si avvia nel lifespan di FastAPI e gira ogni
5 minuti scannando la coda EmailJob. Un solo worker (Railway Hobby = 1
replica): non serve lock distribuito, basta il CAS su sent_at.

Se in futuro si passa a multi-replica, basta aggiungere una lease via
`SELECT ... FOR UPDATE SKIP LOCKED` in acquisition.process_email_queue —
già compatibile col pattern.
"""
from __future__ import annotations

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database import AsyncSessionLocal
from acquisition import process_email_queue

logger = logging.getLogger("vynex.scheduler")

_scheduler: AsyncIOScheduler | None = None


async def _run_cycle() -> None:
    try:
        async with AsyncSessionLocal() as db:
            counts = await process_email_queue(db)
        if counts.get("scanned"):
            logger.info("email queue cycle: %s", counts)
    except Exception:
        logger.exception("email queue cycle failed")


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        _run_cycle,
        trigger=IntervalTrigger(minutes=5),
        id="email_queue",
        max_instances=1,
        coalesce=True,
        next_run_time=None,  # first run dopo 5 min; evita burst al boot
    )
    _scheduler.start()
    logger.info("acquisition scheduler started (email_queue every 5 min)")


async def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
    except Exception:
        logger.exception("scheduler shutdown failed")
    _scheduler = None
