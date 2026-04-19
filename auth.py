from datetime import datetime, timedelta
from typing import Optional, Tuple
from jose import JWTError, jwt
import bcrypt
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
import os
import re
import secrets

from database import get_db
from models import User, EmailVerificationToken

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-cambia-in-produzione")
if os.getenv("BASE_URL", "").startswith("https://") and SECRET_KEY == "dev-secret-key-cambia-in-produzione":
    raise RuntimeError("SECRET_KEY non impostata in produzione — set SECRET_KEY env var")
if len(SECRET_KEY) < 32:
    raise RuntimeError("SECRET_KEY troppo corta — usa almeno 32 byte casuali")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30
PASSWORD_RESET_EXPIRE_MINUTES = 60
EMAIL_VERIFY_EXPIRE_HOURS = 48
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
IDLE_TIMEOUT_DAYS = 14

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login", auto_error=False)


def _bcrypt_safe(password: str) -> bytes:
    # bcrypt rifiuta >72 byte (passlib in passato troncava silenziosamente).
    # Tronchiamo per restare compatibili con hash storici generati da passlib.
    return password.encode("utf-8")[:72]


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_bcrypt_safe(plain), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_bcrypt_safe(password), bcrypt.gensalt()).decode("utf-8")


def validate_password_strength(password: str) -> Tuple[bool, str]:
    if len(password) < 8:
        return False, "La password deve avere almeno 8 caratteri."
    if len(password) > 128:
        return False, "La password è troppo lunga (max 128 caratteri)."
    if not re.search(r"[a-zA-Z]", password):
        return False, "La password deve contenere almeno una lettera."
    if not re.search(r"\d", password):
        return False, "La password deve contenere almeno un numero."
    weak = {"password", "12345678", "qwerty12", "password1", "abcd1234", "letmein1"}
    if password.lower() in weak:
        return False, "Password troppo comune. Scegline una diversa."
    return True, ""


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None, token_version: int = 0) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS))
    to_encode.update({"exp": expire, "purpose": "access", "tv": token_version})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_password_reset_token(email: str, token_version: int = 0) -> str:
    """Binds reset token to user's token_version at creation time.

    After the password is reset we bump token_version, so the same reset
    token can never be replayed — tv no longer matches.
    """
    expire = datetime.utcnow() + timedelta(minutes=PASSWORD_RESET_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": email, "exp": expire, "purpose": "reset", "tv": token_version},
        SECRET_KEY, algorithm=ALGORITHM
    )


def verify_password_reset_token(token: str) -> Optional[Tuple[str, int]]:
    """Returns (email, token_version_at_issue) or None if invalid."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("purpose") != "reset":
            return None
        email = payload.get("sub")
        if not email:
            return None
        return email, int(payload.get("tv", 0))
    except (JWTError, ValueError, TypeError):
        return None


def generate_email_verification_token() -> str:
    return secrets.token_urlsafe(32)


async def create_email_verification_token(db: AsyncSession, user: User) -> str:
    now = datetime.utcnow()
    await db.execute(
        EmailVerificationToken.__table__.update()
        .where(
            EmailVerificationToken.user_id == user.id,
            EmailVerificationToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    token = generate_email_verification_token()
    record = EmailVerificationToken(
        user_id=user.id,
        token=token,
        expires_at=now + timedelta(hours=EMAIL_VERIFY_EXPIRE_HOURS),
    )
    db.add(record)
    await db.commit()
    return token


async def consume_email_verification_token(db: AsyncSession, token: str) -> Optional[User]:
    result = await db.execute(
        select(EmailVerificationToken).where(EmailVerificationToken.token == token)
    )
    record = result.scalar_one_or_none()
    if not record or record.used_at or record.expires_at < datetime.utcnow():
        return None
    user_result = await db.execute(select(User).where(User.id == record.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        return None
    record.used_at = datetime.utcnow()
    user.email_verified = True
    user.email_verified_at = datetime.utcnow()
    await db.commit()
    return user


async def get_user_by_email(
    db: AsyncSession, email: str, include_deleted: bool = False
) -> Optional[User]:
    stmt = select(User).where(User.email == email.lower())
    if not include_deleted:
        stmt = stmt.where(User.deleted_at.is_(None))
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def authenticate_user(db: AsyncSession, email: str, password: str) -> Tuple[Optional[User], str]:
    """Returns (user, reason). reason is one of: ok, not_found, wrong_password, locked, deleted, inactive.

    Uses a DB-side atomic UPDATE to increment failed_login_attempts so
    concurrent wrong-password requests can't race past MAX_FAILED_ATTEMPTS.
    """
    user = await get_user_by_email(db, email, include_deleted=True)
    if not user:
        return None, "not_found"
    if user.deleted_at is not None:
        return None, "deleted"
    if not user.is_active:
        return None, "inactive"
    if user.is_locked:
        return None, "locked"
    if not verify_password(password, user.hashed_password):
        now = datetime.utcnow()
        locked_until_value = now + timedelta(minutes=LOCKOUT_MINUTES)
        await db.execute(
            update(User)
            .where(User.id == user.id)
            .values(
                failed_login_attempts=User.failed_login_attempts + 1,
            )
        )
        await db.commit()
        await db.refresh(user)
        if (user.failed_login_attempts or 0) >= MAX_FAILED_ATTEMPTS:
            await db.execute(
                update(User)
                .where(User.id == user.id)
                .values(
                    failed_login_attempts=0,
                    locked_until=locked_until_value,
                )
            )
            await db.commit()
        return None, "wrong_password"
    now = datetime.utcnow()
    await db.execute(
        update(User)
        .where(User.id == user.id)
        .values(
            failed_login_attempts=0,
            locked_until=None,
            last_login_at=now,
            last_activity_at=now,
        )
    )
    await db.commit()
    await db.refresh(user)
    return user, "ok"


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db)
) -> Optional[User]:
    """Gets current user from cookie OR Authorization header."""
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

    if not token:
        return None

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("purpose") != "access":
            return None
        email: str = payload.get("sub")
        if not email:
            return None
        token_tv = int(payload.get("tv", 0))
    except (JWTError, ValueError, TypeError):
        return None

    user = await get_user_by_email(db, email)
    if not user:
        return None
    if (user.token_version or 0) != token_tv:
        return None
    if user.deleted_at is not None:
        return None
    if not user.is_active:
        return None
    if user.last_activity_at is not None:
        idle = datetime.utcnow() - user.last_activity_at
        if idle > timedelta(days=IDLE_TIMEOUT_DAYS):
            return None
    return user


async def require_user(
    request: Request,
    db: AsyncSession = Depends(get_db)
) -> User:
    user = await get_current_user(request, db)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Non autenticato"
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account disattivato"
        )
    await db.execute(
        update(User).where(User.id == user.id).values(last_activity_at=datetime.utcnow())
    )
    await db.commit()
    return user
