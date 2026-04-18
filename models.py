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

    consent_accepted_at = Column(DateTime, nullable=True)
    consent_ip = Column(String(45), nullable=True)
    consent_user_agent = Column(String(500), nullable=True)

    referral_code = Column(String(16), unique=True, index=True, nullable=True)
    referred_by_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    referral_bonus_months_granted = Column(Integer, default=0, nullable=False)

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


# ─── ACQUISITION ENGINE (lead capture + drip + cold outreach + referral) ──────

class Lead(Base):
    """Prospect non ancora registrato. Email è natural key.

    Fonti:
      - demo  : ha provato /demo (high-intent, si aspetta email)
      - cold  : importato via CSV admin (zero intent, soft opt-out)
      - organic: catturato da waitlist/banner (warm, soft opt-in)
    """
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    full_name = Column(String(255), nullable=True)
    company = Column(String(255), nullable=True)
    source = Column(String(32), nullable=False, default="organic")  # demo|cold|organic
    status = Column(String(32), nullable=False, default="new")       # new|engaged|converted|bounced
    unsubscribed = Column(Boolean, default=False, nullable=False)
    unsubscribed_at = Column(DateTime, nullable=True)
    unsub_token = Column(String(64), unique=True, index=True, nullable=False)
    notes = Column(Text, nullable=True)
    demo_input = Column(Text, nullable=True)  # testo che ha generato il demo
    demo_doc_ids = Column(String(255), nullable=True)  # CSV di Document.id generati in demo
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    last_engaged_at = Column(DateTime, nullable=True)


class EmailJob(Base):
    """Un invio schedulato. Può puntare a un Lead o a un User, mai entrambi.

    Pattern invio:
      1. Creazione con sent_at=NULL, scheduled_for=<future>
      2. Worker claim via UPDATE ... WHERE sent_at IS NULL (CAS)
      3. Render + send
      4. Tracking pixel/click aggiornano opened_at / clicked_at via route HMAC-firmate
    """
    __tablename__ = "email_jobs"

    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    campaign_key = Column(String(64), nullable=False, index=True)
    scheduled_for = Column(DateTime, nullable=False, index=True)
    sent_at = Column(DateTime, nullable=True, index=True)
    opened_at = Column(DateTime, nullable=True)
    clicked_at = Column(DateTime, nullable=True)
    error = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    retry_count = Column(Integer, default=0, nullable=False)
    next_retry_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_email_jobs_pending", "scheduled_for", "sent_at"),
        Index("ix_email_jobs_retry", "next_retry_at", "sent_at"),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    action = Column(String(64), nullable=False, index=True)
    detail = Column(Text, nullable=True)
    ip = Column(String(45), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class ReferralClick(Base):
    """Log dei click su /r/{code} prima del signup. Permette analytics anche per
    click che non convertono (p.es. quanti click → quanti signup → quanti paying)."""
    __tablename__ = "referral_clicks"

    id = Column(Integer, primary_key=True, index=True)
    referrer_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    ip = Column(String(45), nullable=True)
    user_agent = Column(String(500), nullable=True)
    referer = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
