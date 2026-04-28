"""Resend webhook handler — bounce + complaint signaling.

Bloccare i Lead bouncing prima di consumare il quotient di reputazione
del dominio è cruciale: >2% bounce → Resend ban → drip funnel morto.

Eventi gestiti:
  - email.bounced     → Lead.status = "bounced" + stop pending EmailJob
  - email.complained  → Lead.unsubscribed = True + stop pending EmailJob
  - email.delivery_delayed → log only (Resend ritenta da solo)
  - email.delivered / email.opened / email.clicked → ignored (tracciato altrove)

Verifica firma Svix (formato canonico Resend 2025):
  signed_payload = f"{svix_id}.{svix_timestamp}.{body}"
  expected = base64(HMAC_SHA256(secret_bytes, signed_payload))
  header svix-signature può contenere più firme separate da spazio.

Replay protection: timestamp entro RESEND_WEBHOOK_TOLERANCE_SECONDS.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import EmailJob, Lead

logger = logging.getLogger("vynex.resend")

RESEND_WEBHOOK_TOLERANCE_SECONDS = 5 * 60


def _verify_signature(
    secret: str,
    svix_id: str,
    svix_timestamp: str,
    body: bytes,
    signature_header: str,
) -> bool:
    if not (secret and svix_id and svix_timestamp and signature_header):
        return False

    # Replay window: timestamp recente.
    try:
        ts = int(svix_timestamp)
    except ValueError:
        return False
    now = int(datetime.now(tz=timezone.utc).timestamp())
    if abs(now - ts) > RESEND_WEBHOOK_TOLERANCE_SECONDS:
        return False

    raw_secret = secret
    if raw_secret.startswith("whsec_"):
        raw_secret = raw_secret[len("whsec_") :]
    try:
        secret_bytes = base64.b64decode(raw_secret)
    except Exception:
        return False

    signed_payload = f"{svix_id}.{svix_timestamp}.".encode() + body
    digest = hmac.new(secret_bytes, signed_payload, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()

    # Header: "v1,sig1 v1,sig2" — confronta tutte le versioni v1.
    for sig in signature_header.split(" "):
        if "," not in sig:
            continue
        version, payload_sig = sig.split(",", 1)
        if version == "v1" and hmac.compare_digest(expected, payload_sig):
            return True
    return False


async def _stop_pending_jobs_for_lead(db: AsyncSession, lead_id: int) -> int:
    """Marca i job pending come 'failed' con error='bounced' / 'complained'.

    Non li cancella per preservare audit trail. Restituisce numero righe toccate.
    """
    result = await db.execute(
        update(EmailJob)
        .where(EmailJob.lead_id == lead_id, EmailJob.sent_at.is_(None))
        .values(
            sent_at=datetime.utcnow(),  # claim per esclusione futura
            error="suppressed:bounce_or_complaint",
        )
    )
    return result.rowcount or 0


async def _handle_bounced(db: AsyncSession, payload: dict) -> None:
    data = payload.get("data") or {}
    to = data.get("to")
    if isinstance(to, list):
        emails = [e for e in to if isinstance(e, str)]
    elif isinstance(to, str):
        emails = [to]
    else:
        emails = []

    bounce_type = ((data.get("bounce") or {}).get("type") or "").lower()
    # Soft bounce = transitorio (mailbox piena). Resend ritenta. Non sopprimo.
    if bounce_type == "soft":
        logger.info("Soft bounce for %s — keeping lead active", emails)
        return

    for email in emails:
        if not email:
            continue
        result = await db.execute(select(Lead).where(Lead.email == email))
        lead = result.scalar_one_or_none()
        if lead is None:
            logger.info("Bounce for unknown email %s — no lead row", email)
            continue
        if lead.status != "bounced":
            lead.status = "bounced"
            stopped = await _stop_pending_jobs_for_lead(db, lead.id)
            logger.warning(
                "Lead %s (%s) bounced (type=%s) — %d pending jobs suppressed",
                lead.id, email, bounce_type or "hard", stopped,
            )
    await db.commit()


async def _handle_complained(db: AsyncSession, payload: dict) -> None:
    data = payload.get("data") or {}
    to = data.get("to")
    if isinstance(to, list):
        emails = [e for e in to if isinstance(e, str)]
    elif isinstance(to, str):
        emails = [to]
    else:
        emails = []

    for email in emails:
        if not email:
            continue
        result = await db.execute(select(Lead).where(Lead.email == email))
        lead = result.scalar_one_or_none()
        if lead is None:
            logger.info("Complaint for unknown email %s — no lead row", email)
            continue
        if not lead.unsubscribed:
            lead.unsubscribed = True
            lead.unsubscribed_at = datetime.utcnow()
            lead.status = "bounced"  # tratto il complaint come terminale come il bounce
            stopped = await _stop_pending_jobs_for_lead(db, lead.id)
            logger.warning(
                "Lead %s (%s) complained — %d pending jobs suppressed",
                lead.id, email, stopped,
            )
    await db.commit()


async def handle_webhook(
    body: bytes,
    svix_id: str,
    svix_timestamp: str,
    svix_signature: str,
    db: AsyncSession,
) -> str:
    """Punto d'ingresso. Restituisce il tipo evento processato (per debug)."""
    secret = os.getenv("RESEND_WEBHOOK_SECRET", "")
    if not secret:
        logger.error("RESEND_WEBHOOK_SECRET not configured, rejecting webhook")
        raise ValueError("Webhook secret not configured")

    if not _verify_signature(secret, svix_id, svix_timestamp, body, svix_signature):
        raise ValueError("Invalid Svix signature")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid payload: {exc}") from exc

    event_type = payload.get("type") or ""

    if event_type == "email.bounced":
        await _handle_bounced(db, payload)
    elif event_type == "email.complained":
        await _handle_complained(db, payload)
    elif event_type == "email.delivery_delayed":
        # Non sopprimo — Resend ritenta. Solo log per visibility.
        logger.info("Delivery delayed event ignored: %s", payload.get("data", {}).get("to"))
    else:
        # delivered/opened/clicked/sent — già tracciati da pixel/click route.
        logger.debug("Resend event ignored: %s", event_type)

    return event_type
