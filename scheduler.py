"""
Scheduler per VYNEX — 3 job autonomi.

1. email_queue    (5 min)  — processa coda invii drip/transazionali
2. maintenance    (6 ore)  — pulizia token/docs/jobs scaduti
3. stripe_reconcile (12 ore) — sincronizza stato abbonamenti con Stripe

APScheduler async in-process. Si avvia nel lifespan di FastAPI.
Un solo worker (Railway Hobby = 1 replica): non serve lock distribuito.
"""
from __future__ import annotations

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database import AsyncSessionLocal
from acquisition import process_email_queue

logger = logging.getLogger("vynex.scheduler")

_scheduler: AsyncIOScheduler | None = None


async def _run_email_cycle() -> None:
    try:
        async with AsyncSessionLocal() as db:
            counts = await process_email_queue(db)
        if counts.get("scanned"):
            logger.info("email queue cycle: %s", counts)
    except Exception:
        logger.exception("email queue cycle failed")


async def _run_maintenance() -> None:
    try:
        from maintenance import run_all_maintenance
        async with AsyncSessionLocal() as db:
            results = await run_all_maintenance(db)
        logger.info("maintenance cycle: %s", results)
    except Exception:
        logger.exception("maintenance cycle failed")


async def _run_stripe_reconcile() -> None:
    try:
        from maintenance import reconcile_stripe_subscriptions
        async with AsyncSessionLocal() as db:
            results = await reconcile_stripe_subscriptions(db)
        logger.info("stripe reconcile cycle: %s", results)
    except Exception:
        logger.exception("stripe reconcile cycle failed")


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler(timezone="UTC")

    _scheduler.add_job(
        _run_email_cycle,
        trigger=IntervalTrigger(minutes=5),
        id="email_queue",
        max_instances=1,
        coalesce=True,
        next_run_time=None,
    )

    _scheduler.add_job(
        _run_maintenance,
        trigger=IntervalTrigger(hours=6),
        id="maintenance",
        max_instances=1,
        coalesce=True,
        next_run_time=None,
    )

    _scheduler.add_job(
        _run_stripe_reconcile,
        trigger=IntervalTrigger(hours=12),
        id="stripe_reconcile",
        max_instances=1,
        coalesce=True,
        next_run_time=None,
    )

    _scheduler.start()
    logger.info(
        "scheduler started: email_queue/5min, maintenance/6h, stripe_reconcile/12h"
    )


async def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=True)
    except Exception:
        logger.exception("scheduler shutdown failed")
    _scheduler = None
