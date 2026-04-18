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

from models import Lead, EmailJob, User, Document, LeadSource
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
MAX_SEND_PER_CYCLE = int(os.getenv("EMAIL_BATCH_SIZE", "20"))

MAX_RETRY = 3
RETRY_BACKOFF_MINUTES = [15, 60, 240]

# Soglia minima tra invii consecutivi allo stesso lead — evita "burst"
# percepiti come spam anche se la sequenza logicamente consente delay_hours=0.
MIN_INTERVAL_PER_RECIPIENT_SEC = 30


# ──────────────────────────────────────────────────────────────────────────────
# HMAC signing per tracking pixel/click (anti-enumeration)
# ──────────────────────────────────────────────────────────────────────────────

def _sig(job_id: int, purpose: str, extra: str = "") -> str:
    msg = f"{purpose}:{job_id}:{extra}".encode()
    return hmac.new(SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()[:32]


def verify_sig(job_id: int, purpose: str, sig: str, extra: str = "") -> bool:
    return hmac.compare_digest(_sig(job_id, purpose, extra), sig)


def tracking_pixel_url(job_id: int) -> str:
    return f"{BASE_URL}/e/o/{job_id}/{_sig(job_id, 'open')}.gif"


def tracking_click_url(job_id: int, target: str) -> str:
    from urllib.parse import quote_plus
    sig = _sig(job_id, "click", target)
    return f"{BASE_URL}/e/c/{job_id}/{sig}?u={quote_plus(target)}"


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

async def save_source_attribution(
    db: AsyncSession,
    *,
    lead_id: int | None = None,
    user_id: int | None = None,
    utm_cookie: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Crea una riga LeadSource se il cookie UTM ha qualcosa. Idempotent: se gia'
    esiste una riga con stessa lead_id/user_id non crea duplicato."""
    if not utm_cookie:
        return
    try:
        import json as _j
        data = _j.loads(utm_cookie)
        if not isinstance(data, dict):
            return
    except Exception:
        return

    if not any(data.get(k) for k in ("utm_source", "utm_campaign", "first_referer")):
        return

    if lead_id is not None:
        existing = await db.execute(
            select(LeadSource.id).where(LeadSource.lead_id == lead_id).limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            return
    if user_id is not None:
        existing = await db.execute(
            select(LeadSource.id).where(LeadSource.user_id == user_id).limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            return

    row = LeadSource(
        lead_id=lead_id,
        user_id=user_id,
        utm_source=(data.get("utm_source") or "")[:120] or None,
        utm_medium=(data.get("utm_medium") or "")[:120] or None,
        utm_campaign=(data.get("utm_campaign") or "")[:120] or None,
        utm_term=(data.get("utm_term") or "")[:120] or None,
        utm_content=(data.get("utm_content") or "")[:120] or None,
        first_referer=(data.get("first_referer") or "")[:500] or None,
        first_landing=(data.get("first_landing") or "")[:500] or None,
        ip=ip,
        user_agent=user_agent,
    )
    db.add(row)
    await db.commit()


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

_PROVIDER_SUSPENDED_MARKERS = (
    "not yet activated",
    "permission_denied",
    "account is not activated",
    "smtp account",
)


def _is_provider_suspended(error_msg: str) -> bool:
    """True se l'errore indica che il provider (Brevo/Resend) ha sospeso l'account
    globalmente, non un problema specifico del job. In quel caso non consumiamo
    MAX_RETRY inutilmente: basta aspettare che l'admin attivi il provider."""
    m = (error_msg or "").lower()
    return any(marker in m for marker in _PROVIDER_SUSPENDED_MARKERS)


async def _schedule_retry(
    db: AsyncSession, job_id: int, retry_count: int, error_msg: str
) -> bool:
    # Se il provider e' sospeso, non incrementare retry_count (non spreca i 3 tentativi)
    # e schedula un soft retry lungo (1h) — al prossimo cycle dopo activation ripartono.
    if _is_provider_suspended(error_msg):
        next_at = datetime.utcnow() + timedelta(hours=1)
        await db.execute(
            update(EmailJob).where(EmailJob.id == job_id)
            .values(
                sent_at=None,
                next_retry_at=next_at,
                error=f"PROVIDER_SUSPENDED: {error_msg}"[:500],
            )
        )
        logger.warning("provider suspended, soft retry in 1h job=%s", job_id)
        return True

    if retry_count >= MAX_RETRY:
        await db.execute(
            update(EmailJob).where(EmailJob.id == job_id)
            .values(error=f"MAX_RETRY: {error_msg}"[:500], sent_at=None)
        )
        return False
    backoff_min = RETRY_BACKOFF_MINUTES[min(retry_count, len(RETRY_BACKOFF_MINUTES) - 1)]
    next_at = datetime.utcnow() + timedelta(minutes=backoff_min)
    await db.execute(
        update(EmailJob).where(EmailJob.id == job_id)
        .values(
            sent_at=None,
            retry_count=retry_count + 1,
            next_retry_at=next_at,
            error=error_msg[:500],
        )
    )
    logger.info("retry scheduled job=%s attempt=%d next_at=%s", job_id, retry_count + 1, next_at)
    return True


async def reset_all_retries(db: AsyncSession) -> int:
    """Reset tutti i job non ancora spediti: azzera retry_count, next_retry_at, error.
    Usato quando l'admin attiva il provider email e vuole riprocessare subito tutto."""
    result = await db.execute(
        update(EmailJob)
        .where(EmailJob.sent_at.is_(None))
        .values(retry_count=0, next_retry_at=None, error=None)
    )
    await db.commit()
    count = result.rowcount or 0
    logger.info("reset_all_retries: %d jobs unlocked", count)
    return count


async def process_email_queue(db: AsyncSession) -> dict:
    """Scan jobs pendenti, claim via CAS, invia. Ritorna counters."""
    now = datetime.utcnow()
    counts = {"scanned": 0, "claimed": 0, "sent": 0, "failed": 0, "skipped_unsub": 0, "retried": 0}

    from sqlalchemy import or_
    q = await db.execute(
        select(EmailJob)
        .where(EmailJob.sent_at.is_(None))
        .where(or_(
            EmailJob.scheduled_for <= now,
            (EmailJob.next_retry_at.is_not(None)) & (EmailJob.next_retry_at <= now),
        ))
        .where(or_(
            EmailJob.retry_count < MAX_RETRY,
            EmailJob.retry_count.is_(None),
        ))
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
                await db.execute(
                    update(EmailJob).where(EmailJob.id == job.id)
                    .values(error=None, next_retry_at=None)
                )
            else:
                retried = await _schedule_retry(db, job.id, job.retry_count or 0, "send failed")
                counts["retried" if retried else "failed"] += 1
        except Exception as exc:
            logger.exception("process_email_queue send failed job=%s", job.id)
            retried = await _schedule_retry(db, job.id, job.retry_count or 0, str(exc)[:500])
            counts["retried" if retried else "failed"] += 1

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

    try:
        await migrate_demo_to_account(db, user)
    except Exception:
        logger.exception("migrate_demo_to_account failed user=%s", user.id)


async def migrate_demo_to_account(db: AsyncSession, user: User) -> int:
    """Se esiste un Lead con stessa email che ha provato /demo, crea i 3 Document
    nell'account appena registrato. Cosi l'utente, al primo accesso in dashboard,
    ritrova subito i documenti che aveva generato in demo. Idempotente: se esiste
    gia' un doc con stesso input_text per questo user, salta.
    """
    import json as _json_mod
    lr = await db.execute(select(Lead).where(Lead.email == user.email))
    lead = lr.scalar_one_or_none()
    if lead is None or not lead.demo_input:
        return 0

    try:
        payload = _json_mod.loads(lead.demo_input)
    except Exception:
        logger.exception("migrate_demo: invalid demo_input lead=%s", lead.id)
        return 0

    input_text = (payload.get("input") or "")[:2000]
    if not input_text:
        return 0

    existing = await db.execute(
        select(Document.id)
        .where(Document.user_id == user.id)
        .where(Document.input_text == input_text)
        .limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        return 0

    doc = Document(
        user_id=user.id,
        input_text=input_text,
        report_visita=payload.get("report_visita") or "",
        email_followup=payload.get("email_followup") or "",
        offerta_commerciale=payload.get("offerta_commerciale") or "",
        cliente_nome=(payload.get("cliente_nome") or "")[:255] or None,
        azienda_cliente=(payload.get("azienda_cliente") or "")[:255] or None,
    )
    db.add(doc)
    if lead.status != "converted":
        lead.status = "converted"
    await db.commit()
    logger.info("migrated demo lead=%s to user=%s as document", lead.id, user.id)
    return 1


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
