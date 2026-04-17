"""
Motore di acquisizione autonoma per VYNEX.

Tre flussi:
  1. Lead capture via /demo       → enroll in SEQUENCE_LEAD_DEMO
  2. Signup utente                → enroll in SEQUENCE_USER_SIGNUP
  3. Cold import admin            → enroll in SEQUENCE_COLD

Una volta iscritto, il lead/user non viene più toccato manualmente: il
processor APScheduler invia ogni job appena scheduled_for è passato.

Tutti gli invii sono idempotenti: sent_at viene valorizzato con CAS
(UPDATE ... WHERE sent_at IS NULL) per evitare doppio invio anche
con più worker.
"""
from __future__ import annotations

import hmac
import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from models import Lead, EmailJob, User, Document
from email_templates import (
    CAMPAIGNS,
    SEQUENCE_LEAD_DEMO,
    SEQUENCE_USER_SIGNUP,
    SEQUENCE_COLD,
    render,
)
from emailer import send_raw

logger = logging.getLogger("vynex.acquisition")

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")

# Max invii batch per processing cycle — Resend free tier è 100/giorno,
# un worker ogni 5 min con batch 20 dà margine ampio senza mai toccare il cap.
MAX_SEND_PER_CYCLE = 20

# Soglia minima tra invii consecutivi allo stesso lead — evita "burst"
# percepiti come spam anche se la sequenza logicamente consente delay_hours=0.
MIN_INTERVAL_PER_RECIPIENT_SEC = 30


# ──────────────────────────────────────────────────────────────────────────────
# HMAC signing per tracking pixel/click (anti-enumeration)
# ──────────────────────────────────────────────────────────────────────────────

