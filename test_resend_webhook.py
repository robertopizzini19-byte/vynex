"""Unit tests for resend_webhook.py — bounce + complaint handling.

Esegue con: python test_resend_webhook.py
(no pytest, stdlib-only + SQLAlchemy + asyncio)

Cosa verifica:
1. Signature missing/invalid → ValueError (rifiuto sicuro)
2. Replay window: timestamp -10min → ValueError
3. Hard bounce: Lead.status -> "bounced" + EmailJob suppressed
4. Soft bounce: Lead resta attivo (transitorio)
5. Complaint: Lead.unsubscribed=True + EmailJob suppressed
6. Email sconosciuta: no-op safe
7. Eventi non gestiti (delivered/opened): pass-through

Test isolati: ogni test usa un DB SQLite in-memory fresh.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

# Setup env BEFORE importing app modules
TEST_SECRET = "whsec_" + base64.b64encode(b"test-secret-32-bytes-long-x-y-z!").decode()
os.environ["RESEND_WEBHOOK_SECRET"] = TEST_SECRET
os.environ["SECRET_KEY"] = "test-secret-not-for-prod"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["BASE_URL"] = "http://localhost"

sys.path.insert(0, str(Path(__file__).parent))

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from datetime import datetime

from database import Base
from models import Lead, EmailJob
import resend_webhook


# ─── helpers ─────────────────────────────────────────────────────────────────

def sign(body: bytes, svix_id: str, svix_ts: str, secret: str = TEST_SECRET) -> str:
    raw = secret[len("whsec_"):]
    sb = base64.b64decode(raw)
    signed = f"{svix_id}.{svix_ts}.".encode() + body
    digest = hmac.new(sb, signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def make_payload(event_type: str, email: str, bounce_type: str = "hard") -> bytes:
    payload = {
        "type": event_type,
        "data": {
            "email_id": "test-id",
            "to": [email],
            "from": "noreply@example.invalid",
            "subject": "Test",
        },
    }
    if event_type == "email.bounced":
        payload["data"]["bounce"] = {"type": bounce_type}
    return json.dumps(payload, separators=(",", ":")).encode()


async def fresh_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    return engine, Session


async def insert_lead(session, email: str, status: str = "new", **kw) -> Lead:
    lead = Lead(
        email=email,
        status=status,
        unsub_token=f"tok-{email[:8]}",
        **kw,
    )
    session.add(lead)
    await session.commit()
    await session.refresh(lead)
    return lead


async def insert_pending_job(session, lead_id: int) -> EmailJob:
    job = EmailJob(
        lead_id=lead_id,
        campaign_key="drip-day-1",
        scheduled_for=datetime.utcnow(),
        sent_at=None,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


# ─── test runners ────────────────────────────────────────────────────────────

PASSED = 0
FAILED = 0


def fail(name: str, msg: str):
    global FAILED
    FAILED += 1
    print(f"  [FAIL] {name}: {msg}")


def ok(name: str):
    global PASSED
    PASSED += 1
    print(f"  [ OK ] {name}")


async def run_test(name, coro):
    try:
        await coro
        ok(name)
    except AssertionError as e:
        fail(name, str(e))
    except Exception as e:
        fail(name, f"unexpected {type(e).__name__}: {e}")


# ─── tests ───────────────────────────────────────────────────────────────────

async def test_signature_missing():
    engine, Session = await fresh_db()
    async with Session() as db:
        try:
            await resend_webhook.handle_webhook(b"{}", "", "", "", db)
            assert False, "doveva sollevare ValueError"
        except ValueError as e:
            assert "signature" in str(e).lower() or "secret" in str(e).lower()


async def test_signature_invalid():
    engine, Session = await fresh_db()
    async with Session() as db:
        body = make_payload("email.bounced", "x@example.com")
        ts = str(int(time.time()))
        try:
            await resend_webhook.handle_webhook(body, "msg_x", ts, "v1,deadbeef", db)
            assert False, "doveva sollevare ValueError per firma errata"
        except ValueError:
            pass  # expected


async def test_replay_window():
    engine, Session = await fresh_db()
    async with Session() as db:
        body = make_payload("email.bounced", "x@example.com")
        old_ts = str(int(time.time()) - 10 * 60)
        sig = sign(body, "msg_x", old_ts)
        try:
            await resend_webhook.handle_webhook(body, "msg_x", old_ts, sig, db)
            assert False, "doveva rigettare timestamp vecchio"
        except ValueError:
            pass  # expected


async def test_hard_bounce_marks_lead_and_suppresses_job():
    engine, Session = await fresh_db()
    async with Session() as db:
        lead = await insert_lead(db, "bouncing@example.com", status="engaged")
        job = await insert_pending_job(db, lead.id)
        assert job.sent_at is None

        body = make_payload("email.bounced", "bouncing@example.com", "hard")
        ts = str(int(time.time()))
        sig = sign(body, "msg_b", ts)
        await resend_webhook.handle_webhook(body, "msg_b", ts, sig, db)

        await db.refresh(lead)
        await db.refresh(job)
        assert lead.status == "bounced", f"expected status=bounced, got {lead.status}"
        assert job.sent_at is not None, "EmailJob doveva essere suppresso"
        assert job.error == "suppressed:bounce_or_complaint"


async def test_soft_bounce_keeps_lead_active():
    engine, Session = await fresh_db()
    async with Session() as db:
        lead = await insert_lead(db, "softbounce@example.com", status="engaged")
        body = make_payload("email.bounced", "softbounce@example.com", "soft")
        ts = str(int(time.time()))
        sig = sign(body, "msg_s", ts)
        await resend_webhook.handle_webhook(body, "msg_s", ts, sig, db)

        await db.refresh(lead)
        assert lead.status == "engaged", f"soft bounce non doveva cambiare status, got {lead.status}"


async def test_complaint_unsubscribes():
    engine, Session = await fresh_db()
    async with Session() as db:
        lead = await insert_lead(db, "complainer@example.com", status="engaged")
        job = await insert_pending_job(db, lead.id)

        body = make_payload("email.complained", "complainer@example.com")
        ts = str(int(time.time()))
        sig = sign(body, "msg_c", ts)
        await resend_webhook.handle_webhook(body, "msg_c", ts, sig, db)

        await db.refresh(lead)
        await db.refresh(job)
        assert lead.unsubscribed is True
        assert lead.status == "bounced"
        assert job.sent_at is not None


async def test_unknown_email_noop():
    engine, Session = await fresh_db()
    async with Session() as db:
        body = make_payload("email.bounced", "ghost@example.com")
        ts = str(int(time.time()))
        sig = sign(body, "msg_g", ts)
        # Non deve sollevare nulla
        event = await resend_webhook.handle_webhook(body, "msg_g", ts, sig, db)
        assert event == "email.bounced"


async def test_delivered_passthrough():
    engine, Session = await fresh_db()
    async with Session() as db:
        lead = await insert_lead(db, "happy@example.com", status="engaged")
        body = json.dumps({"type": "email.delivered", "data": {"to": ["happy@example.com"]}}, separators=(",", ":")).encode()
        ts = str(int(time.time()))
        sig = sign(body, "msg_d", ts)
        event = await resend_webhook.handle_webhook(body, "msg_d", ts, sig, db)
        assert event == "email.delivered"
        await db.refresh(lead)
        assert lead.status == "engaged"  # invariato


async def test_idempotent_double_bounce():
    engine, Session = await fresh_db()
    async with Session() as db:
        lead = await insert_lead(db, "double@example.com", status="bounced")  # già bounced
        body = make_payload("email.bounced", "double@example.com", "hard")
        ts = str(int(time.time()))
        sig = sign(body, "msg_dup", ts)
        # Non deve fallire né cambiare nulla di significativo
        event = await resend_webhook.handle_webhook(body, "msg_dup", ts, sig, db)
        assert event == "email.bounced"
        await db.refresh(lead)
        assert lead.status == "bounced"


# ─── runner ──────────────────────────────────────────────────────────────────

async def main():
    print("\nRunning resend_webhook tests...\n")
    await run_test("signature_missing rejects", test_signature_missing())
    await run_test("signature_invalid rejects", test_signature_invalid())
    await run_test("replay_window rejects old timestamp", test_replay_window())
    await run_test("hard bounce marks lead + suppresses job", test_hard_bounce_marks_lead_and_suppresses_job())
    await run_test("soft bounce keeps lead active", test_soft_bounce_keeps_lead_active())
    await run_test("complaint unsubscribes lead", test_complaint_unsubscribes())
    await run_test("unknown email is no-op safe", test_unknown_email_noop())
    await run_test("delivered event pass-through", test_delivered_passthrough())
    await run_test("double bounce idempotent", test_idempotent_double_bounce())

    print(f"\n{'='*50}")
    print(f"PASSED: {PASSED}  FAILED: {FAILED}")
    print(f"{'='*50}\n")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
