"""
Maintenance jobs — pulizia automatica + riconciliazione Stripe.

Tutti i job sono idempotenti e safe da eseguire in qualsiasi momento.
Agganciati allo scheduler APScheduler in scheduler.py.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from models import (
    EmailVerificationToken, Document, User, EmailJob, AuditLog, Lead,
)

logger = logging.getLogger("vynex.maintenance")


async def cleanup_expired_tokens(db: AsyncSession) -> int:
    cutoff = datetime.utcnow() - timedelta(hours=2)
    result = await db.execute(
        delete(EmailVerificationToken)
        .where(EmailVerificationToken.expires_at < cutoff)
    )
    count = result.rowcount
    if count:
        await db.commit()
        logger.info("cleaned %d expired verification tokens", count)
    return count


async def purge_soft_deleted_documents(db: AsyncSession, older_than_days: int = 30) -> int:
    cutoff = datetime.utcnow() - timedelta(days=older_than_days)
    result = await db.execute(
        delete(Document)
        .where(Document.deleted_at.is_not(None))
        .where(Document.deleted_at < cutoff)
    )
    count = result.rowcount
    if count:
        await db.commit()
        logger.info("purged %d soft-deleted documents (older than %d days)", count, older_than_days)
    return count


async def cleanup_dead_email_jobs(db: AsyncSession) -> int:
    cutoff = datetime.utcnow() - timedelta(days=90)
    result = await db.execute(
        delete(EmailJob)
        .where(EmailJob.sent_at.is_not(None))
        .where(EmailJob.created_at < cutoff)
        .where(EmailJob.opened_at.is_(None))
        .where(EmailJob.clicked_at.is_(None))
    )
    count = result.rowcount
    if count:
        await db.commit()
        logger.info("cleaned %d old sent-but-unopened email jobs (>90 days)", count)
    return count


async def reconcile_stripe_subscriptions(db: AsyncSession) -> dict:
    """Verifica che lo stato locale dei paganti corrisponda a Stripe."""
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe.api_key:
        return {"skipped": "no stripe key"}

    counts = {"checked": 0, "fixed": 0, "errors": 0}

    result = await db.execute(
        select(User).where(
            User.stripe_subscription_id.is_not(None),
            User.deleted_at.is_(None),
        )
    )
    users = result.scalars().all()
    counts["checked"] = len(users)

    for user in users:
        try:
            sub = await stripe.Subscription.retrieve_async(user.stripe_subscription_id)
            stripe_status = sub.get("status", "")
            stripe_plan = None
            try:
                price_id = sub["items"]["data"][0]["price"]["id"]
                pro_price = os.getenv("STRIPE_PRO_PRICE_ID", "")
                team_price = os.getenv("STRIPE_TEAM_PRICE_ID", "")
                if price_id == pro_price:
                    stripe_plan = "pro"
                elif price_id == team_price:
                    stripe_plan = "team"
            except (KeyError, IndexError):
                pass

            changed = False
            if user.subscription_status != stripe_status:
                logger.warning(
                    "stripe reconcile user=%s: status %s -> %s",
                    user.id, user.subscription_status, stripe_status,
                )
                user.subscription_status = stripe_status
                changed = True

            if stripe_status in ("canceled", "incomplete_expired") and user.plan != "free":
                user.plan = "free"
                user.stripe_subscription_id = None
                changed = True

            if stripe_status in ("active", "trialing") and stripe_plan and user.plan != stripe_plan:
                user.plan = stripe_plan
                changed = True

            period_end_ts = sub.get("current_period_end")
            if period_end_ts:
                period_end = datetime.utcfromtimestamp(int(period_end_ts))
                if user.subscription_current_period_end != period_end:
                    user.subscription_current_period_end = period_end
                    changed = True

            if changed:
                counts["fixed"] += 1
                db.add(AuditLog(
                    user_id=user.id,
                    action="stripe_reconcile",
                    detail=f"status={stripe_status} plan={stripe_plan}",
                ))

        except Exception:
            logger.exception("stripe reconcile failed user=%s", user.id)
            counts["errors"] += 1

    if counts["fixed"]:
        await db.commit()
    logger.info("stripe reconciliation: %s", counts)
    return counts


async def cleanup_old_audit_logs(db: AsyncSession, older_than_days: int = 180) -> int:
    cutoff = datetime.utcnow() - timedelta(days=older_than_days)
    result = await db.execute(
        delete(AuditLog)
        .where(AuditLog.created_at < cutoff)
    )
    count = result.rowcount
    if count:
        await db.commit()
        logger.info("purged %d audit logs older than %d days", count, older_than_days)
    return count


async def cleanup_stale_leads(db: AsyncSession, older_than_days: int = 365) -> int:
    cutoff = datetime.utcnow() - timedelta(days=older_than_days)
    result = await db.execute(
        delete(Lead)
        .where(Lead.unsubscribed == True)
        .where(Lead.created_at < cutoff)
    )
    count = result.rowcount
    if count:
        await db.commit()
        logger.info("purged %d unsubscribed leads older than %d days", count, older_than_days)
    return count


async def run_all_maintenance(db: AsyncSession) -> dict:
    results = {}
    try:
        results["tokens_cleaned"] = await cleanup_expired_tokens(db)
    except Exception:
        logger.exception("cleanup_expired_tokens failed")
    try:
        results["docs_purged"] = await purge_soft_deleted_documents(db)
    except Exception:
        logger.exception("purge_soft_deleted_documents failed")
    try:
        results["dead_jobs_cleaned"] = await cleanup_dead_email_jobs(db)
    except Exception:
        logger.exception("cleanup_dead_email_jobs failed")
    try:
        results["audit_logs_purged"] = await cleanup_old_audit_logs(db)
    except Exception:
        logger.exception("cleanup_old_audit_logs failed")
    try:
        results["stale_leads_purged"] = await cleanup_stale_leads(db)
    except Exception:
        logger.exception("cleanup_stale_leads failed")
    return results
