"""
Setup Stripe products and prices for AgentIA.
Run once with: python setup_stripe.py
Requires STRIPE_SECRET_KEY in .env
"""
import stripe
import os
from dotenv import load_dotenv

load_dotenv()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
if not stripe.api_key:
    print("ERROR: STRIPE_SECRET_KEY non trovata nel .env")
    exit(1)

# --- Pro Plan ---
pro_product = stripe.Product.create(
    name="AgentIA Pro",
    description="Documenti illimitati per agenti di commercio professionisti",
    metadata={"plan": "pro"},
)

pro_price = stripe.Price.create(
    product=pro_product.id,
    unit_amount=3900,  # €39.00
    currency="eur",
    recurring={"interval": "month"},
    nickname="Pro Monthly",
)

# --- Team Plan ---
team_product = stripe.Product.create(
    name="AgentIA Team",
    description="Fino a 5 agenti per la stessa azienda mandante",
    metadata={"plan": "team"},
)

team_price = stripe.Price.create(
    product=team_product.id,
    unit_amount=8900,  # €89.00
    currency="eur",
    recurring={"interval": "month"},
    nickname="Team Monthly",
)

print("Stripe setup completato.")
print(f"\nAggiungi queste variabili al .env e Railway:")
print(f"STRIPE_PRO_PRICE_ID={pro_price.id}")
print(f"STRIPE_TEAM_PRICE_ID={team_price.id}")
print(f"\nPro Product ID: {pro_product.id}")
print(f"Team Product ID: {team_product.id}")
