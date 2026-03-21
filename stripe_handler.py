import stripe
import os
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models import User

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

PRO_PRICE_ID = os.getenv("STRIPE_PRO_PRICE_ID", "")
TEAM_PRICE_ID = os.getenv("STRIPE_TEAM_PRICE_ID", "")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")


async def create_checkout_session(user: User, plan: str) -> str:
    """Crea una Stripe Checkout Session e ritorna l'URL."""
    price_id = PRO_PRICE_ID if plan == "pro" else TEAM_PRICE_ID

    # Crea o recupera il customer Stripe
    if not user.stripe_customer_id:
        customer = stripe.Customer.create(
            email=user.email,
            name=user.full_name,
            metadata={"user_id": str(user.id)}
        )
        customer_id = customer.id
    else:
        customer_id = user.stripe_customer_id

    session = stripe.checkout.Session.create(
        customer=customer_id,
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        success_url=f"{BASE_URL}/dashboard?upgrade=success",
        cancel_url=f"{BASE_URL}/prezzi?upgrade=cancelled",
        metadata={"user_id": str(user.id), "plan": plan},
        subscription_data={
            "trial_period_days": 0,
        },
        locale="it",
    )
    return session.url


async def create_portal_session(user: User) -> str:
    """Crea un Stripe Customer Portal per gestire/cancellare abbonamento."""
    session = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=f"{BASE_URL}/dashboard",
    )
    return session.url


async def handle_webhook(payload: bytes, sig_header: str, db: AsyncSession):
    """Processa webhook Stripe e aggiorna il piano utente."""
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except stripe.error.SignatureVerificationError:
        raise ValueError("Invalid webhook signature")

    if event["type"] == "checkout.session.completed":
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

    elif event["type"] in ("customer.subscription.deleted", "customer.subscription.paused"):
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

    elif event["type"] == "customer.subscription.updated":
        subscription = event["data"]["object"]
        customer_id = subscription["customer"]
        status = subscription["status"]

        result = await db.execute(
            select(User).where(User.stripe_customer_id == customer_id)
        )
        user = result.scalar_one_or_none()
        if user and status == "active":
            # Determina il piano dal price_id
            price_id = subscription["items"]["data"][0]["price"]["id"]
            if price_id == PRO_PRICE_ID:
                user.plan = "pro"
            elif price_id == TEAM_PRICE_ID:
                user.plan = "team"
            await db.commit()