def _sig(job_id: int, purpose: str) -> str:
    msg = f"{purpose}:{job_id}".encode()
    return hmac.new(SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()[:16]


def verify_sig(job_id: int, purpose: str, sig: str) -> bool:
    return hmac.compare_digest(_sig(job_id, purpose), sig)


def tracking_pixel_url(job_id: int) -> str:
    return f"{BASE_URL}/e/o/{job_id}/{_sig(job_id, 'open')}.gif"


def tracking_click_url(job_id: int, target: str) -> str:
    from urllib.parse import quote_plus
    return f"{BASE_URL}/e/c/{job_id}/{_sig(job_id, 'click')}?u={quote_plus(target)}"


def unsubscribe_url(token: str) -> str:
    return f"{BASE_URL}/unsubscribe/{token}"


def referral_code() -> str:
    """6 char alphanumerico case-insensitive. ~57M combinazioni, abbastanza per
    milioni di utenti senza collisioni realistiche; l'unique constraint retrya."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I (confusione stampa)
    return "".join(secrets.choice(alphabet) for _ in range(6))


def unsub_token() -> str:
    return secrets.token_urlsafe(24)


# ──────────────────────────────────────────────────────────────────────────────
# Lead lifecycle
# ──────────────────────────────────────────────────────────────────────────────

async def upsert_lead(
    db: AsyncSession,
    email: str,
    full_name: str | None,
    company: str | None,
    source: str,
    notes: str | None = None,
) -> tuple[Lead, bool]:
    """Crea o aggiorna un Lead (email è natural key).

    Returns (lead, created) — created=True se è nuovo.
    """
    email_norm = (email or "").strip().lower()
    if not email_norm:
        raise ValueError("email vuota")

    result = await db.execute(select(Lead).where(Lead.email == email_norm))
    lead = result.scalar_one_or_none()
    created = False
    if lead is None:
        lead = Lead(
            email=email_norm,
            full_name=(full_name or email_norm.split("@")[0]).strip()[:255],
            company=(company or "").strip()[:255] or None,
            source=source,
            status="new",
            unsub_token=unsub_token(),
            notes=(notes or "")[:2000] or None,
        )
        db.add(lead)
        await db.commit()
        await db.refresh(lead)
        created = True
    else:
        # update dati solo se non sono già valorizzati (non sovrascrivere manual curation)
        changed = False
        if not lead.full_name and full_name:
            lead.full_name = full_name.strip()[:255]
            changed = True
        if not lead.company and company:
            lead.company = company.strip()[:255]
            changed = True
        if changed:
            await db.commit()
    return lead, created


async def enroll_lead_in_sequence(
    db: AsyncSession, lead: Lead, sequence: list[str], *, starting_at: datetime | None = None
) -> int:
    """Crea gli EmailJob pendenti per tutta la sequenza. Idempotente per
    (lead_id, campaign_key): se una campaign è già schedulata/inviata salta."""
    if lead.unsubscribed:
        logger.info("lead %s unsubscribed, skip enrollment", lead.email)
        return 0
    start = starting_at or datetime.utcnow()

    existing_q = await db.execute(
        select(EmailJob.campaign_key).where(EmailJob.lead_id == lead.id)
    )
    already = {row[0] for row in existing_q.all()}

    created = 0
    for key in sequence:
        if key in already:
            continue
        c = CAMPAIGNS.get(key)
        if c is None:
            logger.warning("unknown campaign key %r, skip", key)
            continue
        job = EmailJob(
            lead_id=lead.id,
            user_id=None,
            campaign_key=key,
            scheduled_for=start + timedelta(hours=c.delay_hours),
        )
        db.add(job)
        created += 1
    if created:
        await db.commit()
    return created


async def enroll_user_in_sequence(
    db: AsyncSession, user: User, sequence: list[str], *, starting_at: datetime | None = None
) -> int:
    """Gemello di enroll_lead_in_sequence ma agganciato al User (no Lead row)."""
    start = starting_at or datetime.utcnow()
    existing_q = await db.execute(
        select(EmailJob.campaign_key).where(EmailJob.user_id == user.id)
    )
    already = {row[0] for row in existing_q.all()}

    created = 0
    for key in sequence:
        if key in already:
            continue
        c = CAMPAIGNS.get(key)
        if c is None:
            continue
        job = EmailJob(
            lead_id=None,
            user_id=user.id,
            campaign_key=key,
            scheduled_for=start + timedelta(hours=c.delay_hours),
        )
        db.add(job)
        created += 1
    if created:
        await db.commit()
    return created


# ──────────────────────────────────────────────────────────────────────────────
# Queue processor
# ──────────────────────────────────────────────────────────────────────────────

async def process_email_queue(db: AsyncSession) -> dict:
    """Scan jobs pendenti, claim via CAS, invia. Ritorna counters."""
    now = datetime.utcnow()
    counts = {"scanned": 0, "claimed": 0, "sent": 0, "failed": 0, "skipped_unsub": 0}

    q = await db.execute(
        select(EmailJob)
        .where(EmailJob.sent_at.is_(None))
        .where(EmailJob.scheduled_for <= now)
        .order_by(EmailJob.scheduled_for.asc())
        .limit(MAX_SEND_PER_CYCLE)
    )
    jobs = q.scalars().all()
    counts["scanned"] = len(jobs)
    if not jobs:
        return counts

    for job in jobs:
        claim = await db.execute(
            update(EmailJob)
            .where(EmailJob.id == job.id)
            .where(EmailJob.sent_at.is_(None))
            .values(sent_at=now)
            .returning(EmailJob.id)
        )
        if claim.scalar_one_or_none() is None:
            continue
        counts["claimed"] += 1

        to_email = None
        ctx: dict = {}

        if job.lead_id is not None:
            lr = await db.execute(select(Lead).where(Lead.id == job.lead_id))
            lead = lr.scalar_one_or_none()
            if lead is None:
                await db.execute(
                    update(EmailJob).where(EmailJob.id == job.id)
                    .values(error="lead missing", sent_at=None)
                )
                counts["failed"] += 1
                continue
            if lead.unsubscribed:
                await db.execute(
                    update(EmailJob).where(EmailJob.id == job.id).values(error="unsub")
                )
                counts["skipped_unsub"] += 1
                continue
            to_email = lead.email
            ctx = {
                "name": (lead.full_name or "").split(" ")[0] or lead.email.split("@")[0],
                "full_name": lead.full_name or "",
                "company": lead.company or "",
                "unsub_url": unsubscribe_url(lead.unsub_token),
                "demo_url": f"{BASE_URL}/demo",
            }
        elif job.user_id is not None:
            ur = await db.execute(select(User).where(User.id == job.user_id))
            user = ur.scalar_one_or_none()
            if user is None or user.deleted_at is not None:
                await db.execute(
                    update(EmailJob).where(EmailJob.id == job.id)
                    .values(error="user missing or deleted")
                )
                counts["failed"] += 1
                continue
            to_email = user.email
            # conteggio referrals (se la campaign lo usa)
            rc_q = await db.execute(
                select(func.count(User.id))
                .where(User.referred_by_id == user.id)
                .where(User.plan.in_(("pro", "team")))
            )
            ctx = {
                "name": (user.full_name or "").split(" ")[0] or user.email.split("@")[0],
                "full_name": user.full_name or "",
                "unsub_url": f"{BASE_URL}/account",  # per user, unsub = gestione account
                "referrals_count": rc_q.scalar() or 0,
            }
        else:
            counts["failed"] += 1
            continue

        try:
            subject, html = render(job.campaign_key, ctx)
            pixel = f'<img src="{tracking_pixel_url(job.id)}" width="1" height="1" style="display:none">'
            html_with_pixel = html + pixel
            ok = await send_raw(to_email, subject, html_with_pixel)
            if ok:
                counts["sent"] += 1
            else:
                await db.execute(
                    update(EmailJob).where(EmailJob.id == job.id)
                    .values(error="send failed")
                )
                counts["failed"] += 1
        except Exception as exc:
            logger.exception("process_email_queue send failed job=%s", job.id)
            await db.execute(
                update(EmailJob).where(EmailJob.id == job.id)
                .values(error=str(exc)[:500])
            )
            counts["failed"] += 1

    await db.commit()
    return counts


# ──────────────────────────────────────────────────────────────────────────────
# Referral conversions (called on Stripe webhook subscription active)
# ──────────────────────────────────────────────────────────────────────────────

async def post_signup_setup(
    db: AsyncSession, user: User, referral_cookie_value: str | None
) -> None:
    """Post-registrazione: genera referral_code, aggancia referrer se cookie valido,
    iscrive nella drip sequence onboarding.

    Idempotente: se l'utente ha già un referral_code non lo rigenera."""
    changed = False
    if not user.referral_code:
        for _ in range(5):
            code = referral_code()
            exists = await db.execute(
                select(User.id).where(User.referral_code == code)
            )
            if exists.scalar_one_or_none() is None:
                user.referral_code = code
                changed = True
                break

    if (
        referral_cookie_value
        and not user.referred_by_id
    ):
        r = await db.execute(
            select(User).where(User.referral_code == referral_cookie_value)
        )
        referrer = r.scalar_one_or_none()
        if referrer is not None and referrer.id != user.id and referrer.deleted_at is None:
            user.referred_by_id = referrer.id
            changed = True

    if changed:
        await db.commit()

    try:
        await enroll_user_in_sequence(db, user, SEQUENCE_USER_SIGNUP)
    except Exception:
        logger.exception("enroll_user_in_sequence failed user=%s", user.id)


async def on_user_converted_to_paid(db: AsyncSession, user: User) -> None:
    """Chiamato quando user diventa paying. Se era stato referral-ato, notifica
    il referrer e lo accredita di un mese gratis (Stripe coupon auto)."""
    if not user.referred_by_id:
        return
    r = await db.execute(select(User).where(User.id == user.referred_by_id))
    referrer = r.scalar_one_or_none()
    if referrer is None or referrer.deleted_at is not None:
        return

    # conta conversioni per il referrer (non include self)
    rc_q = await db.execute(
        select(func.count(User.id))
        .where(User.referred_by_id == referrer.id)
        .where(User.plan.in_(("pro", "team")))
    )
    conversions = rc_q.scalar() or 0

    # enroll notifica — scheduled_for=now così parte nel prossimo ciclo processor
    job = EmailJob(
        lead_id=None,
        user_id=referrer.id,
        campaign_key="referral_converted",
        scheduled_for=datetime.utcnow(),
    )
    db.add(job)
    await db.commit()

    # ogni 2 conversioni → 1 mese bonus via Stripe coupon applicato alla sub
    if conversions > 0 and conversions % 2 == 0:
        try:
            from stripe_handler import apply_referral_bonus_month
            await apply_referral_bonus_month(referrer)
        except Exception:
            logger.exception("referral bonus apply failed user=%s", referrer.id)
