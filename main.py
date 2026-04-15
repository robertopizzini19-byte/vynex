from fastapi import FastAPI, Request, Depends, Form, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, update
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
import asyncio
import hmac
import logging
import os
import io

from dotenv import load_dotenv
load_dotenv()

from email_validator import validate_email, EmailNotValidError

from database import get_db, init_db, AsyncSessionLocal
from models import User, Document
from auth import (
    hash_password, verify_password, authenticate_user, create_access_token,
    get_current_user, require_user, get_user_by_email,
    create_password_reset_token, verify_password_reset_token,
    validate_password_strength, create_email_verification_token,
    consume_email_verification_token,
)
from ai_engine import genera_documenti, rigenera_documento
from stripe_handler import (
    create_checkout_session, create_portal_session, handle_webhook, apply_coupon,
)
from rate_limit import limiter, rate_limit_exceeded_handler, RateLimitExceeded
from emailer import (
    send_welcome_email, send_password_reset_email, send_verification_email,
)
from logging_setup import configure_logging, RequestIdMiddleware, user_id_var
from oauth_google import (
    is_enabled as google_oauth_enabled,
    client_id as google_client_id,
    verify_credential as verify_google_credential,
)

configure_logging(os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("vynex")


def _validate_prod_env() -> None:
    """Refuse to boot if production env is missing critical secrets.

    Better to crash at startup than to serve traffic with silently
    disabled payments, email, or AI generation.
    """
    is_prod = os.getenv("BASE_URL", "").startswith("https://")
    if not is_prod:
        return
    required = [
        "SECRET_KEY",
        "DATABASE_URL",
        "ANTHROPIC_API_KEY",
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "ADMIN_TOKEN",
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise RuntimeError(
            f"Env vars mancanti in produzione: {', '.join(missing)}"
        )
    # RESEND_API_KEY non e' fatal: l'emailer e' gia' graceful no-op.
    # Logga forte per ricordarsi di settarla prima del lancio.
    if not os.getenv("RESEND_API_KEY"):
        import logging
        logging.getLogger("vynex.main").warning(
            "RESEND_API_KEY non settata — email transazionali disabilitate"
        )
    if "sqlite" in os.getenv("DATABASE_URL", ""):
        raise RuntimeError("DATABASE_URL deve puntare a Postgres in produzione")
    if len(os.getenv("ADMIN_TOKEN", "")) < 32:
        raise RuntimeError("ADMIN_TOKEN troppo corto — usa almeno 32 byte casuali")


_validate_prod_env()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="VYNEX", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "script-src 'self' 'unsafe-inline' https://unpkg.com https://js.stripe.com https://accounts.google.com; "
            "connect-src 'self' https://api.stripe.com https://accounts.google.com; "
            "frame-src https://js.stripe.com https://hooks.stripe.com https://accounts.google.com; "
            "base-uri 'self'; form-action 'self' https://checkout.stripe.com; "
            "frame-ancestors 'none'"
        )
        return response


_CSRF_EXEMPT_PATHS = ("/webhook/stripe", "/auth/google/verify")
_CSRF_SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "TRACE")


