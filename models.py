from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, ForeignKey, Index
from sqlalchemy.orm import relationship, backref
from datetime import datetime
import enum
from database import Base


class PlanType(str, enum.Enum):
    free = "free"
    pro = "pro"
    team = "team"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=False)
    company_name = Column(String(255), nullable=True)
    plan = Column(String(20), default=PlanType.free, nullable=False)
    stripe_customer_id = Column(String(255), nullable=True, index=True)
    stripe_subscription_id = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    team_owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    email_verified = Column(Boolean, default=False, nullable=False)
    email_verified_at = Column(DateTime, nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    last_activity_at = Column(DateTime, nullable=True)
    failed_login_attempts = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime, nullable=True)
    deleted_at = Column(DateTime, nullable=True)
    subscription_status = Column(String(30), nullable=True)
    subscription_current_period_end = Column(DateTime, nullable=True)
    token_version = Column(Integer, default=0, nullable=False)

    documents = relationship("Document", back_populates="user")
    team_members = relationship(
        "User",
        foreign_keys=[team_owner_id],
        backref=backref("team_owner", remote_side="User.id")
    )

    @property
    def monthly_limit(self) -> int:
        return {"free": 10, "pro": 99999, "team": 99999}.get(self.plan, 10)

    @property
    def is_locked(self) -> bool:
        return self.locked_until is not None and self.locked_until > datetime.utcnow()

    @property
    def in_grace_period(self) -> bool:
        return self.subscription_status in ("past_due", "unpaid")


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    input_text = Column(Text, nullable=False)
    report_visita = Column(Text, nullable=True)
    email_followup = Column(Text, nullable=True)
    offerta_commerciale = Column(Text, nullable=True)
    cliente_nome = Column(String(255), nullable=True)
    azienda_cliente = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    deleted_at = Column(DateTime, nullable=True)
    tokens_used = Column(Integer, nullable=True)
    generation_time_ms = Column(Integer, nullable=True)

    user = relationship("User", back_populates="documents")


class StripeEventLog(Base):
    __tablename__ = "stripe_events"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String(255), unique=True, index=True, nullable=False)
    event_type = Column(String(100), nullable=False)
    processed_at = Column(DateTime, default=datetime.utcnow)


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token = Column(String(255), unique=True, index=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Coupon(Base):
    __tablename__ = "coupons"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, index=True, nullable=False)
    description = Column(String(255), nullable=True)
    discount_percent = Column(Integer, nullable=True)
    discount_amount_cents = Column(Integer, nullable=True)
    max_redemptions = Column(Integer, nullable=True)
    redemptions_count = Column(Integer, default=0, nullable=False)
    valid_until = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    stripe_coupon_id = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CouponRedemption(Base):
    __tablename__ = "coupon_redemptions"

    id = Column(Integer, primary_key=True, index=True)
    coupon_id = Column(Integer, ForeignKey("coupons.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    redeemed_at = Column(DateTime, default=datetime.utcnow)
