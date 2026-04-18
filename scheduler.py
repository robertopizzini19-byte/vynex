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


async def _run_nps_invites() -> None:
    """Giornaliero: enroll NPS invite per utenti a 7gg e 30gg post-signup."""
    try:
        from datetime import datetime, timedelta
        from sqlalchemy import select
        from models import User, EmailJob
        async with AsyncSessionLocal() as db:
            now = datetime.utcnow()
            counts = {"t7": 0, "t30": 0}
            for tag, delta in [("t7", 7), ("t30", 30)]:
                window_start = now - timedelta(days=delta, hours=12)
                window_end = now - timedelta(days=delta)
                cq = await db.execute(
                    select(User)
                    .where(User.created_at >= window_start)
                    .where(User.created_at <= window_end)
                    .where(User.deleted_at.is_(None))
                    .where(User.is_active.is_(True))
                )
                users = cq.scalars().all()
                campaign = f"user_nps_{tag}"
                for u in users:
                    exq = await db.execute(
                        select(EmailJob.id)
                        .where(EmailJob.user_id == u.id)
                        .where(EmailJob.campaign_key == campaign)
                        .limit(1)
                    )
                    if exq.scalar_one_or_none() is not None:
                        continue
                    db.add(EmailJob(
                        user_id=u.id, lead_id=None,
                        campaign_key=campaign, scheduled_for=now,
                    ))
                    counts[tag] += 1
                if counts[tag]:
                    await db.commit()
            if counts["t7"] or counts["t30"]:
                logger.info("NPS invites enrolled: %s", counts)
    except Exception:
        logger.exception("nps_invites cycle failed")


async def _run_churn_winback() -> None:
    """Giornaliero: enroll win-back per Pro/Team inattivi >21gg, cooldown 60gg."""
    try:
        from datetime import datetime, timedelta
        from sqlalchemy import select
        from models import User, EmailJob
        async with AsyncSessionLocal() as db:
            now = datetime.utcnow()
            inactive_cutoff = now - timedelta(days=21)
            winback_cooldown = now - timedelta(days=60)
            uq = await db.execute(
                select(User)
                .where(User.plan.in_(("pro", "team")))
                .where(User.deleted_at.is_(None))
                .where(User.is_active.is_(True))
                .where((User.last_activity_at.is_(None)) | (User.last_activity_at < inactive_cutoff))
            )
            users = uq.scalars().all()
            enrolled = 0
            for u in users:
                exq = await db.execute(
                    select(EmailJob.id)
                    .where(EmailJob.user_id == u.id)
                    .where(EmailJob.campaign_key == "user_winback")
                    .where(EmailJob.created_at >= winback_cooldown)
                    .limit(1)
                )
                if exq.scalar_one_or_none() is not None:
                    continue
                db.add(EmailJob(
                    user_id=u.id, lead_id=None,
                    campaign_key="user_winback", scheduled_for=now,
                ))
                enrolled += 1
            if enrolled:
                await db.commit()
                logger.info("churn winback enrolled: %d users", enrolled)
    except Exception:
        logger.exception("churn_winback cycle failed")


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

    _scheduler.add_job(
        _run_nps_invites,
        trigger=IntervalTrigger(hours=24),
        id="nps_invites",
        max_instances=1,
        coalesce=True,
        next_run_time=None,
    )

    _scheduler.add_job(
        _run_churn_winback,
        trigger=IntervalTrigger(hours=24),
        id="churn_winback",
        max_instances=1,
        coalesce=True,
        next_run_time=None,
    )

    _scheduler.start()
    logger.info(
        "scheduler started: email_queue/5min, maintenance/6h, "
        "stripe_reconcile/12h, nps_invites/24h, churn_winback/24h"
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