class OriginCSRFMiddleware(BaseHTTPMiddleware):
    """Reject state-changing requests whose Origin/Referer doesn't match BASE_URL.

    Belt-and-suspenders on top of SameSite=Lax cookies. Prevents classic
    CSRF even if a future browser relaxes SameSite semantics or if a
    subdomain is compromised.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method in _CSRF_SAFE_METHODS:
            return await call_next(request)
        path = request.url.path
        if any(path.startswith(p) for p in _CSRF_EXEMPT_PATHS):
            return await call_next(request)
        base_url = os.getenv("BASE_URL", "").rstrip("/")
        if not base_url or not base_url.startswith("https://"):
            return await call_next(request)
        origin = request.headers.get("origin", "")
        referer = request.headers.get("referer", "")
        if origin and origin.rstrip("/") == base_url:
            return await call_next(request)
        if referer and referer.startswith(base_url + "/"):
            return await call_next(request)
        logger.warning(
            "CSRF block: %s %s origin=%r referer=%r",
            request.method, path, origin, referer,
        )
        return JSONResponse(
            {"error": "Origine richiesta non valida"}, status_code=403
        )


app.add_middleware(OriginCSRFMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIdMiddleware)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ─── HELPERS ──────────────────────────────────────────────────────────────────

_COOKIE_SECURE = os.getenv("BASE_URL", "").startswith("https://")


def redirect_with_cookie(url: str, token: str) -> RedirectResponse:
    response = RedirectResponse(url=url, status_code=302)
    response.set_cookie(
        "access_token", token,
        httponly=True, secure=_COOKIE_SECURE, samesite="lax",
        max_age=60 * 60 * 24 * 30  # 30 giorni
    )
    return response


async def get_monthly_usage(db: AsyncSession, user_id: int) -> int:
    start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(func.count(Document.id))
        .where(Document.user_id == user_id)
        .where(Document.created_at >= start)
        .where(Document.deleted_at.is_(None))
    )
    return result.scalar() or 0


# ─── PAGINE PUBBLICHE ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/prezzi", response_class=HTMLResponse)
async def prezzi(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    return templates.TemplateResponse("prezzi.html", {"request": request, "user": user})


@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse("privacy.html", {"request": request})


@app.get("/termini", response_class=HTMLResponse)
async def termini(request: Request):
    return templates.TemplateResponse("termini.html", {"request": request})


@app.get("/cookie", response_class=HTMLResponse)
async def cookie_policy(request: Request):
    return templates.TemplateResponse("cookie.html", {"request": request})


@app.get("/chi-siamo", response_class=HTMLResponse)
async def chi_siamo(request: Request):
    return templates.TemplateResponse("chi_siamo.html", {"request": request})


@app.get("/come-funziona", response_class=HTMLResponse)
async def come_funziona(request: Request):
    return templates.TemplateResponse("come_funziona.html", {"request": request})


def _google_ctx() -> dict:
    return {
        "oauth_google": google_oauth_enabled(),
        "google_client_id": google_client_id(),
        "base_url": os.getenv("BASE_URL", "http://localhost:8000").rstrip("/"),
    }


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    error = request.query_params.get("error", "")
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": error, **_google_ctx()},
    )


@app.get("/registrati", response_class=HTMLResponse)
async def register_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    error = request.query_params.get("error", "")
    return templates.TemplateResponse(
        "register.html",
        {"request": request, "error": error, **_google_ctx()},
    )


@app.post("/auth/google/verify")
@limiter.limit("20/minute")
async def auth_google_verify(request: Request, db: AsyncSession = Depends(get_db)):
    """Google Identity Services callback — riceve un JWT ID token dal widget GIS.

    GIS fa POST form-encoded con:
      - credential: il JWT ID token firmato da Google
      - g_csrf_token: token CSRF (deve matchare il cookie g_csrf_token, double-submit)
    """
    if not google_oauth_enabled():
        raise HTTPException(503, "Google login non configurato")

    form = await request.form()
    credential = form.get("credential")
    csrf_body = form.get("g_csrf_token")
    csrf_cookie = request.cookies.get("g_csrf_token")

    if not credential:
        return RedirectResponse("/login?error=Token+Google+mancante", status_code=303)
    if not csrf_body or csrf_body != csrf_cookie:
        logger.warning("Google verify CSRF mismatch")
        return RedirectResponse("/login?error=CSRF+non+valido", status_code=303)

    idinfo = verify_google_credential(credential)
    if not idinfo:
        return RedirectResponse("/login?error=Token+Google+non+valido", status_code=303)

    email = (idinfo.get("email") or "").lower().strip()
    name = idinfo.get("name") or (email.split("@")[0] if email else "")
    email_verified = bool(idinfo.get("email_verified", False))

    if not email:
        return RedirectResponse("/login?error=Email+Google+mancante", status_code=303)

    existing = await get_user_by_email(db, email)
    if existing:
        if existing.deleted_at is not None:
            return RedirectResponse("/login?error=Account+eliminato", status_code=303)
        if not existing.is_active:
            return RedirectResponse("/login?error=Account+disattivato", status_code=303)
        existing.last_login_at = datetime.utcnow()
        existing.last_activity_at = datetime.utcnow()
        existing.failed_login_attempts = 0
        existing.locked_until = None
        if email_verified and not existing.email_verified:
            existing.email_verified = True
            existing.email_verified_at = datetime.utcnow()
        await db.commit()
        access = create_access_token(
            {"sub": existing.email},
            token_version=existing.token_version or 0,
        )
        return redirect_with_cookie("/dashboard", access)

    import secrets as _secrets
    random_pw = _secrets.token_urlsafe(32)
    new_user = User(
        email=email,
        hashed_password=hash_password(random_pw),
        full_name=name or email.split("@")[0],
        company_name=None,
        plan="free",
        email_verified=email_verified,
        email_verified_at=datetime.utcnow() if email_verified else None,
        last_login_at=datetime.utcnow(),
        last_activity_at=datetime.utcnow(),
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    try:
        await send_welcome_email(new_user.email, new_user.full_name)
    except Exception:
        logger.exception("welcome email (google signup) failed")

    access = create_access_token({"sub": new_user.email}, token_version=0)
    return redirect_with_cookie("/dashboard", access)


@app.get("/recupera-password", response_class=HTMLResponse)
async def forgot_page(request: Request):
    message = request.query_params.get("message", "")
    error = request.query_params.get("error", "")
    return templates.TemplateResponse("forgot_password.html", {
        "request": request, "message": message, "error": error
    })


@app.get("/reset-password", response_class=HTMLResponse)
async def reset_page(request: Request):
    token = request.query_params.get("token", "")
    if not token:
        return RedirectResponse("/recupera-password?error=Link+non+valido", status_code=302)
    if verify_password_reset_token(token) is None:
        return RedirectResponse("/recupera-password?error=Link+scaduto+o+non+valido", status_code=302)
    return templates.TemplateResponse("reset_password.html", {"request": request, "token": token})


# ─── AUTH ─────────────────────────────────────────────────────────────────────

@app.post("/api/login")
@limiter.limit("20/minute")
async def api_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    user, reason = await authenticate_user(db, email, password)
    if not user:
        if reason == "locked":
            return RedirectResponse("/login?error=Account+temporaneamente+bloccato.+Riprova+tra+15+minuti", status_code=302)
        if reason == "deleted":
            return RedirectResponse("/login?error=Account+eliminato", status_code=302)
        if reason == "inactive":
            return RedirectResponse("/login?error=Account+disattivato", status_code=302)
        return RedirectResponse("/login?error=Email+o+password+non+corretti", status_code=302)
    token = create_access_token({"sub": user.email}, token_version=user.token_version or 0)
    return redirect_with_cookie("/dashboard", token)


@app.post("/api/registrati")
@limiter.limit("5/minute")
async def api_register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(...),
    company_name: str = Form(""),
    accept_terms: str = Form(""),
    db: AsyncSession = Depends(get_db)
):
    if accept_terms != "on":
        return RedirectResponse("/registrati?error=Devi+accettare+termini+e+privacy", status_code=302)

    try:
        valid = validate_email(email, check_deliverability=False)
        email_norm = valid.normalized.lower()
    except EmailNotValidError:
        return RedirectResponse("/registrati?error=Email+non+valida", status_code=302)

    existing = await get_user_by_email(db, email_norm)
    if existing:
        return RedirectResponse("/registrati?error=Email+già+registrata", status_code=302)

    ok, msg = validate_password_strength(password)
    if not ok:
        from urllib.parse import quote_plus
        return RedirectResponse(f"/registrati?error={quote_plus(msg)}", status_code=302)

    if not full_name.strip() or len(full_name.strip()) < 2:
        return RedirectResponse("/registrati?error=Nome+non+valido", status_code=302)

    client_ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "")
    )[:45]
    user_agent = request.headers.get("user-agent", "")[:500]
    user = User(
        email=email_norm,
        hashed_password=hash_password(password),
        full_name=full_name.strip(),
        company_name=company_name.strip() or None,
        plan="free",
        consent_accepted_at=datetime.utcnow(),
        consent_ip=client_ip,
        consent_user_agent=user_agent,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    try:
        await send_welcome_email(user.email, user.full_name)
    except Exception:
        logger.exception("welcome email failed")

    try:
        verify_token = await create_email_verification_token(db, user)
        base_url = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
        verify_link = f"{base_url}/verifica-email?token={verify_token}"
        await send_verification_email(user.email, user.full_name, verify_link)
    except Exception:
        logger.exception("verification email failed")

    token = create_access_token({"sub": user.email}, token_version=user.token_version or 0)
    return redirect_with_cookie("/dashboard", token)


@app.post("/api/recupera-password")
@limiter.limit("5/minute")
async def api_forgot(
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    user = await get_user_by_email(db, email)
    if user:
        token = create_password_reset_token(user.email, token_version=user.token_version or 0)
        base_url = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
        reset_link = f"{base_url}/reset-password?token={token}"
        try:
            await send_password_reset_email(user.email, user.full_name, reset_link)
        except Exception:
            logger.exception("reset email failed")
    return RedirectResponse(
        "/recupera-password?message=Se+l%27email+esiste%2C+ti+abbiamo+inviato+il+link+di+reset",
        status_code=302
    )


@app.post("/api/reset-password")
@limiter.limit("5/minute")
async def api_reset(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    parsed = verify_password_reset_token(token)
    if parsed is None:
        return RedirectResponse("/recupera-password?error=Link+scaduto+o+non+valido", status_code=302)
    email, token_tv = parsed
    ok, msg = validate_password_strength(password)
    if not ok:
        from urllib.parse import quote_plus
        return RedirectResponse(f"/reset-password?token={token}&error={quote_plus(msg)}", status_code=302)
    user = await get_user_by_email(db, email)
    if not user:
        return RedirectResponse("/recupera-password?error=Link+scaduto+o+non+valido", status_code=302)
    # Single-use enforcement: after first reset we bump token_version,
    # so a replayed token can't match anymore.
    if (user.token_version or 0) != token_tv:
        return RedirectResponse("/recupera-password?error=Link+già+utilizzato+o+scaduto", status_code=302)
    user.hashed_password = hash_password(password)
    user.token_version = (user.token_version or 0) + 1
    user.failed_login_attempts = 0
    user.locked_until = None
    await db.commit()
    return RedirectResponse("/login?error=Password+aggiornata%2C+accedi+con+la+nuova", status_code=302)


@app.get("/logout")
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if user:
        await db.execute(
            update(User)
            .where(User.id == user.id)
            .values(token_version=(user.token_version or 0) + 1)
        )
        await db.commit()
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie("access_token")
    return response


@app.get("/verifica-email", response_class=HTMLResponse)
async def verifica_email_page(request: Request, db: AsyncSession = Depends(get_db)):
    token = request.query_params.get("token", "")
    if not token:
        return templates.TemplateResponse(
            "verifica_email.html",
            {"request": request, "ok": False, "error": "Token mancante."},
        )
    user = await consume_email_verification_token(db, token)
    if not user:
        return templates.TemplateResponse(
            "verifica_email.html",
            {"request": request, "ok": False, "error": "Token non valido o scaduto."},
        )
    return templates.TemplateResponse(
        "verifica_email.html",
        {"request": request, "ok": True, "error": ""},
    )


@app.post("/api/rinvia-verifica")
@limiter.limit("3/hour")
async def api_rinvia_verifica(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    if user.email_verified:
        return JSONResponse({"status": "already_verified"})
    try:
        verify_token = await create_email_verification_token(db, user)
        base_url = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
        verify_link = f"{base_url}/verifica-email?token={verify_token}"
        await send_verification_email(user.email, user.full_name, verify_link)
    except Exception:
        logger.exception("rinvia verification email failed")
        return JSONResponse({"error": "Invio fallito"}, status_code=500)
    return JSONResponse({"status": "sent"})


@app.post("/api/logout-all")
@limiter.limit("5/hour")
async def api_logout_all(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    user.token_version = (user.token_version or 0) + 1
    await db.commit()
    response = JSONResponse({"status": "ok"})
    response.delete_cookie("access_token")
    return response


@app.get("/account", response_class=HTMLResponse)
async def account_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user)
):
    error = request.query_params.get("error", "")
    message = request.query_params.get("message", "")
    return templates.TemplateResponse(
        "account.html",
        {"request": request, "user": user, "error": error, "message": message}
    )


@app.post("/api/account/update")
@limiter.limit("10/hour")
async def api_account_update(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    company_name: str = Form(""),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    from urllib.parse import quote_plus
    full_name_clean = full_name.strip()
    company_clean = company_name.strip() or None

    if len(full_name_clean) < 2 or len(full_name_clean) > 120:
        return RedirectResponse("/account?error=Nome+non+valido", status_code=302)

    try:
        valid = validate_email(email, check_deliverability=False)
        email_norm = valid.normalized.lower()
    except EmailNotValidError:
        return RedirectResponse("/account?error=Email+non+valida", status_code=302)

    email_changed = email_norm != user.email
    if email_changed:
        existing = await get_user_by_email(db, email_norm)
        if existing and existing.id != user.id:
            return RedirectResponse("/account?error=Email+già+in+uso", status_code=302)

    user.full_name = full_name_clean
    user.company_name = company_clean
    message = "Dati aggiornati"

    if email_changed:
        user.email = email_norm
        user.email_verified = False
        user.email_verified_at = None
        user.token_version = (user.token_version or 0) + 1
        await db.commit()
        try:
            verify_token = await create_email_verification_token(db, user)
            base_url = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
            verify_link = f"{base_url}/verifica-email?token={verify_token}"
            await send_verification_email(user.email, user.full_name, verify_link)
        except Exception:
            logger.exception("verification email on update failed")
        new_token = create_access_token({"sub": user.email}, token_version=user.token_version)
        response = RedirectResponse(
            f"/account?message={quote_plus('Email cambiata. Controlla la nuova casella per verificarla.')}",
            status_code=302,
        )
        response.set_cookie(
            "access_token", new_token,
            httponly=True, secure=_COOKIE_SECURE, samesite="lax",
            max_age=60 * 60 * 24 * 30,
        )
        return response

    await db.commit()
    return RedirectResponse(f"/account?message={quote_plus(message)}", status_code=302)


@app.post("/api/account/password")
@limiter.limit("5/hour")
async def api_account_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    from urllib.parse import quote_plus
    if not verify_password(current_password, user.hashed_password):
        return RedirectResponse("/account?error=Password+attuale+non+corretta", status_code=302)
    ok, msg = validate_password_strength(new_password)
    if not ok:
        return RedirectResponse(f"/account?error={quote_plus(msg)}", status_code=302)
    user.hashed_password = hash_password(new_password)
    user.token_version = (user.token_version or 0) + 1
    await db.commit()
    response = RedirectResponse(
        "/login?error=Password+aggiornata%2C+rifai+il+login",
        status_code=302,
    )
    response.delete_cookie("access_token")
    return response


@app.get("/api/export-data")
@limiter.limit("5/hour")
async def api_export_data(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user)
):
    """GDPR art. 15 — diritto di accesso. Esporta tutti i dati utente in JSON."""
    result = await db.execute(
        select(Document)
        .where(Document.user_id == user.id)
        .order_by(Document.created_at.asc())
    )
    docs = result.scalars().all()

    export = {
        "export_version": "1.0",
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "company_name": user.company_name,
            "plan": user.plan,
            "is_active": user.is_active,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "stripe_customer_id": user.stripe_customer_id,
            "has_active_subscription": bool(user.stripe_subscription_id),
        },
        "documents": [
            {
                "id": d.id,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "cliente_nome": d.cliente_nome,
                "azienda_cliente": d.azienda_cliente,
                "input_text": d.input_text,
                "report_visita": d.report_visita,
                "email_followup": d.email_followup,
                "offerta_commerciale": d.offerta_commerciale,
            }
            for d in docs
        ],
        "document_count": len(docs),
    }
    filename = f"vynex-export-{user.id}-{datetime.utcnow().strftime('%Y%m%d')}.json"
    return JSONResponse(
        content=export,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.post("/api/delete-account")
@limiter.limit("3/hour")
async def api_delete_account(
    request: Request,
    password: str = Form(...),
    confirm: str = Form(""),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user)
):
    if confirm != "ELIMINA":
        return RedirectResponse("/account?error=Conferma+scrivendo+ELIMINA", status_code=302)
    if not verify_password(password, user.hashed_password):
        return RedirectResponse("/account?error=Password+non+corretta", status_code=302)

    if user.stripe_subscription_id:
        try:
            import stripe as _stripe
            await _stripe.Subscription.delete_async(user.stripe_subscription_id)
        except Exception:
            logger.exception("stripe subscription delete failed")

    now = datetime.utcnow()
    user.deleted_at = now
    user.is_active = False
    user.token_version = (user.token_version or 0) + 1
    user.email = f"deleted-{user.id}-{int(now.timestamp())}@deleted.local"
    await db.execute(
        Document.__table__.update()
        .where(Document.user_id == user.id)
        .values(deleted_at=now)
    )
    await db.commit()

    response = RedirectResponse("/?deleted=1", status_code=302)
    response.delete_cookie("access_token")
    return response


# ─── APP PROTETTA ─────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user)
):
    q = (request.query_params.get("q", "") or "").strip()
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    page_size = 10
    offset = (page - 1) * page_size

    base_query = select(Document).where(
        Document.user_id == user.id,
        Document.deleted_at.is_(None),
    )
    count_query = select(func.count(Document.id)).where(
        Document.user_id == user.id,
        Document.deleted_at.is_(None),
    )

    if q:
        like = f"%{q}%"
        filt = or_(
            Document.cliente_nome.ilike(like),
            Document.azienda_cliente.ilike(like),
            Document.input_text.ilike(like),
        )
        base_query = base_query.where(filt)
        count_query = count_query.where(filt)

    total = (await db.execute(count_query)).scalar() or 0
    result = await db.execute(
        base_query.order_by(Document.created_at.desc()).offset(offset).limit(page_size)
    )
    documenti = result.scalars().all()
    usage = await get_monthly_usage(db, user.id)
    upgrade_msg = request.query_params.get("upgrade", "")
    has_more = (offset + len(documenti)) < total

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "documenti": documenti,
        "usage": usage,
        "limit": user.monthly_limit,
        "upgrade_msg": upgrade_msg,
        "search_query": q,
        "page": page,
        "has_more": has_more,
        "total_docs": total,
    })


@app.get("/genera", response_class=HTMLResponse)
async def genera_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user)
):
    usage = await get_monthly_usage(db, user.id)
    can_generate = usage < user.monthly_limit
    return templates.TemplateResponse("genera.html", {
        "request": request,
        "user": user,
        "usage": usage,
        "limit": user.monthly_limit,
        "can_generate": can_generate
    })


@app.get("/documento/{doc_id}", response_class=HTMLResponse)
async def documento_detail(
    doc_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user)
):
    result = await db.execute(
        select(Document).where(
            Document.id == doc_id,
            Document.user_id == user.id,
            Document.deleted_at.is_(None),
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(404, "Documento non trovato")
    return templates.TemplateResponse("documento.html", {
        "request": request,
        "user": user,
        "doc": doc
    })


# ─── API JSON ─────────────────────────────────────────────────────────────────

@app.post("/api/genera")
@limiter.limit("30/minute")
async def api_genera(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user)
):
    body = await request.json()
    input_testo = body.get("input_testo", "").strip()
    azienda_mandante = body.get("azienda_mandante", "").strip()

    if not input_testo or len(input_testo) < 20:
        return JSONResponse({"error": "Descrizione troppo corta. Aggiungi più dettagli."}, status_code=400)

    if len(input_testo) > 2000:
        return JSONResponse({"error": "Descrizione troppo lunga (max 2000 caratteri)."}, status_code=400)

    # Atomic quota reservation: lock the user row, re-check usage, insert
    # a placeholder Document, commit (releases lock). Concurrent requests
    # serialize on the user row so quota can't be bypassed under load.
    # The placeholder is counted toward quota during the slow AI call;
    # on failure we soft-delete it so the slot is freed.
    locked_result = await db.execute(
        select(User).where(User.id == user.id).with_for_update()
    )
    locked_user = locked_result.scalar_one_or_none()
    if locked_user is None:
        await db.rollback()
        return JSONResponse({"error": "Utente non trovato"}, status_code=404)
    usage = await get_monthly_usage(db, user.id)
    if usage >= locked_user.monthly_limit:
        await db.rollback()
        return JSONResponse(
            {"error": "Limite mensile raggiunto. Prova Pro gratis per 10 giorni — documenti illimitati."},
            status_code=429
        )

    doc = Document(user_id=user.id, input_text=input_testo)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    try:
        result = await genera_documenti(
            input_testo=input_testo,
            nome_agente=user.full_name,
            azienda_mandante=azienda_mandante or (user.company_name or "")
        )
    except Exception:
        logger.exception("genera_documenti failed")
        doc.deleted_at = datetime.utcnow()
        await db.commit()
        return JSONResponse(
            {"error": "Errore durante la generazione. Riprova tra qualche secondo."},
            status_code=500
        )

    doc.report_visita = result["report_visita"]
    doc.email_followup = result["email_followup"]
    doc.offerta_commerciale = result["offerta_commerciale"]
    doc.cliente_nome = result.get("cliente_nome")
    doc.azienda_cliente = result.get("azienda_cliente")
    doc.tokens_used = result.get("tokens_used")
    doc.generation_time_ms = result.get("generation_time_ms")
    await db.commit()
    await db.refresh(doc)

    return JSONResponse({
        "doc_id": doc.id,
        "report_visita": doc.report_visita,
        "email_followup": doc.email_followup,
        "offerta_commerciale": doc.offerta_commerciale,
        "cliente_nome": doc.cliente_nome,
        "azienda_cliente": doc.azienda_cliente,
    })


@app.post("/api/rigenera")
@limiter.limit("30/minute")
async def api_rigenera(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user)
):
    if user.plan == "free":
        return JSONResponse(
            {"error": "Rigenerazione disponibile solo per Pro/Team. Passa a Pro per accedere."},
            status_code=402,
        )
    body = await request.json()
    doc_id = body.get("doc_id")
    tipo = body.get("tipo")
    istruzione = body.get("istruzione", "").strip()

    if tipo not in ("report_visita", "email_followup", "offerta_commerciale"):
        return JSONResponse({"error": "Tipo non valido"}, status_code=400)

    if not istruzione:
        return JSONResponse({"error": "Specifica cosa vuoi modificare."}, status_code=400)
    if len(istruzione) > 500:
        return JSONResponse({"error": "Istruzione troppo lunga (max 500 caratteri)."}, status_code=400)

    result = await db.execute(
        select(Document).where(Document.id == doc_id, Document.user_id == user.id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        return JSONResponse({"error": "Documento non trovato"}, status_code=404)

    documento_attuale = getattr(doc, tipo)
    try:
        nuovo_testo = await rigenera_documento(
            tipo=tipo,
            input_originale=doc.input_text,
            documento_attuale=documento_attuale,
            istruzione=istruzione,
            nome_agente=user.full_name
        )
    except Exception:
        logger.exception("rigenera_documento failed")
        return JSONResponse(
            {"error": "Errore durante la rigenerazione. Riprova tra qualche secondo."},
            status_code=500
        )

    setattr(doc, tipo, nuovo_testo)
    await db.commit()

    return JSONResponse({"testo": nuovo_testo})


# ─── STRIPE ───────────────────────────────────────────────────────────────────

@app.get("/checkout/{plan}")
async def checkout(
    plan: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user)
):
    if plan not in ("pro", "team"):
        raise HTTPException(400, "Piano non valido")
    if not os.getenv("STRIPE_SECRET_KEY"):
        raise HTTPException(503, "Pagamenti non ancora configurati")

    try:
        checkout_url = await create_checkout_session(db, user, plan)
    except RuntimeError as exc:
        logger.error("checkout failed: %s", exc)
        return RedirectResponse("/prezzi?error=Errore+pagamento.+Riprova.", status_code=302)
    return RedirectResponse(checkout_url, status_code=302)


@app.get("/portale-fatturazione")
async def portale_fatturazione(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user)
):
    if not user.stripe_customer_id:
        raise HTTPException(400, "Nessun abbonamento attivo")
    portal_url = await create_portal_session(user)
    return RedirectResponse(portal_url, status_code=302)


@app.post("/webhook/stripe")
async def webhook_stripe(request: Request, db: AsyncSession = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        await handle_webhook(payload, sig_header, db)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        logger.exception("webhook processing failed")
        raise HTTPException(500, "Webhook processing error")
    return {"status": "ok"}


# ─── SEO & HEALTH ─────────────────────────────────────────────────────────────

@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    base = os.getenv("BASE_URL", "").rstrip("/")
    sitemap_line = f"Sitemap: {base}/sitemap.xml\n" if base else ""
    disallow = (
        "Disallow: /dashboard\n"
        "Disallow: /genera\n"
        "Disallow: /documento/\n"
        "Disallow: /api/\n"
        "Disallow: /admin/\n"
        "Disallow: /checkout/\n"
        "Disallow: /portale-fatturazione\n"
        "Disallow: /reset-password\n"
        "Disallow: /recupera-password\n"
        "Disallow: /account\n"
    )
    ai_bots = [
        "GPTBot", "ChatGPT-User", "OAI-SearchBot",
        "ClaudeBot", "Claude-Web", "anthropic-ai",
        "PerplexityBot", "Perplexity-User",
        "Google-Extended", "GoogleOther",
        "Applebot-Extended", "Bytespider",
        "CCBot", "cohere-ai", "Diffbot",
        "Amazonbot", "meta-externalagent", "FacebookBot",
        "YouBot", "ImagesiftBot", "DuckAssistBot",
    ]
    ai_blocks = "".join(f"\nUser-agent: {bot}\nAllow: /\n{disallow}" for bot in ai_bots)
    return (
        "User-agent: *\n"
        "Allow: /\n"
        f"{disallow}"
        f"{ai_blocks}\n"
        f"{sitemap_line}"
    )


@app.get("/sitemap.xml")
async def sitemap():
    base = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
    pages = [
        ("/",              "1.0", "weekly"),
        ("/prezzi",        "0.9", "weekly"),
        ("/chi-siamo",     "0.8", "monthly"),
        ("/come-funziona", "0.8", "monthly"),
        ("/registrati",    "0.7", "monthly"),
        ("/login",         "0.5", "monthly"),
        ("/privacy",       "0.3", "yearly"),
        ("/termini",       "0.3", "yearly"),
        ("/cookie",        "0.3", "yearly"),
    ]
    today = datetime.utcnow().strftime("%Y-%m-%d")
    items = "".join(
        f"<url><loc>{base}{u}</loc><lastmod>{today}</lastmod>"
        f"<changefreq>{freq}</changefreq><priority>{prio}</priority></url>"
        for u, prio, freq in pages
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{items}"
        "</urlset>"
    )
    return Response(content=xml, media_type="application/xml")


@app.get("/llms.txt", response_class=PlainTextResponse)
async def llms_txt():
    """Index markdown per LLM crawler (standard llmstxt.org)."""
    base = os.getenv("BASE_URL", "https://agentia-production-fb78.up.railway.app").rstrip("/")
    return f"""# VYNEX

