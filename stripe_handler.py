import os
import logging
import stripe
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from models import User, StripeEventLog
from emailer import send_payment_success_email, send_payment_failed_email

logger = logging.getLogger("vynex.stripe")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

PRO_PRICE_ID = os.getenv("STRIPE_PRO_PRICE_ID", "")
TEAM_PRICE_ID = os.getenv("STRIPE_TEAM_PRICE_ID", "")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")


async def create_checkout_session(user: User, plan: str) -> str:
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

    session = await stripe.checkout.Session.create_async(
        customer=customer_id,
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        success_url=f"{BASE_URL}/dashboard?upgrade=success",
        cancel_url=f"{BASE_URL}/prezzi?upgrade=cancelled",
        metadata={"user_id": str(user.id), "plan": plan},
        locale="it",
    )
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
            await db.commit()

    elif event_type == "customer.subscription.updated":
        subscription = event["data"]["object"]
        customer_id = subscription["customer"]
        status_str = subscription["status"]

        result = await db.execute(
            select(User).where(User.stripe_customer_id == customer_id)
        )
        user = result.scalar_one_or_none()
        if user and status_str == "active":
            price_id = subscription["items"]["data"][0]["price"]["id"]
            if price_id == PRO_PRICE_ID:
                user.plan = "pro"
            elif price_id == TEAM_PRICE_ID:
                user.plan = "team"
            await db.commit()

    elif event_type == "invoice.payment_failed":
        invoice = event["data"]["object"]
        customer_id = invoice.get("customer")
        if customer_id:
            result = await db.execute(
                select(User).where(User.stripe_customer_id == customer_id)
            )
            user = result.scalar_one_or_none()
            if user:
                try:
                    await send_payment_failed_email(user.email, user.full_name)
                except Exception:
                    logger.exception("payment failed email failed")
