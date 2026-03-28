from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, ForeignKey, Enum
from sqlalchemy.orm import relationship, backref
from datetime import datetime
import enum
from database import Base


class PlanType(str, enum.Enum):
    free = "free"
    pro = "pro"
    enterprise = "enterprise"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=False)
    company_name = Column(String(255), nullable=True)
    plan = Column(String(20), default=PlanType.free, nullable=False)
    stripe_customer_id = Column(String(255), nullable=True)
    stripe_subscription_id = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    team_owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    documents = relationship("Document", back_populates="user")
    team_members = relationship(
        "User",
        foreign_keys=[team_owner_id],
        backref=backref("team_owner", remote_side="User.id")
    )

    @property
    def monthly_limit(self) -> int:
        return {"free": 5, "pro": 99999, "team": 99999, "enterprise": 99999}.get(self.plan, 5)


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    input_text = Column(Text, nullable=False)
    report_visita = Column(Text, nullable=True)
    email_followup = Column(Text, nullable=True)
    offerta_commerciale = Column(Text, nullable=True)
    cliente_nome = Column(String(255), nullable=True)
    azienda_cliente = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="documents")