> VYNEX è un SaaS italiano di intelligenza artificiale che genera report di visita, email di follow-up e offerte commerciali in 30 secondi, a partire da una descrizione testuale della visita. Costruito specificamente per agenti commerciali italiani, rappresentanti plurimandatari e reti vendita aziendali.

Fondato nel 2026 da Roberto Pizzini. Sede: Italia. Lingua: italiano. Target: agenti commerciali, plurimandatari, aziende con reti vendita in Italia.

## Prodotto

- [Homepage]({base}/): Overview prodotto, hero, funzionalità, pricing, FAQ
- [Prezzi]({base}/prezzi): Piano Free (€0, 10 docs/mese), Pro (€49/mese illimitati), Team (€89/agente, min 3)
- [Chi siamo]({base}/chi-siamo): Missione, fondatore, timeline, contatti

## Come funziona

1. L'utente descrive la visita in linguaggio naturale (2 minuti, informale come a un collega)
2. VYNEX invoca un modello AI (Claude Haiku 4.5) calibrato su italiano commerciale
3. Il sistema genera 3 documenti in ~30 secondi: report di visita, email di follow-up, offerta commerciale
4. I documenti possono essere affinati con istruzioni in linguaggio naturale ("rendi più formale", "aggiungi sconto 15%")

## Fatti chiave

