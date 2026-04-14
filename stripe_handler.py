import os
import logging
from datetime import datetime, timedelta
import stripe
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from models import User, StripeEventLog, Coupon, CouponRedemption
from emailer import (
    send_payment_success_email,
    send_payment_failed_email,
    send_subscription_past_due_email,
)

logger = logging.getLogger("vynex.stripe")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

PRO_PRICE_ID = os.getenv("STRIPE_PRO_PRICE_ID", "")
TEAM_PRICE_ID = os.getenv("STRIPE_TEAM_PRICE_ID", "")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")

GRACE_PERIOD_DAYS = 7


async def create_checkout_session(user: User, plan: str, coupon_code: str | None = None) -> str:
    price_id = PRO_PRICE_ID if plan == "pro" else TEAM_PRICE_ID

    if not user.stripe_customer_id:
        customer = await stripe.Customer.create_async(
            email=user.email,
            name=user.full_name,
            metadata={"user_id": str(user.id)}
        )
        customer_id = customer.id
    else:
        customer_id = user.stripe_customer_id

    create_kwargs = dict(
        customer=customer_id,
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        success_url=f"{BASE_URL}/dashboard?upgrade=success",
        cancel_url=f"{BASE_URL}/prezzi?upgrade=cancelled",
        metadata={"user_id": str(user.id), "plan": plan},
        locale="it",
        allow_promotion_codes=True,
    )

    if coupon_code:
        create_kwargs["discounts"] = [{"coupon": coupon_code}]
        create_kwargs.pop("allow_promotion_codes", None)

    session = await stripe.checkout.Session.create_async(**create_kwargs)
    return session.url


async def create_portal_session(user: User) -> str:
    session = await stripe.billing_portal.Session.create_async(
        customer=user.stripe_customer_id,
        return_url=f"{BASE_URL}/dashboard",
    )
    return session.url


async def _record_event(db: AsyncSession, event_id: str, event_type: str) -> bool:
    """Insert event into log. Returns True if new, False if already processed."""
    log_entry = StripeEventLog(event_id=event_id, event_type=event_type)
    db.add(log_entry)
    try:
        await db.commit()
        return True
    except IntegrityError:
        await db.rollback()
        return False


def _ts(value) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.utcfromtimestamp(int(value))
    except Exception:
        return None


def _plan_from_price(price_id: str) -> str | None:
    if price_id == PRO_PRICE_ID:
        return "pro"
    if price_id == TEAM_PRICE_ID:
        return "team"
    return None


async def handle_webhook(payload: bytes, sig_header: str, db: AsyncSession):
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except stripe.error.SignatureVerificationError:
        raise ValueError("Invalid webhook signature")

    event_id = event["id"]
    event_type = event["type"]

    is_new = await _record_event(db, event_id, event_type)
    if not is_new:
        logger.info("Stripe event %s already processed, skipping", event_id)
        return

    try:
        await _dispatch_event(event_type, event, db)
    except Exception:
        logger.exception("Stripe event %s (%s) handler failed", event_id, event_type)
        # Re-raise so Stripe retries; we already logged the event so duplicate
        # delivery will be filtered, but the second delivery still re-runs the handler.
        raise


async def _dispatch_event(event_type: str, event: dict, db: AsyncSession):
    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = int(session["metadata"]["user_id"])
        plan = session["metadata"]["plan"]
        subscription_id = session.get("subscription")
        customer_id = session.get("customer")

        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user:
            user.plan = plan
            user.stripe_subscription_id = subscription_id
            user.stripe_customer_id = customer_id
            user.subscription_status = "active"
            await db.commit()
            try:
                await send_payment_success_email(user.email, user.full_name, plan)
            except Exception:
                logger.exception("payment success email failed")

    elif event_type in ("customer.subscription.deleted", "customer.subscription.paused"):
        subscription = event["data"]["object"]
        customer_id = subscription["customer"]

        result = await db.execute(
            select(User).where(User.stripe_customer_id == customer_id)
        )
        user = result.scalar_one_or_none()
        if user:
            user.plan = "free"
            user.stripe_subscription_id = None
            user.subscription_status = "canceled"
            user.subscription_current_period_end = None
            await db.commit()

    elif event_type == "customer.subscription.updated":
        subscription = event["data"]["object"]
        customer_id = subscription["customer"]
        status_str = subscription["status"]
        period_end = _ts(subscription.get("current_period_end"))

        result = await db.execute(
            select(User).where(User.stripe_customer_id == customer_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            return

        user.subscription_status = status_str
        user.subscription_current_period_end = period_end

        if status_str in ("active", "trialing"):
            try:
                price_id = subscription["items"]["data"][0]["price"]["id"]
                new_plan = _plan_from_price(price_id)
                if new_plan:
                    user.plan = new_plan
            except (KeyError, IndexError):
                logger.warning("subscription.updated missing price for customer %s", customer_id)
        elif status_str in ("canceled", "incomplete_expired"):
            user.plan = "free"
            user.stripe_subscription_id = None

        await db.commit()

    elif event_type == "invoice.payment_failed":
        invoice = event["data"]["object"]
        customer_id = invoice.get("customer")
        if not customer_id:
            return
        result = await db.execute(
            select(User).where(User.stripe_customer_id == customer_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            return

        user.subscription_status = "past_due"
        if not user.subscription_current_period_end:
            user.subscription_current_period_end = datetime.utcnow() + timedelta(days=GRACE_PERIOD_DAYS)
        await db.commit()

        try:
            days_left = max(
                0,
                (user.subscription_current_period_end - datetime.utcnow()).days
            ) if user.subscription_current_period_end else GRACE_PERIOD_DAYS
            await send_subscription_past_due_email(user.email, user.full_name, days_left)
        except Exception:
            logger.exception("past_due email failed")
        try:
            await send_payment_failed_email(user.email, user.full_name)
        except Exception:
            logger.exception("payment failed email failed")

    elif event_type == "invoice.payment_succeeded":
        invoice = event["data"]["object"]
        customer_id = invoice.get("customer")
        if not customer_id:
            return
        result = await db.execute(
            select(User).where(User.stripe_customer_id == customer_id)
        )
        user = result.scalar_one_or_none()
        if user and user.subscription_status == "past_due":
            user.subscription_status = "active"
            await db.commit()


async def apply_coupon(db: AsyncSession, user: User, code: str) -> tuple[bool, str]:
    """Validates a local coupon code, records redemption, returns (ok, message)."""
    code_norm = (code or "").strip().upper()
    if not code_norm:
        return False, "Codice mancante."

    result = await db.execute(select(Coupon).where(Coupon.code == code_norm))
    coupon = result.scalar_one_or_none()
    if not coupon or not coupon.is_active:
        return False, "Codice non valido."
    if coupon.valid_until and coupon.valid_until < datetime.utcnow():
        return False, "Codice scaduto."
    if coupon.max_redemptions and coupon.redemptions_count >= coupon.max_redemptions:
        return False, "Codice esaurito."

    existing = await db.execute(
        select(CouponRedemption).where(
            CouponRedemption.coupon_id == coupon.id,
            CouponRedemption.user_id == user.id,
        )
    )
    if existing.scalar_one_or_none():
        return False, "Hai già usato questo codice."

    db.add(CouponRedemption(coupon_id=coupon.id, user_id=user.id))
    coupon.redemptions_count = (coupon.redemptions_count or 0) + 1
    await db.commit()
    return True, coupon.stripe_coupon_id or coupon.code
