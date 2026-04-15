from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from fastapi import Request
from fastapi.responses import JSONResponse
from jose import jwt, JWTError
import os

ALGORITHM = "HS256"
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-cambia-in-produzione")


def _user_or_ip(request: Request) -> str:
    """Prefer authenticated user as rate-limit key, fallback to IP.

    Validates expiration so an attacker can't replay a long-expired JWT
    to escape IP-based rate limiting.
    """
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            if payload.get("purpose") == "access":
                email = payload.get("sub")
                if email:
                    return f"user:{email}"
        except JWTError:
            pass
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=_user_or_ip, default_limits=[])


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"error": "Troppe richieste. Riprova tra qualche secondo."},
    )