- Stack: FastAPI, PostgreSQL (Supabase), Claude Haiku 4.5, Stripe, Railway
- Output: 3 documenti da 1 input, tempo medio 28-30 secondi
- Piano Free: 10 documenti/mese per sempre, senza carta di credito
- Piano Pro: €49/mese, 10 giorni di prova gratuiti, documenti illimitati
- Piano Team: €89/agente/mese, minimo 3 agenti, dashboard team
- Lingua: italiano professionale nativo (non traduzione)
- Dati: ospitati in EU (Frankfurt, Supabase), conformi GDPR
- Risparmio stimato: ~20 ore al mese per agente

## Policy

- [Privacy]({base}/privacy): trattamento dati GDPR
- [Termini]({base}/termini): condizioni di servizio
- [Cookie]({base}/cookie): policy cookie (solo tecnici)

## Contatti

- Email: ciao@vynex.it
- Sito: {base}
"""


@app.get("/llms-full.txt", response_class=PlainTextResponse)
async def llms_full_txt():
    """Versione estesa con tutti i contenuti pubblici chiave per LLM."""
    base = os.getenv("BASE_URL", "https://agentia-production-fb78.up.railway.app").rstrip("/")
    return f"""# VYNEX — documentazione estesa per LLM

## Cos'è VYNEX

VYNEX è un software-as-a-service italiano di intelligenza artificiale, fondato nel 2026 da Roberto Pizzini, pensato per automatizzare la scrittura della documentazione commerciale generata da agenti e rappresentanti dopo ogni visita a un cliente. L'utente descrive a parole la visita come farebbe con un collega e VYNEX produce automaticamente tre documenti professionali: un report di visita dettagliato, un'email di follow-up personalizzata e un'offerta commerciale pronta da inviare. L'intero processo richiede circa 30 secondi.

## Il problema che risolve

Un agente commerciale italiano tipo visita 5-10 clienti al giorno. Dopo ogni visita deve: scrivere un report al mandante, inviare un'email di follow-up al cliente, preparare un'offerta commerciale. Queste tre attività richiedono complessivamente 20-30 minuti per visita, cioè 2-5 ore al giorno di lavoro amministrativo che si somma al lavoro sul campo. Gli agenti arrivano a casa la sera e devono ancora scrivere i documenti della giornata. VYNEX riduce questo tempo da 25 minuti a 30 secondi per visita.

## Per chi è pensato

- Agenti commerciali con P.IVA (plurimandatari, monomandatari, rappresentanti)
- Reti vendita aziendali (aziende con 3-100 agenti)
- PMI italiane che vogliono standardizzare la qualità della reportistica commerciale
- Settori serviti: ferramenta, edilizia, industriale, HORECA, retail, manifatturiero

## Funzionalità principali

1. **Report di visita automatico**: strutturato con obiettivi, svolgimento, next steps. Pronto per il mandante.
2. **Email di follow-up**: personalizzata sul cliente, tono caldo e professionale italiano.
3. **Offerta commerciale**: include condizioni discusse, sconti, termini. Pronta da firmare.
4. **Affinamento con istruzioni AI**: modificare i documenti con linguaggio naturale ("rendi più formale", "aggiungi sconto 15%", "togli il paragrafo sull'urgenza").
5. **Storico completo**: tutti i documenti salvati, ricerca rapida per azienda/cliente/data.
6. **Italiano professionale nativo**: terminologia commerciale italiana corretta, non traduzione da inglese.

## Prezzi

| Piano | Prezzo | Documenti | Note |
|---|---|---|---|
| Free | €0/mese | 10/mese | Per sempre, senza carta di credito |
| Pro | €49/mese | Illimitati | 10 giorni di prova gratuita |
| Team | €89/agente/mese | Illimitati | Min. 3 agenti, dashboard team |

Tutti i piani includono: report di visita, email di follow-up, offerta commerciale, storico completo, italiano professionale. Pro e Team aggiungono affinamento con istruzioni AI e documenti illimitati.

## Stack tecnico

- Backend: FastAPI 0.115, Python 3.11, SQLAlchemy 2.0 async
- Database: PostgreSQL (Supabase, session pooler, region Frankfurt)
- AI: Anthropic Claude Haiku 4.5 via API
- Pagamenti: Stripe Checkout + Portal + Webhooks
- Email: Resend API (transazionali)
- Hosting: Railway (Hobby plan), Docker multi-stage
- Security: HSTS, CSP strict, X-Frame-Options DENY, rate limiting slowapi

## Sicurezza e privacy

- Dati cifrati in transito (TLS 1.3) e a riposo
- Hosting EU: Supabase Frankfurt, Railway EU region
- Conformità GDPR: cookie banner, privacy policy, termini, cookie policy
- Art. 15 GDPR: esportazione completa dati in JSON (`/api/export-data`)
- Art. 17 GDPR: eliminazione account con cancellazione cascata
- Password: bcrypt, JWT con purpose field, reset token con TTL 60min
- Rate limiting: 5-30 req/min sulle route sensibili
- Nessun cookie di profilazione di terze parti

## Fondatore

Roberto Pizzini è il fondatore e sviluppatore di VYNEX. VYNEX è un progetto indipendente, bootstrapped, senza investitori esterni. Obiettivo: libertà economica attraverso software utile agli italiani.

## Contatti

- Email: ciao@vynex.it
- Sito: {base}
- Lingua supporto: italiano

## Domande frequenti

**Come funziona VYNEX?**
Descrivi la visita in linguaggio naturale come parleresti a un collega (2 minuti). VYNEX usa un modello AI calibrato su italiano commerciale e genera automaticamente i 3 documenti in circa 30 secondi.

**Devo installare qualcosa?**
No. VYNEX è una web app. Si accede da qualsiasi browser, su mobile, tablet o desktop.

**Serve la carta di credito?**
No per iniziare. Il piano Free è gratuito per sempre. Il piano Pro offre 10 giorni di prova gratuita senza richiedere la carta.

**I documenti sono in italiano professionale?**
Sì. VYNEX usa un modello AI addestrato sulla terminologia commerciale italiana. Non è una traduzione automatica da inglese.

**Posso modificare i documenti?**
Sì. Ogni documento può essere affinato con istruzioni in linguaggio naturale oppure copiato e modificato manualmente.

**I miei dati sono al sicuro?**
Sì. Dati cifrati, hosting in EU (Frankfurt), conformità GDPR. Esportazione e cancellazione dati disponibili in qualsiasi momento.

**Posso disdire in qualsiasi momento?**
Sì. Nessun vincolo contrattuale. Disdici dal portale di fatturazione quando vuoi. Conservi l'accesso fino alla fine del periodo pagato.

**Cosa succede se supero i 10 documenti del piano Free?**
I documenti si rinnovano il primo di ogni mese. Se servono di più, Pro offre 10 giorni gratis per documenti illimitati.
"""


@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    """Shallow health — returns 200 quickly, used by platform probes."""
    return {"status": "ok", "service": "vynex"}


@app.get("/health/deep")
@limiter.limit("30/minute")
async def health_deep(request: Request, db: AsyncSession = Depends(get_db)):
    """Deep health: DB + Anthropic + Stripe + Resend reachability."""
    checks: dict = {}

    try:
        await db.execute(select(func.count(User.id)))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"

    checks["anthropic_api_key"] = "set" if os.getenv("ANTHROPIC_API_KEY") else "missing"
    checks["stripe_api_key"] = "set" if os.getenv("STRIPE_SECRET_KEY") else "missing"
    checks["stripe_webhook_secret"] = "set" if os.getenv("STRIPE_WEBHOOK_SECRET") else "missing"
    checks["resend_api_key"] = "set" if os.getenv("RESEND_API_KEY") else "missing"

    all_ok = checks.get("database") == "ok" and checks.get("anthropic_api_key") == "set"
    return JSONResponse(
        {"status": "ok" if all_ok else "degraded", "checks": checks},
        status_code=200 if all_ok else 503,
    )


@app.post("/api/apply-coupon")
@limiter.limit("10/hour")
async def api_apply_coupon(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    body = await request.json()
    code = (body.get("code") or "").strip()
    ok, msg = await apply_coupon(db, user, code)
    if not ok:
        return JSONResponse({"error": msg}, status_code=400)
    return JSONResponse({"status": "ok", "stripe_coupon": msg})


@app.get("/api/documento/{doc_id}/pdf")
@limiter.limit("30/hour")
async def api_documento_pdf(
    doc_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    if user.plan == "free":
        raise HTTPException(
            402,
            "Export PDF disponibile solo per Pro/Team. Passa a Pro per scaricare.",
        )
    result = await db.execute(
        select(Document).where(
            Document.id == doc_id,
            Document.user_id == user.id,
            Document.deleted_at.is_(None),
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(404, "Documento non trovato")

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
    except ImportError:
        raise HTTPException(503, "Export PDF non disponibile")

    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title=f"VYNEX — Documento {doc.id}",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleVynex", parent=styles["Title"], fontSize=18, textColor="#0f172a"
    )
    h2_style = ParagraphStyle(
        "H2Vynex", parent=styles["Heading2"], fontSize=13, textColor="#1e293b", spaceBefore=12
    )
    body_style = ParagraphStyle(
        "BodyVynex", parent=styles["BodyText"], fontSize=10, leading=14, textColor="#1e293b"
    )

    def _section(title_txt: str, body_txt: str | None):
        if not body_txt:
            return []
        safe = body_txt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        safe = safe.replace("\n", "<br/>")
        return [Paragraph(title_txt, h2_style), Spacer(1, 4), Paragraph(safe, body_style)]

    story = [
        Paragraph(f"VYNEX — {doc.cliente_nome or 'Documento'}", title_style),
        Paragraph(
            f"{doc.azienda_cliente or ''} · generato il {doc.created_at.strftime('%d/%m/%Y')}",
            body_style,
        ),
        Spacer(1, 8),
    ]
    story += _section("Report di visita", doc.report_visita)
    story += [PageBreak()] + _section("Email di follow-up", doc.email_followup)
    story += [PageBreak()] + _section("Offerta commerciale", doc.offerta_commerciale)

    # reportlab is synchronous and CPU-bound. Running it directly on the
    # event loop blocks every other coroutine for hundreds of ms per call.
    await asyncio.to_thread(pdf.build, story)
    buffer.seek(0)
    filename = f"vynex-{doc.id}.pdf"
    return Response(
        content=buffer.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/admin/metrics")
@limiter.limit("30/minute")
async def admin_metrics(request: Request, db: AsyncSession = Depends(get_db)):
    """Aggregati business per il founder. Protetto da ADMIN_TOKEN header."""
    admin_token = os.getenv("ADMIN_TOKEN", "")
    header_token = request.headers.get("X-Admin-Token", "")
    if not admin_token or not hmac.compare_digest(
        header_token.encode("utf-8"), admin_token.encode("utf-8")
    ):
        raise HTTPException(401, "Non autorizzato")

    from datetime import timedelta
    now = datetime.utcnow()
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    total_users = (await db.execute(select(func.count(User.id)))).scalar() or 0
    active_users = (await db.execute(
        select(func.count(User.id)).where(User.is_active == True)
    )).scalar() or 0
    pro_users = (await db.execute(
        select(func.count(User.id)).where(User.plan == "pro")
    )).scalar() or 0
    team_users = (await db.execute(
        select(func.count(User.id)).where(User.plan == "team")
    )).scalar() or 0
    paying_with_sub = (await db.execute(
        select(func.count(User.id)).where(User.stripe_subscription_id.isnot(None))
    )).scalar() or 0

    docs_total = (await db.execute(select(func.count(Document.id)))).scalar() or 0
    docs_24h = (await db.execute(
        select(func.count(Document.id)).where(Document.created_at >= day_ago)
    )).scalar() or 0
    docs_7d = (await db.execute(
        select(func.count(Document.id)).where(Document.created_at >= week_ago)
    )).scalar() or 0
    docs_mtd = (await db.execute(
        select(func.count(Document.id)).where(Document.created_at >= month_start)
    )).scalar() or 0

    signups_24h = (await db.execute(
        select(func.count(User.id)).where(User.created_at >= day_ago)
    )).scalar() or 0
    signups_7d = (await db.execute(
        select(func.count(User.id)).where(User.created_at >= week_ago)
    )).scalar() or 0

    mrr = pro_users * 49 + team_users * 89

    return {
        "generated_at": now.isoformat() + "Z",
        "users": {
            "total": total_users,
            "active": active_users,
            "free": total_users - pro_users - team_users,
            "pro": pro_users,
            "team": team_users,
            "paying": paying_with_sub,
            "signups_24h": signups_24h,
            "signups_7d": signups_7d,
        },
        "revenue": {
            "mrr_eur": mrr,
            "target_eur": 117,
            "progress_pct": round(mrr / 117 * 100, 1) if mrr else 0,
        },
        "activity": {
            "documents_total": docs_total,
            "documents_24h": docs_24h,
            "documents_7d": docs_7d,
            "documents_mtd": docs_mtd,
        },
    }


@app.exception_handler(404)
async def not_found(request: Request, exc: HTTPException):
    accept = request.headers.get("accept", "")
    if "text/html" not in accept:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return templates.TemplateResponse(
        "404.html", {"request": request}, status_code=404
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
