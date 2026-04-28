import observability  # noqa: F401  (auto-init Sentry, no-op if SENTRY_DSN missing)

from fastapi import FastAPI, Request, Depends, Form, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.gzip import GZipMiddleware
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
import re

from dotenv import load_dotenv
load_dotenv()

from email_validator import validate_email, EmailNotValidError

from database import get_db, init_db, AsyncSessionLocal
from models import User, Document, Lead, EmailJob, ReferralClick, BlogPost, LeadSource, NPSResponse, APIKey, AuditLog, EmailVerificationToken
from auth import (
    hash_password, verify_password, authenticate_user, create_access_token,
    get_current_user, require_user, get_user_by_email,
    create_password_reset_token, verify_password_reset_token,
    validate_password_strength, create_email_verification_token,
    consume_email_verification_token,
)
from ai_engine import genera_documenti, rigenera_documento
from acquisition import (
    post_signup_setup,
    upsert_lead,
    enroll_lead_in_sequence,
    process_email_queue,
    reset_all_retries,
    save_source_attribution,
    verify_sig,
)
from email_templates import SEQUENCE_LEAD_DEMO
from scheduler import start_scheduler, shutdown_scheduler
from stripe_handler import (
    create_checkout_session, create_portal_session, handle_webhook, apply_coupon,
)
from rate_limit import limiter, rate_limit_exceeded_handler, RateLimitExceeded
from emailer import (
    send_welcome_email, send_password_reset_email, send_verification_email,
    send_demo_recovery_email, send_lead_magnet_email,
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
    try:
        start_scheduler()
    except Exception:
        logger.exception("scheduler start failed — acquisition drip disabled")
    try:
        yield
    finally:
        try:
            await shutdown_scheduler()
        except Exception:
            logger.exception("scheduler shutdown failed")


app = FastAPI(title="VYNEX", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # /embed/* deve poter essere iframmato da siti partner → no X-Frame-Options
        if request.url.path.startswith("/embed"):
            response.headers["X-Frame-Options"] = "SAMEORIGIN"  # soft — permette altri domini via CSP
        else:
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
        # Override per /embed/*: permetti embedding da qualsiasi origine
        if request.url.path.startswith("/embed"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "img-src 'self' data:; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com; "
                "script-src 'self' 'unsafe-inline'; "
                "base-uri 'self'; form-action 'self'; "
                "frame-ancestors *"  # iframe da qualsiasi dominio
            )
        return response


_CSRF_EXEMPT_PATHS = ("/webhook/stripe", "/webhook/resend", "/auth/google/verify", "/api/admin/")
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


class UtmTrackingMiddleware(BaseHTTPMiddleware):
    """Cattura utm_source/medium/campaign/term/content dai query params
    e li salva in cookie vynex_utm (30 giorni) per attribution a Lead/User
    al momento della conversione."""

    _UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        try:
            qp = request.query_params
            found = {k: qp.get(k, "")[:120] for k in self._UTM_KEYS if qp.get(k)}
            if found:
                # add referer and landing on first touch
                if "first_referer" not in found:
                    ref = request.headers.get("referer", "")[:500]
                    if ref and "agentia-production-fb78" not in ref and "vynex" not in ref:
                        found["first_referer"] = ref
                found["first_landing"] = str(request.url.path)[:500]
                import json as _j
                response.set_cookie(
                    "vynex_utm", _j.dumps(found),
                    httponly=True, secure=_COOKIE_SECURE, samesite="lax",
                    max_age=60 * 60 * 24 * 30,
                )
        except Exception:
            logger.exception("utm middleware set-cookie failed")
        return response


app.add_middleware(UtmTrackingMiddleware)
app.add_middleware(OriginCSRFMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.globals["canonical_base"] = (
    os.getenv("BASE_URL", "https://vynex.it").rstrip("/")
)


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
    response = templates.TemplateResponse(
        "index.html", {"request": request}
    )
    return response


@app.get("/embed/demo", response_class=HTMLResponse)
async def embed_demo(request: Request):
    """Widget iframe-safe per embedding su siti terzi (partner, blog)."""
    return templates.TemplateResponse(
        "embed_demo.html",
        {"request": request, "error": request.query_params.get("error", "")},
    )


@app.get("/embed", response_class=HTMLResponse)
async def embed_docs(request: Request):
    """Documentazione copy-paste codice iframe per partner."""
    base = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
    html = f"""<!DOCTYPE html><html lang="it"><head><meta charset="UTF-8">
<title>Embed VYNEX — widget demo per siti partner</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<style>body{{font-family:Inter,sans-serif;background:#04060f;color:#f1f5f9;max-width:720px;margin:0 auto;padding:60px 24px;line-height:1.6}}
code{{background:#1e293b;padding:12px 16px;border-radius:8px;display:block;color:#60a5fa;font-size:13px;overflow-x:auto;white-space:pre-wrap;word-break:break-all}}
h1{{font-size:32px;font-weight:800}}h2{{font-size:20px;margin-top:32px}}p{{color:#cbd5e1}}a{{color:#60a5fa}}</style></head><body>
<h1>Embed il widget VYNEX sul tuo sito</h1>
<p>Integra la demo di VYNEX su qualsiasi pagina web con un iframe. I lead generati
vengono automaticamente attribuiti alla tua fonte via UTM. Zero SDK, zero JS.</p>
<h2>Codice embed (copia-incolla)</h2>
<code>&lt;iframe src="{base}/embed/demo?utm_source=partner&amp;utm_campaign=embed"
  width="100%" height="640" style="border:1px solid #1e293b;border-radius:12px"
  title="VYNEX Demo"&gt;&lt;/iframe&gt;</code>
<h2>Parametri UTM personalizzati</h2>
<p>Sostituisci <code style="display:inline;padding:2px 6px">utm_source=partner</code>
con il tuo ID traffico. Esempio:</p>
<code>{base}/embed/demo?utm_source=iltuoblog.it&amp;utm_medium=sidebar&amp;utm_campaign=nov2026</code>
<h2>Responsive</h2>
<p>Il widget si adatta da 300px a 800px di larghezza. Altezza consigliata: 640-700px.</p>
<h2>Referenze</h2>
<p><a href="{base}/">Home VYNEX</a> · <a href="{base}/demo">Demo full-page</a> ·
<a href="mailto:robertopizzini19@gmail.com">Supporto partner</a></p>
</body></html>"""
    return HTMLResponse(html)


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

    try:
        ref_cookie = (request.cookies.get("vynex_ref") or "")[:16] or None
        await post_signup_setup(db, new_user, ref_cookie)
    except Exception:
        logger.exception("post_signup_setup (google) failed")

    access = create_access_token({"sub": new_user.email}, token_version=0)
    response = redirect_with_cookie("/dashboard", access)
    response.delete_cookie("vynex_ref")
    return response


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
    client_ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "")
    )[:45]
    user, reason = await authenticate_user(db, email, password)
    if not user:
        logger.warning("login failed: email=%s reason=%s ip=%s", email[:50], reason, client_ip)
        if reason == "locked":
            db.add(AuditLog(action="login_locked", detail=f"email={email[:255]}", ip=client_ip))
            await db.commit()
            return RedirectResponse("/login?error=Account+temporaneamente+bloccato.+Riprova+tra+15+minuti", status_code=302)
        if reason == "deleted":
            return RedirectResponse("/login?error=Account+eliminato", status_code=302)
        if reason == "inactive":
            return RedirectResponse("/login?error=Account+disattivato", status_code=302)
        return RedirectResponse("/login?error=Email+o+password+non+corretti", status_code=302)
    db.add(AuditLog(user_id=user.id, action="login", ip=client_ip))
    await db.commit()
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

    if not full_name.strip() or len(full_name.strip()) < 2 or len(full_name.strip()) > 120:
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

    db.add(AuditLog(user_id=user.id, action="signup", detail=f"email={user.email}", ip=client_ip))
    await db.commit()

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

    try:
        ref_cookie = (request.cookies.get("vynex_ref") or "")[:16] or None
        await post_signup_setup(db, user, ref_cookie)
    except Exception:
        logger.exception("post_signup_setup failed")

    try:
        await save_source_attribution(
            db,
            user_id=user.id,
            utm_cookie=request.cookies.get("vynex_utm"),
            ip=client_ip,
            user_agent=user_agent,
        )
    except Exception:
        logger.exception("save_source_attribution signup failed user=%s", user.id)

    token = create_access_token({"sub": user.email}, token_version=user.token_version or 0)
    response = redirect_with_cookie("/benvenuto", token)
    response.delete_cookie("vynex_ref")
    return response


@app.get("/benvenuto", response_class=HTMLResponse)
async def benvenuto(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    return templates.TemplateResponse("benvenuto.html", {
        "request": request,
        "user": user,
    })


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
    original_email = user.email
    user.deleted_at = now
    user.is_active = False
    user.token_version = (user.token_version or 0) + 1
    user.email = f"deleted-{user.id}-{int(now.timestamp())}@deleted.local"
    await db.execute(
        Document.__table__.update()
        .where(Document.user_id == user.id)
        .values(deleted_at=now)
    )
    client_ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "")
    )[:45]
    db.add(AuditLog(user_id=user.id, action="account_deleted", detail=f"email={original_email}", ip=client_ip))
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

    if not user.referral_code:
        try:
            from acquisition import referral_code as _gen_ref
            for _ in range(5):
                code = _gen_ref()
                exists = await db.execute(
                    select(User.id).where(User.referral_code == code)
                )
                if exists.scalar_one_or_none() is None:
                    user.referral_code = code
                    await db.commit()
                    break
        except Exception:
            logger.exception("lazy referral_code gen failed")

    referrals_signups = (await db.execute(
        select(func.count(User.id)).where(User.referred_by_id == user.id)
    )).scalar() or 0
    referrals_paying = (await db.execute(
        select(func.count(User.id))
        .where(User.referred_by_id == user.id)
        .where(User.plan.in_(("pro", "team")))
    )).scalar() or 0

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "documenti": documenti,
        "usage": usage,
        "limit": user.monthly_limit,
        "upgrade_msg": upgrade_msg,
        "q": q,
        "search_query": q,
        "page": page,
        "has_more": has_more,
        "total_docs": total,
        "base_url": os.getenv("BASE_URL", "http://localhost:8000").rstrip("/"),
        "referrals_signups": referrals_signups,
        "referrals_paying": referrals_paying,
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
@limiter.limit("10/hour")
async def checkout(
    request: Request,
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


@app.post("/webhook/resend")
async def webhook_resend(request: Request, db: AsyncSession = Depends(get_db)):
    from resend_webhook import handle_webhook as handle_resend_webhook

    body = await request.body()
    svix_id = request.headers.get("svix-id", "")
    svix_timestamp = request.headers.get("svix-timestamp", "")
    svix_signature = request.headers.get("svix-signature", "")
    try:
        event_type = await handle_resend_webhook(
            body, svix_id, svix_timestamp, svix_signature, db
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        logger.exception("resend webhook processing failed")
        raise HTTPException(500, "Webhook processing error")
    return {"status": "ok", "event": event_type}


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
async def sitemap(db: AsyncSession = Depends(get_db)):
    base = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
    pages = [
        ("/",              "1.0", "weekly"),
        ("/demo",          "0.95", "weekly"),
        ("/prezzi",        "0.9", "weekly"),
        ("/blog",          "0.85", "weekly"),
        ("/come-funziona", "0.8", "monthly"),
        ("/chi-siamo",     "0.8", "monthly"),
        ("/registrati",    "0.7", "monthly"),
        ("/login",         "0.5", "monthly"),
        ("/privacy",       "0.3", "yearly"),
        ("/termini",       "0.3", "yearly"),
        ("/cookie",        "0.3", "yearly"),
    ]
    today = datetime.utcnow().strftime("%Y-%m-%d")
    items_list = [
        f"<url><loc>{base}{u}</loc><lastmod>{today}</lastmod>"
        f"<changefreq>{freq}</changefreq><priority>{prio}</priority></url>"
        for u, prio, freq in pages
    ]
    try:
        posts = await db.execute(
            select(BlogPost)
            .where(BlogPost.published.is_(True))
            .order_by(BlogPost.published_at.desc())
        )
        for p in posts.scalars().all():
            lm = (p.updated_at or p.published_at or datetime.utcnow()).strftime("%Y-%m-%d")
            items_list.append(
                f"<url><loc>{base}/blog/{p.slug}</loc><lastmod>{lm}</lastmod>"
                f"<changefreq>monthly</changefreq><priority>0.7</priority></url>"
            )
    except Exception:
        logger.exception("sitemap blog enumeration failed")
    items = "".join(items_list)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{items}"
        "</urlset>"
    )
    return Response(content=xml, media_type="application/xml")


@app.get("/ai-context.json")
async def ai_context_json():
    """Structured context JSON per AI agents (Claude, GPT, Perplexity, Gemini).
    Standard emergente: alcuni agents leggono JSON strutturato per comprensione veloce del prodotto."""
    base = os.getenv("BASE_URL", "https://vynex.it").rstrip("/")
    return JSONResponse({
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": "VYNEX",
        "alternateName": ["vynex.it"],
        "applicationCategory": "BusinessApplication",
        "applicationSubCategory": "SalesDocumentGeneration",
        "operatingSystem": "Web browser, any",
        "description": (
            "SaaS italiano di AI che genera report di visita, email di follow-up "
            "e offerte commerciali in 30 secondi da una descrizione della visita. "
            "Per agenti di commercio italiani, plurimandatari e reti vendita B2B."
        ),
        "inLanguage": "it-IT",
        "url": base,
        "identifier": f"{base}/ai-context.json",
        "dateCreated": "2026-04",
        "author": {
            "@type": "Person",
            "name": "Roberto Pizzini",
            "email": "robertopizzini19@gmail.com",
        },
        "publisher": {
            "@type": "Organization",
            "name": "VYNEX",
            "url": base,
            "email": "robertopizzini19@gmail.com",
            "areaServed": {"@type": "Country", "name": "Italia"},
        },
        "offers": [
            {"@type": "Offer", "name": "Free", "price": "0", "priceCurrency": "EUR",
             "description": "10 documenti/mese per sempre, senza carta di credito",
             "eligibleCustomerType": "Individual"},
            {"@type": "Offer", "name": "Pro", "price": "49", "priceCurrency": "EUR",
             "priceSpecification": {"@type": "UnitPriceSpecification",
                                     "price": "49", "priceCurrency": "EUR",
                                     "unitText": "MONTH"},
             "description": "Documenti illimitati, 10 giorni prova gratuita",
             "eligibleCustomerType": "Individual"},
            {"@type": "Offer", "name": "Team", "price": "89", "priceCurrency": "EUR",
             "priceSpecification": {"@type": "UnitPriceSpecification",
                                     "price": "89", "priceCurrency": "EUR",
                                     "unitText": "MONTH", "referenceQuantity": {
                                         "@type": "QuantitativeValue",
                                         "value": "1", "unitCode": "AGENT"}},
             "description": "Min. 3 agenti, dashboard team, documenti illimitati",
             "eligibleCustomerType": "Business"},
        ],
        "featureList": [
            "Generazione 3 documenti da 1 descrizione testuale (report + follow-up + offerta)",
            "Tempo di generazione: 28-32 secondi reali",
            "Italiano professionale nativo (non traduzione da inglese)",
            "Affinamento documenti con istruzioni in linguaggio naturale",
            "Storico documenti ricercabile per cliente/azienda/data",
            "Esportazione PDF 3-pagine con layout italiano",
            "API REST pubblica v1 per integrazioni 3rd party",
            "Widget iframe embeddabile su siti partner",
            "Referral program con bonus Stripe +30 giorni ogni 2 conversioni",
            "GDPR compliant: export dati (art. 15), cancellazione (art. 17)",
            "Dati ospitati in EU (Frankfurt, Supabase)",
        ],
        "targetAudience": {
            "@type": "Audience",
            "audienceType": ["Agenti di commercio italiani",
                             "Plurimandatari", "Rappresentanti B2B",
                             "Reti vendita aziendali", "PMI con reparto commerciale"],
            "geographicArea": {"@type": "Country", "name": "Italia"},
        },
        "softwareRequirements": "Browser web moderno (Chrome, Safari, Firefox, Edge)",
        "softwareVersion": "1.0",
        "api": {
            "@type": "WebAPI",
            "name": "VYNEX API v1",
            "documentation": f"{base}/api/v1/docs",
            "baseUrl": f"{base}/api/v1",
            "authentication": "X-API-Key header (vx_<secret> format)",
            "rateLimit": "60 requests/minute per key",
            "endpoints": [
                {"method": "POST", "path": "/documents/generate",
                 "description": "Genera 3 documenti da descrizione visita"},
                {"method": "GET", "path": "/health",
                 "description": "Health check service"},
            ],
        },
        "sameAs": [],  # populate quando ci sono social verificati
        "typicalAgeRange": "35-60",
        "interactionStatistic": {
            "@type": "InteractionCounter",
            "interactionType": "https://schema.org/UseAction",
            "name": "Documents generated per user per month",
            "description": "Average 40-80 per Pro subscriber",
        },
        "contactPoint": {
            "@type": "ContactPoint",
            "email": "robertopizzini19@gmail.com",
            "contactType": "customer support",
            "availableLanguage": ["Italian"],
            "areaServed": "IT",
        },
        "_meta": {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "format_version": "1.0",
            "crawler_friendly": True,
            "updates_frequency": "realtime (pricing), monthly (features)",
        },
    })


@app.get("/llms.txt", response_class=PlainTextResponse)
async def llms_txt():
    """Index markdown per LLM crawler (standard llmstxt.org)."""
    base = os.getenv("BASE_URL", "https://vynex.it").rstrip("/")
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

- Email: robertopizzini19@gmail.com
- Sito: {base}
"""


@app.get("/llms-full.txt", response_class=PlainTextResponse)
async def llms_full_txt():
    """Versione estesa con tutti i contenuti pubblici chiave per LLM."""
    base = os.getenv("BASE_URL", "https://vynex.it").rstrip("/")
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

- Email: robertopizzini19@gmail.com
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
@limiter.limit("10/minute")
async def health_deep(request: Request, db: AsyncSession = Depends(get_db)):
    """Deep health: DB connectivity check. Admin-only for full details."""
    is_admin = False
    admin_token = os.getenv("ADMIN_TOKEN", "")
    header_token = (
        request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    )
    if admin_token and header_token and hmac.compare_digest(header_token, admin_token):
        is_admin = True

    try:
        await db.execute(select(func.count(User.id)))
        db_ok = True
    except Exception as exc:
        db_ok = False
        if is_admin:
            logger.warning("health/deep DB error: %s", exc)

    if not is_admin:
        return JSONResponse(
            {"status": "ok" if db_ok else "degraded"},
            status_code=200 if db_ok else 503,
        )

    checks = {
        "database": "ok" if db_ok else "error",
        "anthropic_api_key": "set" if os.getenv("ANTHROPIC_API_KEY") else "missing",
        "stripe_api_key": "set" if os.getenv("STRIPE_SECRET_KEY") else "missing",
        "stripe_webhook_secret": "set" if os.getenv("STRIPE_WEBHOOK_SECRET") else "missing",
        "resend_api_key": "set" if os.getenv("RESEND_API_KEY") else "missing",
    }
    all_ok = db_ok and checks.get("anthropic_api_key") == "set"
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


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard_html():
    """Dashboard admin UI — fetcha endpoint admin lato client con token in localStorage.
    Token chiesto 1 volta via prompt() al primo accesso, persistito 30 giorni."""
    return HTMLResponse("""<!DOCTYPE html><html lang="it"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VYNEX · Admin</title>
<meta name="robots" content="noindex,nofollow">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
body{margin:0;font-family:Inter,system-ui,sans-serif;background:#04060f;color:#f1f5f9;padding:32px;max-width:1200px;margin:0 auto}
h1{font-size:28px;font-weight:800;margin:0 0 6px;background:linear-gradient(135deg,#60a5fa,#8b5cf6,#ec4899);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.sub{color:#64748b;font-size:13px;margin-bottom:28px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;margin-bottom:32px}
.card{background:rgba(15,23,42,0.6);border:1px solid rgba(96,165,250,0.2);border-radius:14px;padding:20px;backdrop-filter:blur(10px)}
.k{color:#94a3b8;font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase}
.v{font-size:28px;font-weight:800;color:#f1f5f9;margin-top:6px}
.vs{font-size:12px;color:#64748b;margin-top:4px}
.section{background:rgba(15,23,42,0.4);border:1px solid rgba(96,165,250,0.15);border-radius:14px;padding:24px;margin-bottom:20px}
.section h2{font-size:16px;font-weight:700;margin:0 0 16px;color:#60a5fa;letter-spacing:0.3px}
pre{background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:14px;font-size:12px;overflow-x:auto;color:#cbd5e1;margin:0;font-family:Menlo,Consolas,monospace}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
button{background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff;border:none;padding:8px 14px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer}
button:hover{transform:translateY(-1px)}
button.ghost{background:rgba(15,23,42,0.8);border:1px solid #334155}
.err{background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);border-radius:8px;padding:12px;color:#fca5a5;margin-bottom:16px;font-size:13px}
a{color:#60a5fa;text-decoration:none}
</style></head><body>
<h1>VYNEX Admin</h1>
<div class="sub">Dashboard ops · Refresh ogni 60s · <span id="last-refresh">—</span> · <a href="/" style="margin-left:12px">← Site</a> · <a href="#" onclick="localStorage.removeItem('vynex_admin_token');location.reload();return false">🔓 Cambia token</a></div>

<div id="err"></div>

<div class="grid" id="overview"></div>

<div class="section">
  <h2>📧 EMAIL QUEUE</h2>
  <div class="grid" id="email-stats"></div>
  <div class="row" style="margin-top:14px">
    <button onclick="call('POST','/api/admin/acquisition/tick').then(r=>alert('tick: '+JSON.stringify(r)));">Tick manuale</button>
    <button class="ghost" onclick="if(confirm('Reset retry su tutti i job pending?'))call('POST','/api/admin/acquisition/retry-all').then(r=>alert('reset: '+JSON.stringify(r)));">Retry all</button>
  </div>
</div>

<div class="section">
  <h2>📊 NPS</h2>
  <div class="grid" id="nps-stats"></div>
</div>

<div class="section">
  <h2>📝 BLOG (ultimi)</h2>
  <pre id="blog-list">loading…</pre>
  <div class="row" style="margin-top:14px">
    <input id="blog-kw" placeholder="keyword long-tail" style="flex:1;padding:8px 12px;background:#0f172a;border:1px solid #1e293b;border-radius:8px;color:#f1f5f9;font-size:13px;min-width:240px">
    <button onclick="gen()">Genera articolo</button>
  </div>
</div>

<div class="section">
  <h2>🔧 HEALTH</h2>
  <pre id="health">loading…</pre>
</div>

<script>
let T = localStorage.getItem('vynex_admin_token');
if (!T) { T = prompt('Admin token:'); if (T) localStorage.setItem('vynex_admin_token', T); }

async function call(method, path, body) {
  const opts = {method, headers: {'Authorization': 'Bearer ' + T}};
  if (body) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  if (r.status === 401) { localStorage.removeItem('vynex_admin_token'); showErr('Token non valido — ricarica la pagina'); throw 'unauth'; }
  return r.ok ? r.json() : Promise.reject(await r.text());
}

function showErr(m) { document.getElementById('err').innerHTML = '<div class="err">'+m+'</div>'; }

function card(k, v, vs) { return `<div class="card"><div class="k">${k}</div><div class="v">${v}</div>${vs?'<div class="vs">'+vs+'</div>':''}</div>`; }

async function refresh() {
  try {
    const [acq, nps, blog] = await Promise.all([
      call('GET', '/api/admin/acquisition/stats'),
      call('GET', '/api/admin/nps/stats'),
      call('GET', '/api/admin/blog/list'),
    ]);

    document.getElementById('overview').innerHTML =
      card('LEADS totali', acq.leads.total, '+' + acq.leads.last_24h + ' ultime 24h') +
      card('EMAIL JOBS pending', acq.email_jobs.pending, '') +
      card('REFERRALS paying', acq.referrals.paying, acq.referrals.total_signups + ' iscritti totali') +
      card('SORGENTI lead', Object.keys(acq.leads.by_source).length, JSON.stringify(acq.leads.by_source));

    document.getElementById('email-stats').innerHTML =
      card('Pending', acq.email_jobs.pending, '') +
      card('Sent 24h', acq.email_jobs.sent_24h, '') +
      card('Failed 24h', acq.email_jobs.failed_24h || 0, '') +
      card('Opens 7d', acq.email_jobs.opened_7d, '') +
      card('Clicks 7d', acq.email_jobs.clicked_7d, '');

    document.getElementById('nps-stats').innerHTML =
      card('NPS score', nps.nps !== null ? nps.nps : '—', 'n=' + nps.total) +
      card('Promoters (9-10)', nps.buckets.promoters, '') +
      card('Passives (7-8)', nps.buckets.passives, '') +
      card('Detractors (0-6)', nps.buckets.detractors, '') +
      card('Avg score', nps.avg !== null ? nps.avg.toFixed(1) : '—', '');

    document.getElementById('blog-list').textContent = blog.length
      ? blog.slice(0, 10).map(p => (p.published?'✓':'✗') + ' ' + p.slug + '  [' + (p.keyword || '—') + ']').join('\\n')
      : 'nessun articolo';

    const h = await fetch('/health').then(r=>r.json());
    document.getElementById('health').textContent = JSON.stringify(h, null, 2);

    document.getElementById('last-refresh').textContent = new Date().toLocaleTimeString('it');
    document.getElementById('err').innerHTML = '';
  } catch (e) { if (e !== 'unauth') showErr('Errore: ' + e); }
}

async function gen() {
  const kw = document.getElementById('blog-kw').value.trim();
  if (!kw) return alert('Inserisci una keyword');
  const btn = event.target; btn.disabled = true; btn.innerText = 'Generazione…';
  try {
    const r = await call('POST', '/api/admin/blog/generate', {keyword: kw});
    alert('Articolo: ' + r.url);
    refresh();
  } catch (e) { alert('Errore: ' + e); }
  btn.disabled = false; btn.innerText = 'Genera articolo';
}

if (T) { refresh(); setInterval(refresh, 60000); }
else { showErr('Inserisci admin token e ricarica la pagina.'); }
</script></body></html>""")


@app.get("/admin/metrics")
@limiter.limit("30/minute")
async def admin_metrics(request: Request, db: AsyncSession = Depends(get_db)):
    """Aggregati business per il founder. Protetto da ADMIN_TOKEN header."""
    _require_admin(request)

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


# ─── ACQUISITION ENGINE — lead capture, drip, referral, tracking ─────────────

import json as _json
import csv as _csv

_GIF_1x1 = bytes([
    0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x01, 0x00, 0x01, 0x00,
    0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0xff, 0xff, 0xff, 0x21,
    0xf9, 0x04, 0x01, 0x00, 0x00, 0x00, 0x00, 0x2c, 0x00, 0x00,
    0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0x02, 0x02, 0x44,
    0x01, 0x00, 0x3b,
])


@app.get("/demo", response_class=HTMLResponse)
async def demo_page(request: Request):
    draft = {"email": "", "full_name": "", "company": "", "input_text": ""}
    raw = request.cookies.get("vynex_demo_draft", "")
    if raw:
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, dict):
                for k in draft:
                    v = parsed.get(k) or ""
                    if isinstance(v, str):
                        draft[k] = v[:2100]
        except Exception:
            pass
    response = templates.TemplateResponse(
        "demo.html",
        {
            "request": request,
            "error": request.query_params.get("error", ""),
            "draft": draft,
        },
    )
    return response


@app.get("/demo/recovery", response_class=HTMLResponse)
async def demo_recovery_page(request: Request):
    return templates.TemplateResponse(
        "demo_recovery.html",
        {
            "request": request,
            "error": request.query_params.get("error", ""),
            "message": request.query_params.get("message", ""),
        },
    )


@app.post("/demo/recovery")
@limiter.limit("5/hour")
async def api_demo_recovery(
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    from urllib.parse import quote_plus
    try:
        valid = validate_email(email, check_deliverability=False)
        email_norm = valid.normalized.lower()
    except EmailNotValidError:
        return RedirectResponse("/demo/recovery?error=Email+non+valida", status_code=302)

    r = await db.execute(select(Lead).where(Lead.email == email_norm))
    lead = r.scalar_one_or_none()

    # Always reply with the same message — non-enumeration.
    generic_ok = "Se l'email e' nel nostro sistema, ricevi il link entro 1 minuto."
    if lead is not None and lead.demo_input and not lead.unsubscribed:
        try:
            base = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
            link = f"{base}/demo/result/{lead.unsub_token}"
            await send_demo_recovery_email(lead.email, lead.full_name or "", link)
        except Exception:
            logger.exception("demo recovery email failed lead=%s", lead.id)
    return RedirectResponse(
        f"/demo/recovery?message={quote_plus(generic_ok)}", status_code=302
    )


@app.post("/api/demo")
@limiter.limit("10/hour")
async def api_demo(
    request: Request,
    email: str = Form(...),
    full_name: str = Form(...),
    company: str = Form(""),
    input_text: str = Form(...),
    accept_terms: str = Form(""),
    website: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    def _draft_cookie() -> str:
        return _json.dumps({
            "email": (email or "")[:255],
            "full_name": (full_name or "")[:120],
            "company": (company or "")[:120],
            "input_text": (input_text or "")[:2000],
        })

    def _preserve_redirect(url: str) -> RedirectResponse:
        resp = RedirectResponse(url, status_code=302)
        resp.set_cookie(
            "vynex_demo_draft", _draft_cookie(),
            httponly=True, secure=_COOKIE_SECURE, samesite="lax",
            max_age=60 * 60,  # 1h
        )
        return resp

    if website:
        # honeypot: silent drop (non dire al bot che l'abbiamo capito)
        return RedirectResponse("/demo?error=Errore+di+validazione", status_code=302)
    if accept_terms != "on":
        return _preserve_redirect("/demo?error=Devi+accettare+la+privacy+policy")
    try:
        valid = validate_email(email, check_deliverability=False)
        email_norm = valid.normalized.lower()
    except EmailNotValidError:
        return _preserve_redirect("/demo?error=Email+non+valida")
    if len(input_text.strip()) < 30:
        return _preserve_redirect("/demo?error=Descrivi+la+visita+in+almeno+30+caratteri")
    if len(full_name.strip()) < 2:
        return _preserve_redirect("/demo?error=Nome+non+valido")

    # Anti-abuse: 1 demo / 24h per email. Reindirizza al recovery senza bruciare quota Haiku.
    existing_lead = (await db.execute(
        select(Lead).where(Lead.email == email_norm)
    )).scalar_one_or_none()
    if existing_lead and existing_lead.demo_input and existing_lead.last_engaged_at:
        if (datetime.utcnow() - existing_lead.last_engaged_at) < timedelta(hours=24):
            try:
                base_url = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
                link = f"{base_url}/demo/result/{existing_lead.unsub_token}"
                await send_demo_recovery_email(existing_lead.email, existing_lead.full_name or "", link)
            except Exception:
                logger.exception("demo re-send failed lead=%s", existing_lead.id)
            resp = RedirectResponse(
                "/demo/recovery?message=Hai+gia%27+usato+la+demo+di+recente.+Ti+abbiamo+inviato+il+link+via+email.",
                status_code=302,
            )
            resp.delete_cookie("vynex_demo_draft")
            return resp

    try:
        docs = await genera_documenti(
            input_text.strip()[:2000],
            nome_agente=full_name.strip()[:120],
            azienda_mandante=company.strip()[:120],
        )
    except Exception:
        logger.exception("demo generation failed")
        return _preserve_redirect(
            "/demo?error=Generazione+non+riuscita.+Riprova+tra+2+minuti"
        )

    lead, _created = await upsert_lead(
        db,
        email=email_norm,
        full_name=full_name.strip(),
        company=company.strip() or None,
        source="demo",
    )
    lead.demo_input = _json.dumps({
        "input": input_text.strip()[:2000],
        "report_visita": docs["report_visita"],
        "email_followup": docs["email_followup"],
        "offerta_commerciale": docs["offerta_commerciale"],
        "cliente_nome": docs.get("cliente_nome", ""),
        "azienda_cliente": docs.get("azienda_cliente", ""),
    })
    lead.last_engaged_at = datetime.utcnow()
    await db.commit()

    # Invia sempre email coi 3 documenti: se chiude la tab, ha il link recovery.
    try:
        base_url = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
        result_link = f"{base_url}/demo/result/{lead.unsub_token}"
        await send_demo_recovery_email(lead.email, lead.full_name or "", result_link)
    except Exception:
        logger.exception("demo result email failed lead=%s", lead.id)

    try:
        await enroll_lead_in_sequence(db, lead, SEQUENCE_LEAD_DEMO)
    except Exception:
        logger.exception("demo sequence enroll failed lead=%s", lead.id)

    try:
        await save_source_attribution(
            db,
            lead_id=lead.id,
            utm_cookie=request.cookies.get("vynex_utm"),
            ip=(request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else ""))[:45],
            user_agent=request.headers.get("user-agent", "")[:500],
        )
    except Exception:
        logger.exception("save_source_attribution demo failed lead=%s", lead.id)

    response = RedirectResponse(f"/demo/result/{lead.unsub_token}", status_code=302)
    # Clear the draft cookie on success so next visit loads a clean form.
    response.delete_cookie("vynex_demo_draft")
    return response


@app.get("/demo/result/{token}", response_class=HTMLResponse)
async def demo_result(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Lead).where(Lead.unsub_token == token))
    lead = r.scalar_one_or_none()
    if lead is None or not lead.demo_input:
        return templates.TemplateResponse(
            "404.html", {"request": request}, status_code=404
        )
    try:
        data = _json.loads(lead.demo_input)
    except Exception:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    first_name = (lead.full_name or "").split(" ")[0] or lead.email.split("@")[0]
    return templates.TemplateResponse(
        "demo_result.html",
        {
            "request": request,
            "lead_first_name": first_name,
            "lead_unsub_token": lead.unsub_token,
            "report_visita": data.get("report_visita", ""),
            "email_followup": data.get("email_followup", ""),
            "offerta_commerciale": data.get("offerta_commerciale", ""),
        },
    )


@app.get("/demo/result/{token}/pdf")
async def demo_result_pdf(token: str, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Lead).where(Lead.unsub_token == token))
    lead = r.scalar_one_or_none()
    if lead is None or not lead.demo_input:
        raise HTTPException(404, "Documenti non disponibili")
    try:
        data = _json.loads(lead.demo_input)
    except Exception:
        raise HTTPException(404, "Dati corrotti")

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
    from reportlab.lib.enums import TA_LEFT

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        title=f"VYNEX — 3 documenti per {lead.full_name or lead.email}",
        author="VYNEX",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "VynexTitle", parent=styles["Heading1"],
        fontSize=22, leading=28, textColor=colors.HexColor("#0f172a"),
        spaceAfter=4, fontName="Helvetica-Bold",
    )
    sub_style = ParagraphStyle(
        "VynexSub", parent=styles["Normal"],
        fontSize=10, textColor=colors.HexColor("#64748b"),
        spaceAfter=18,
    )
    section_style = ParagraphStyle(
        "VynexSection", parent=styles["Heading2"],
        fontSize=14, leading=18, textColor=colors.HexColor("#1e40af"),
        spaceBefore=8, spaceAfter=10, fontName="Helvetica-Bold",
    )
    body_style = ParagraphStyle(
        "VynexBody", parent=styles["Normal"],
        fontSize=10.5, leading=16, textColor=colors.HexColor("#1e293b"),
        alignment=TA_LEFT, spaceAfter=4,
    )

    def _paragraphs(text: str) -> list:
        result = []
        for raw in (text or "").split("\n"):
            line = raw.strip()
            if not line:
                result.append(Spacer(1, 6))
                continue
            safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            result.append(Paragraph(safe, body_style))
        return result

    flow = []
    flow.append(Paragraph("VYNEX", title_style))
    flow.append(Paragraph(
        f"3 documenti generati per <b>{lead.full_name or lead.email}</b> · "
        f"{datetime.utcnow().strftime('%d/%m/%Y')}",
        sub_style,
    ))

    flow.append(Paragraph("Report di visita", section_style))
    flow.extend(_paragraphs(data.get("report_visita", "")))
    flow.append(PageBreak())

    flow.append(Paragraph("Email di follow-up", section_style))
    flow.extend(_paragraphs(data.get("email_followup", "")))
    flow.append(PageBreak())

    flow.append(Paragraph("Offerta commerciale", section_style))
    flow.extend(_paragraphs(data.get("offerta_commerciale", "")))

    flow.append(Spacer(1, 24))
    flow.append(Paragraph(
        '<font color="#64748b" size="9">Generato da VYNEX — '
        '<a href="https://vynex.it">vynex.it</a></font>',
        body_style,
    ))

    doc.build(flow)
    pdf = buf.getvalue()
    buf.close()

    filename = f"vynex-{(lead.full_name or lead.email).split(' ')[0].lower()}-{datetime.utcnow().strftime('%Y%m%d')}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/r/{code}")
@limiter.limit("30/minute")
async def referral_click_route(code: str, request: Request, db: AsyncSession = Depends(get_db)):
    code_norm = code.strip().upper()[:16]
    r = await db.execute(select(User).where(User.referral_code == code_norm))
    referrer = r.scalar_one_or_none()
    if referrer is not None and referrer.deleted_at is None:
        try:
            ip = (
                request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                or (request.client.host if request.client else "")
            )[:45]
            db.add(ReferralClick(
                referrer_user_id=referrer.id,
                ip=ip,
                user_agent=request.headers.get("user-agent", "")[:500],
                referer=request.headers.get("referer", "")[:500],
            ))
            await db.commit()
        except Exception:
            logger.exception("referral click log failed")
    response = RedirectResponse("/registrati", status_code=302)
    response.set_cookie(
        "vynex_ref", code_norm,
        httponly=True, secure=_COOKIE_SECURE, samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response


@app.get("/e/o/{job_id}/{sig}.gif")
@limiter.limit("60/minute")
async def tracking_open(job_id: int, sig: str, request: Request, db: AsyncSession = Depends(get_db)):
    if verify_sig(job_id, "open", sig):
        try:
            await db.execute(
                update(EmailJob)
                .where(EmailJob.id == job_id)
                .where(EmailJob.opened_at.is_(None))
                .values(opened_at=datetime.utcnow())
            )
            await db.commit()
        except Exception:
            logger.exception("tracking open update failed job=%s", job_id)
    return Response(
        content=_GIF_1x1,
        media_type="image/gif",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@app.get("/e/c/{job_id}/{sig}")
@limiter.limit("60/minute")
async def tracking_click(
    job_id: int, sig: str, request: Request, db: AsyncSession = Depends(get_db)
):
    target = request.query_params.get("u", "/")
    if not (target.startswith("http://") or target.startswith("https://") or target.startswith("/")):
        target = "/"
    if not verify_sig(job_id, "click", sig, target):
        if not verify_sig(job_id, "click", sig):
            return RedirectResponse("/", status_code=302)
        logger.warning("click tracking: legacy sig used job=%s, target tamper blocked", job_id)
        target = "/"
    try:
        await db.execute(
            update(EmailJob)
            .where(EmailJob.id == job_id)
            .where(EmailJob.clicked_at.is_(None))
            .values(clicked_at=datetime.utcnow())
        )
        await db.commit()
    except Exception:
        logger.exception("tracking click update failed job=%s", job_id)
    return RedirectResponse(target, status_code=302)


@app.get("/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe_route(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Lead).where(Lead.unsub_token == token))
    lead = r.scalar_one_or_none()
    if lead is None:
        return HTMLResponse(
            "<h1 style='font-family:sans-serif;padding:40px'>Token non valido o già usato.</h1>",
            status_code=404,
        )
    if not lead.unsubscribed:
        lead.unsubscribed = True
        lead.unsubscribed_at = datetime.utcnow()
        await db.commit()
    return HTMLResponse(
        "<!DOCTYPE html><html lang='it'><body style='font-family:system-ui;"
        "background:#04060f;color:#f1f5f9;min-height:100vh;display:flex;"
        "align-items:center;justify-content:center;padding:40px'>"
        "<div style='max-width:480px;text-align:center'>"
        "<h1 style='font-size:28px;margin:0 0 16px'>Disiscritto ✓</h1>"
        "<p style='color:#94a3b8;line-height:1.7'>Non riceverai più email di acquisizione VYNEX. "
        "Le email transazionali (reset password, fatture) continueranno se hai un account.</p>"
        "<p style='margin-top:32px'><a href='/' style='color:#60a5fa'>Torna a vynex.it</a></p>"
        "</div></body></html>"
    )


# ─── ADMIN acquisition tools ──────────────────────────────────────────────────

def _require_admin(request: Request) -> None:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    expected = os.getenv("ADMIN_TOKEN", "")
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(401, "Admin token non valido")


@app.post("/api/admin/leads/import")
async def admin_leads_import(request: Request, db: AsyncSession = Depends(get_db)):
    _require_admin(request)
    body = await request.body()
    if not body:
        raise HTTPException(400, "CSV vuoto")
    text = body.decode("utf-8", errors="replace")
    reader = _csv.DictReader(io.StringIO(text))
    if "email" not in (reader.fieldnames or []):
        raise HTTPException(400, "CSV deve avere header con almeno: email")

    from email_templates import SEQUENCE_COLD
    created = 0
    skipped = 0
    enrolled = 0
    for row in reader:
        email = (row.get("email") or "").strip().lower()
        if not email or "@" not in email:
            skipped += 1
            continue
        try:
            lead, new = await upsert_lead(
                db,
                email=email,
                full_name=row.get("full_name") or row.get("name"),
                company=row.get("company") or row.get("azienda"),
                source="cold",
                notes=row.get("notes"),
            )
            if new:
                created += 1
            n = await enroll_lead_in_sequence(db, lead, SEQUENCE_COLD)
            enrolled += n
        except Exception:
            logger.exception("cold lead import failed for %s", email)
            skipped += 1
    return {"created": created, "skipped": skipped, "jobs_enrolled": enrolled}


@app.get("/api/admin/acquisition/stats")
async def admin_acquisition_stats(request: Request, db: AsyncSession = Depends(get_db)):
    _require_admin(request)
    now = datetime.utcnow()
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)

    lead_total = (await db.execute(select(func.count(Lead.id)))).scalar() or 0
    lead_24h = (await db.execute(
        select(func.count(Lead.id)).where(Lead.created_at >= day_ago)
    )).scalar() or 0
    lead_by_source_q = await db.execute(
        select(Lead.source, func.count(Lead.id)).group_by(Lead.source)
    )
    lead_by_source = {src: cnt for src, cnt in lead_by_source_q.all()}

    jobs_pending = (await db.execute(
        select(func.count(EmailJob.id)).where(EmailJob.sent_at.is_(None))
    )).scalar() or 0
    jobs_sent_24h = (await db.execute(
        select(func.count(EmailJob.id))
        .where(EmailJob.sent_at >= day_ago)
        .where(EmailJob.error.is_(None))
    )).scalar() or 0
    jobs_failed_24h = (await db.execute(
        select(func.count(EmailJob.id))
        .where(EmailJob.sent_at >= day_ago)
        .where(EmailJob.error.is_not(None))
    )).scalar() or 0
    jobs_opened_7d = (await db.execute(
        select(func.count(EmailJob.id)).where(EmailJob.opened_at >= week_ago)
    )).scalar() or 0
    jobs_clicked_7d = (await db.execute(
        select(func.count(EmailJob.id)).where(EmailJob.clicked_at >= week_ago)
    )).scalar() or 0

    referrals_total = (await db.execute(
        select(func.count(User.id)).where(User.referred_by_id.is_not(None))
    )).scalar() or 0
    referrals_paying = (await db.execute(
        select(func.count(User.id))
        .where(User.referred_by_id.is_not(None))
        .where(User.plan.in_(("pro", "team")))
    )).scalar() or 0

    return {
        "generated_at": now.isoformat() + "Z",
        "leads": {
            "total": lead_total,
            "last_24h": lead_24h,
            "by_source": lead_by_source,
        },
        "email_jobs": {
            "pending": jobs_pending,
            "sent_24h": jobs_sent_24h,
            "failed_24h": jobs_failed_24h,
            "opened_7d": jobs_opened_7d,
            "clicked_7d": jobs_clicked_7d,
        },
        "referrals": {
            "total_signups": referrals_total,
            "paying": referrals_paying,
        },
    }


@app.post("/api/admin/acquisition/tick")
async def admin_acquisition_tick(request: Request, db: AsyncSession = Depends(get_db)):
    _require_admin(request)
    return await process_email_queue(db)


@app.post("/api/admin/acquisition/retry-all")
async def admin_acquisition_retry_all(request: Request, db: AsyncSession = Depends(get_db)):
    """Sblocca tutti i job email pending — reset retry_count, next_retry_at, error.
    Da usare dopo che il provider email (Brevo/Resend) e' stato attivato."""
    _require_admin(request)
    unlocked = await reset_all_retries(db)
    processed = await process_email_queue(db)
    return {"unlocked": unlocked, "immediately_processed": processed}


@app.post("/api/admin/maintenance/run")
async def admin_maintenance_run(request: Request, db: AsyncSession = Depends(get_db)):
    _require_admin(request)
    from maintenance import run_all_maintenance
    return await run_all_maintenance(db)


@app.post("/api/admin/stripe/reconcile")
async def admin_stripe_reconcile(request: Request, db: AsyncSession = Depends(get_db)):
    _require_admin(request)
    from maintenance import reconcile_stripe_subscriptions
    return await reconcile_stripe_subscriptions(db)


@app.get("/api/admin/system/health")
async def admin_system_health(request: Request, db: AsyncSession = Depends(get_db)):
    """Unified health radar: email queue, subscriptions, cleanup backlog, services."""
    _require_admin(request)
    now = datetime.utcnow()

    pending = (await db.execute(
        select(func.count(EmailJob.id)).where(EmailJob.sent_at.is_(None))
    )).scalar() or 0

    retrying = (await db.execute(
        select(func.count(EmailJob.id))
        .where(EmailJob.sent_at.is_(None))
        .where(EmailJob.retry_count > 0)
    )).scalar() or 0

    permanently_failed = (await db.execute(
        select(func.count(EmailJob.id))
        .where(EmailJob.sent_at.is_(None))
        .where(EmailJob.error.like("MAX_RETRY%"))
    )).scalar() or 0

    past_due_users = (await db.execute(
        select(func.count(User.id))
        .where(User.subscription_status == "past_due")
    )).scalar() or 0

    disputed_users = (await db.execute(
        select(func.count(User.id))
        .where(User.is_active == False)
        .where(User.deleted_at.is_(None))
    )).scalar() or 0

    expired_tokens = (await db.execute(
        select(func.count(EmailVerificationToken.id))
        .where(EmailVerificationToken.expires_at < now)
        .where(EmailVerificationToken.used_at.is_(None))
    )).scalar() or 0

    docs_to_purge = (await db.execute(
        select(func.count(Document.id))
        .where(Document.deleted_at.is_not(None))
        .where(Document.deleted_at < now - timedelta(days=30))
    )).scalar() or 0

    return {
        "generated_at": now.isoformat() + "Z",
        "email_queue": {
            "pending": pending,
            "retrying": retrying,
            "permanently_failed": permanently_failed,
        },
        "subscriptions": {
            "past_due": past_due_users,
            "frozen_accounts": disputed_users,
        },
        "cleanup_backlog": {
            "expired_tokens": expired_tokens,
            "docs_to_purge": docs_to_purge,
        },
        "services": {
            "anthropic": "set" if os.getenv("ANTHROPIC_API_KEY") else "missing",
            "stripe": "set" if os.getenv("STRIPE_SECRET_KEY") else "missing",
            "email": "set" if (os.getenv("RESEND_API_KEY") or os.getenv("BREVO_API_KEY")) else "missing",
        },
    }


# ─── BLOG SEO ──────────────────────────────────────────────────────────────────

@app.get("/blog", response_class=HTMLResponse)
async def blog_index(request: Request, db: AsyncSession = Depends(get_db)):
    q = await db.execute(
        select(BlogPost)
        .where(BlogPost.published.is_(True))
        .order_by(BlogPost.published_at.desc())
        .limit(50)
    )
    posts = q.scalars().all()
    return templates.TemplateResponse(
        "blog_index.html", {"request": request, "posts": posts}
    )


@app.get("/blog/{slug}", response_class=HTMLResponse)
async def blog_article(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    q = await db.execute(
        select(BlogPost).where(BlogPost.slug == slug).where(BlogPost.published.is_(True))
    )
    post = q.scalar_one_or_none()
    if post is None:
        return templates.TemplateResponse(
            "404.html", {"request": request}, status_code=404
        )
    return templates.TemplateResponse(
        "blog_article.html", {"request": request, "post": post}
    )


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(title: str) -> str:
    s = _SLUG_RE.sub("-", (title or "").lower().strip()).strip("-")
    return s[:100] or "articolo"


@app.post("/api/admin/blog/generate")
async def admin_blog_generate(request: Request, db: AsyncSession = Depends(get_db)):
    """Genera un articolo SEO via Claude Haiku su una keyword long-tail.

    Body JSON: {"keyword": "...", "audience": "...", "angle": "..."}
    """
    _require_admin(request)
    payload = await request.json()
    keyword = (payload.get("keyword") or "").strip()[:200]
    audience = (payload.get("audience") or "agenti di commercio italiani").strip()[:200]
    angle = (payload.get("angle") or "guida pratica con esempi concreti").strip()[:200]
    if not keyword:
        raise HTTPException(400, "keyword richiesta")

    import anthropic
    client = anthropic.AsyncAnthropic()
    prompt = f"""Scrivi un articolo SEO in italiano professionale per il blog di VYNEX (SaaS AI per agenti commerciali italiani).

Keyword principale: "{keyword}"
Audience: {audience}
Angolo: {angle}

Requisiti OBBLIGATORI:
- Lingua: italiano professionale nativo
- Lunghezza: 900-1400 parole
- Struttura: H2 + H3 + paragrafi + 1-2 liste puntate
- Tono: concreto, utile, zero marketing fluff
- Target SEO: long-tail per "{keyword}"
- Includi almeno 2 esempi concreti di agente italiano (nomi realistici, numeri, settore)
- Chiudi con CTA soft verso /demo o /registrati
- body_html: HTML puro (no markdown). Tag consentiti: h2, h3, p, ul, ol, li, strong, em, a, blockquote.

Chiama lo strumento save_blog_post con tutti i campi compilati."""

    tools = [{
        "name": "save_blog_post",
        "description": "Salva l'articolo blog generato nel database.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Titolo SEO (50-65 char)"},
                "meta_description": {"type": "string", "description": "Meta description SEO (140-160 char)"},
                "hero_subtitle": {"type": "string", "description": "Sottotitolo hero (80-140 char)"},
                "body_html": {"type": "string", "description": "Body HTML puro dell'articolo (900-1400 parole)"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "3-6 tag"},
                "reading_minutes": {"type": "integer", "description": "Tempo lettura in minuti (3-10)"},
            },
            "required": ["title", "meta_description", "body_html", "tags", "reading_minutes"],
        },
    }]

    try:
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            tools=tools,
            tool_choice={"type": "tool", "name": "save_blog_post"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        logger.exception("blog generate: Anthropic call failed")
        raise HTTPException(502, f"AI error: {exc}")

    data = None
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "save_blog_post":
            data = block.input
            break
    if data is None:
        raise HTTPException(502, "AI did not invoke save_blog_post tool")

    title = (data.get("title") or keyword.title())[:200]
    slug_base = _slugify(title)
    # Ensure unique
    slug = slug_base
    suffix = 1
    while True:
        exists = await db.execute(select(BlogPost.id).where(BlogPost.slug == slug))
        if exists.scalar_one_or_none() is None:
            break
        suffix += 1
        slug = f"{slug_base}-{suffix}"[:120]

    # Sanitize body_html: whitelist rigorosa su output Claude tool-use per prevenire XSS stored.
    # Zero-deps: HTMLParser stdlib + whitelist tag/attr/protocol.
    from html.parser import HTMLParser as _HTMLParser
    from html import escape as _html_escape
    _BLOG_ALLOWED_TAGS = {"h2", "h3", "h4", "p", "ul", "ol", "li", "strong", "em", "a", "blockquote", "br", "code", "pre"}
    _BLOG_ALLOWED_ATTRS = {"a": {"href", "title", "rel"}}
    _BLOG_ALLOWED_PROTOCOLS = ("http://", "https://", "mailto:", "/", "#")

    class _BlogSanitizer(_HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.out = []
        def handle_starttag(self, tag, attrs):
            if tag not in _BLOG_ALLOWED_TAGS:
                return
            allowed = _BLOG_ALLOWED_ATTRS.get(tag, set())
            safe_attrs = []
            for k, v in attrs:
                if k not in allowed or v is None:
                    continue
                if k == "href":
                    vl = v.strip().lower()
                    if not any(vl.startswith(p) for p in _BLOG_ALLOWED_PROTOCOLS):
                        continue
                safe_attrs.append(f'{k}="{_html_escape(v, quote=True)}"')
            if tag == "a":
                safe_attrs.append('rel="nofollow noopener"')
            self.out.append(f"<{tag}{(' ' + ' '.join(safe_attrs)) if safe_attrs else ''}>")
        def handle_endtag(self, tag):
            if tag in _BLOG_ALLOWED_TAGS:
                self.out.append(f"</{tag}>")
        def handle_startendtag(self, tag, attrs):
            if tag == "br":
                self.out.append("<br>")
        def handle_data(self, data):
            self.out.append(_html_escape(data, quote=False))

    raw_body = data.get("body_html") or "<p>(vuoto)</p>"
    _s = _BlogSanitizer()
    _s.feed(raw_body)
    safe_body = "".join(_s.out) or "<p>(vuoto)</p>"

    post = BlogPost(
        slug=slug,
        title=title,
        meta_description=(data.get("meta_description") or "")[:300],
        hero_subtitle=(data.get("hero_subtitle") or "")[:300] or None,
        body_html=safe_body,
        keyword_primary=keyword[:120],
        tags_csv=",".join(data.get("tags") or [])[:255],
        published=True,
        published_at=datetime.utcnow(),
        reading_minutes=int(data.get("reading_minutes") or 5),
    )
    db.add(post)
    await db.commit()
    return {
        "slug": slug,
        "title": title,
        "url": f"{os.getenv('BASE_URL', '').rstrip('/')}/blog/{slug}",
    }


# ─── API v1 PUBBLICA ──────────────────────────────────────────────────────────

def _api_key_hash(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()


async def _auth_api_key(request: Request, db: AsyncSession) -> User:
    raw = request.headers.get("x-api-key", "").strip()
    if not raw or not raw.startswith("vx_"):
        raise HTTPException(401, "X-API-Key mancante o non valida")
    h = _api_key_hash(raw)
    r = await db.execute(
        select(APIKey).where(APIKey.key_hash == h).where(APIKey.revoked_at.is_(None))
    )
    api_key = r.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(401, "API key non valida o revocata")
    api_key.last_used_at = datetime.utcnow()
    await db.commit()
    ur = await db.execute(select(User).where(User.id == api_key.user_id))
    user = ur.scalar_one_or_none()
    if user is None or user.deleted_at is not None or not user.is_active:
        raise HTTPException(403, "Utente disabilitato")
    return user


@app.post("/api/v1/documents/generate")
@limiter.limit("60/minute")
async def api_v1_generate(request: Request, db: AsyncSession = Depends(get_db)):
    """Genera 3 documenti commerciali — API pubblica v1.

    Headers:
      X-API-Key: vx_<secret>

    Body JSON:
      {
        "input_text": "descrizione visita (30-2000 char)",
        "nome_agente": "Mario Rossi",
        "azienda_mandante": "Acme Srl" (optional)
      }

    Response 200:
      {
        "document_id": 123,
        "cliente_nome": "...",
        "azienda_cliente": "...",
        "report_visita": "...",
        "email_followup": "...",
        "offerta_commerciale": "...",
        "tokens_used": 1234,
        "generation_time_ms": 28000
      }
    """
    user = await _auth_api_key(request, db)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Body JSON richiesto")
    input_text = (payload.get("input_text") or "").strip()
    nome_agente = (payload.get("nome_agente") or user.full_name or "").strip()
    azienda_mandante = (payload.get("azienda_mandante") or "").strip()
    if len(input_text) < 30:
        raise HTTPException(400, "input_text deve essere >=30 caratteri")
    if len(input_text) > 2000:
        raise HTTPException(400, "input_text deve essere <=2000 caratteri")

    # quota check — free tier 10/mese
    usage = await get_monthly_usage(db, user.id)
    if user.plan == "free" and usage >= user.monthly_limit:
        raise HTTPException(429, "Quota mensile piano Free esaurita. Upgrade a Pro.")

    try:
        docs = await genera_documenti(
            input_text[:2000],
            nome_agente=nome_agente[:120],
            azienda_mandante=azienda_mandante[:120],
        )
    except Exception:
        logger.exception("api/v1 generation failed user=%s", user.id)
        raise HTTPException(502, "Generazione AI non riuscita")

    doc = Document(
        user_id=user.id,
        input_text=input_text,
        report_visita=docs["report_visita"],
        email_followup=docs["email_followup"],
        offerta_commerciale=docs["offerta_commerciale"],
        cliente_nome=(docs.get("cliente_nome") or "")[:255] or None,
        azienda_cliente=(docs.get("azienda_cliente") or "")[:255] or None,
        tokens_used=docs.get("tokens_used"),
        generation_time_ms=docs.get("generation_time_ms"),
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    return {
        "document_id": doc.id,
        "cliente_nome": doc.cliente_nome,
        "azienda_cliente": doc.azienda_cliente,
        "report_visita": doc.report_visita,
        "email_followup": doc.email_followup,
        "offerta_commerciale": doc.offerta_commerciale,
        "tokens_used": doc.tokens_used,
        "generation_time_ms": doc.generation_time_ms,
    }


@app.get("/api/v1/health")
async def api_v1_health():
    return {"status": "ok", "version": "1.0.0", "service": "vynex-api"}


@app.post("/api/account/api-keys/create")
@limiter.limit("10/hour")
async def api_keys_create(
    request: Request,
    name: str = Form(""),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Genera nuova API key — mostrata una sola volta in clear-text."""
    import secrets as _sec
    raw = "vx_" + _sec.token_urlsafe(32)
    prefix = raw[:12]
    key = APIKey(
        user_id=user.id,
        name=(name or "API Key")[:120],
        prefix=prefix,
        key_hash=_api_key_hash(raw),
    )
    db.add(key)
    await db.commit()
    return {"key": raw, "prefix": prefix, "name": key.name}


@app.post("/api/account/api-keys/{key_id}/revoke")
async def api_keys_revoke(
    key_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    r = await db.execute(
        update(APIKey)
        .where(APIKey.id == key_id)
        .where(APIKey.user_id == user.id)
        .where(APIKey.revoked_at.is_(None))
        .values(revoked_at=datetime.utcnow())
        .returning(APIKey.id)
    )
    await db.commit()
    return {"revoked": r.scalar_one_or_none() is not None}


@app.get("/api/account/api-keys")
async def api_keys_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    r = await db.execute(
        select(APIKey).where(APIKey.user_id == user.id).order_by(APIKey.created_at.desc())
    )
    return [
        {
            "id": k.id, "name": k.name, "prefix": k.prefix,
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
            "created_at": k.created_at.isoformat() if k.created_at else None,
        }
        for k in r.scalars().all()
    ]


# ─── NPS SURVEY ────────────────────────────────────────────────────────────────

def _nps_sig(user_id: int, tag: str) -> str:
    msg = f"nps:{user_id}:{tag}".encode()
    secret = os.getenv("SECRET_KEY", "").encode()
    import hashlib as _h
    return hmac.new(secret, msg, _h.sha256).hexdigest()[:16]


@app.get("/nps", response_class=HTMLResponse)
async def nps_page(
    request: Request,
    u: int = 0,
    t: str = "t7",
    s: int | None = None,
    sig: str = "",
    db: AsyncSession = Depends(get_db),
):
    if not hmac.compare_digest(_nps_sig(u, t), sig):
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    r = await db.execute(select(User).where(User.id == u))
    user = r.scalar_one_or_none()
    if user is None or user.deleted_at is not None:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    first = (user.full_name or "").split(" ")[0] or user.email.split("@")[0]
    ctx = {
        "request": request, "user_id": u, "tag": t, "sig": sig,
        "user_first_name": first, "saved": False, "score": None,
    }
    if s is not None and 0 <= s <= 10:
        # Upsert NPS response
        existing_q = await db.execute(
            select(NPSResponse).where(NPSResponse.user_id == u).where(NPSResponse.survey_tag == t)
        )
        existing = existing_q.scalar_one_or_none()
        if existing is None:
            db.add(NPSResponse(user_id=u, survey_tag=t, score=s, responded_at=datetime.utcnow()))
        else:
            existing.score = s
            existing.responded_at = datetime.utcnow()
        await db.commit()
        ctx["saved"] = True
        ctx["score"] = s
    return templates.TemplateResponse("nps.html", ctx)


@app.post("/api/nps")
@limiter.limit("20/hour")
async def api_nps(
    request: Request,
    u: int = Form(...),
    t: str = Form(...),
    s: int = Form(...),
    sig: str = Form(...),
    comment: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if not hmac.compare_digest(_nps_sig(u, t), sig):
        raise HTTPException(403, "Sig non valida")
    r = await db.execute(
        update(NPSResponse)
        .where(NPSResponse.user_id == u)
        .where(NPSResponse.survey_tag == t)
        .values(score=s, comment=comment[:1000] or None, responded_at=datetime.utcnow())
        .returning(NPSResponse.id)
    )
    await db.commit()
    if r.scalar_one_or_none() is None:
        raise HTTPException(404, "Risposta non trovata — richiedi di nuovo il link")
    return RedirectResponse(f"/nps?u={u}&t={t}&s={s}&sig={sig}", status_code=303)


@app.get("/api/admin/nps/stats")
async def admin_nps_stats(request: Request, db: AsyncSession = Depends(get_db)):
    _require_admin(request)
    total_q = await db.execute(
        select(func.count(NPSResponse.id)).where(NPSResponse.responded_at.is_not(None))
    )
    total = total_q.scalar() or 0
    if total == 0:
        return {"total": 0, "nps": None, "buckets": {"promoters": 0, "passives": 0, "detractors": 0}, "avg": None}
    promoters_q = await db.execute(
        select(func.count(NPSResponse.id)).where(NPSResponse.score >= 9)
    )
    passives_q = await db.execute(
        select(func.count(NPSResponse.id))
        .where(NPSResponse.score >= 7).where(NPSResponse.score <= 8)
    )
    detractors_q = await db.execute(
        select(func.count(NPSResponse.id)).where(NPSResponse.score <= 6)
    )
    avg_q = await db.execute(
        select(func.avg(NPSResponse.score)).where(NPSResponse.responded_at.is_not(None))
    )
    p = promoters_q.scalar() or 0
    pa = passives_q.scalar() or 0
    d = detractors_q.scalar() or 0
    nps = round((p - d) / total * 100, 1)
    return {
        "total": total,
        "nps": nps,
        "buckets": {"promoters": p, "passives": pa, "detractors": d},
        "avg": float(avg_q.scalar() or 0),
    }


# ─── LEAD MAGNET ──────────────────────────────────────────────────────────────

_CHECKLIST_CONTENT = {
    "title": "Checklist 20 punti: la visita commerciale perfetta",
    "subtitle": "Dall'agenda pre-visita al follow-up, cosa fare prima/durante/dopo — validato su reti vendita italiane.",
    "sections": [
        {
            "heading": "PRIMA DELLA VISITA",
            "icon": "clock",
            "time": "30 min in ufficio",
            "intro": "La vittoria si prepara prima. Un agente che va in visita senza obiettivo chiude il 23% in meno — dato ENASARCO 2024. 5 mosse per trasformare una visita qualunque in una visita con missione.",
            "case": {
                "title": "Caso reale — Agente utensileria, Bergamo",
                "before": "5 visite/giorno, 1 chiusura/settimana",
                "after": "5 visite/giorno, 3 chiusure/settimana dopo 30gg checklist",
                "key": "Il cambiamento e' arrivato applicando il punto 02: un solo obiettivo SMART per visita.",
            },
            "items": [
                "Rileggi il fascicolo cliente: storico ordini, ultimi 3 contatti, eventuali insoluti aperti.",
                "Definisci UN solo obiettivo SMART per la visita (es. presentare linea primavera, chiudere rinnovo).",
                "Prepara 2-3 ipotesi di offerta con condizioni alternative (prezzo vs pagamento vs volumi).",
                "Controlla la posizione geografica e il percorso — arriva 10 minuti prima, entra 2 minuti prima.",
                "Pianifica il tempo: 15% introduzione, 40% ascolto, 30% proposta, 15% chiusura.",
            ],
        },
        {
            "heading": "DURANTE LA VISITA",
            "icon": "speaker",
            "time": "45-60 min col cliente",
            "intro": "Ascolta piu' di quanto parli: gli agenti che mantengono 60% ascolto / 40% parlato chiudono il doppio degli altri. 10 tecniche per estrarre informazioni, quantificare il valore e chiudere con una condizionale.",
            "case": {
                "title": "Caso reale — Informatore farmaceutico, Milano",
                "before": "Prezzo come primo argomento → 1 chiusura su 8",
                "after": "Budget rilevato PRIMA (punto 07) → 1 chiusura su 3",
                "key": "Chiedere il budget prima di parlare di prezzi cambia la percezione del valore.",
            },
            "items": [
                "Apri chiedendo un dato concreto: 'Come va il magazzino di [prodotto]?' — subito pain rilevato.",
                "Rileva il budget disponibile PRIMA di parlare di prezzi. 'Che budget avete previsto quest'anno per...?'",
                "Prendi nota di almeno 3 dati quantitativi: prezzo competitor, volumi attuali, tempi di riassortimento.",
                "Identifica chi decide davvero (non sempre coincide con chi riceve la visita).",
                "Quantifica il pain: 'Quanto vi costa questo problema al mese?' — rende tangibile la tua soluzione.",
                "Proponi sempre 2 opzioni (no 1 sola, no 4+). Il cervello umano sceglie meglio con 2.",
                "Concorda un prossimo step SPECIFICO con data e ora, non 'ci sentiamo'.",
                "Chiudi con domanda condizionale: 'Se le condizioni fossero X, firmereste oggi?'",
                "Lascia sempre 1 documento fisico (brochure, catalogo, sample): materializza il ricordo della visita.",
                "Prendi note visibili durante il colloquio — il cliente si sente ascoltato e ti prende piu' seriamente.",
            ],
        },
        {
            "heading": "DOPO LA VISITA",
            "icon": "paper",
            "time": "20-40 min in 48h",
            "intro": "La differenza tra chi vince e chi perde. Il 68% degli agenti italiani perde il contratto per ritardo nel follow-up (fonte Fnaarc 2023). 5 azioni nelle 48 ore successive che decidono davvero la chiusura.",
            "case": {
                "title": "Caso reale — Agente food, Palermo",
                "before": "Follow-up a ~5 giorni → tasso risposta 11%",
                "after": "Follow-up entro 2h (punto 17) → tasso risposta 47%",
                "key": "Ogni giorno di ritardo dimezza la probabilita' di risposta.",
            },
            "items": [
                "Entro 30 minuti: scrivi 3 bullet point sul telefono (chi, cosa ha detto, next step).",
                "Entro 2 ore: invia email di follow-up con riepilogo dei punti discussi e conferma prossimo step.",
                "Entro 24 ore: report al mandante se sei plurimandatario (serve per tracciabilita' e continuita').",
                "Entro 48 ore: invia la proposta scritta se richiesta — piu' aspetti, piu' cala il tasso di risposta.",
                "Entro 7 giorni: chiamata di follow-up se nessuna risposta. Max 3 follow-up poi archivi il lead.",
            ],
        },
    ],
}


def _render_checklist_pdf(lead_name: str | None = None) -> bytes:
    """PDF premium editoriale: cover gradient + watermark sezioni + item card con check +
    bonus page + CTA finale. ReportLab-only (Helvetica), safe su Railway."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (
        BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer,
        PageBreak, Table, TableStyle, KeepTogether, Flowable,
    )
    from reportlab.lib.enums import TA_LEFT, TA_CENTER

    PAGE_W, PAGE_H = A4
    BLUE = colors.HexColor("#3b82f6")
    BLUE_L = colors.HexColor("#60a5fa")
    BLUE_XL = colors.HexColor("#93c5fd")
    PURPLE = colors.HexColor("#8b5cf6")
    PINK = colors.HexColor("#f472b6")
    GREEN = colors.HexColor("#10b981")
    INK = colors.HexColor("#0f172a")
    INK_SOFT = colors.HexColor("#334155")
    MUTED = colors.HexColor("#64748b")
    CARD_BG = colors.HexColor("#f8fafc")
    CARD_BG_BLUE = colors.HexColor("#eff6ff")
    DARK_BG = colors.HexColor("#04060f")
    DARK_CARD = colors.HexColor("#0b1020")

    # Shared state for body pages: current section number for watermark
    _state = {"section_idx": 0, "section_total": 3}

    buf = io.BytesIO()

    def cover_page(c, doc):
        c.saveState()
        # Full-bleed dark bg
        c.setFillColor(DARK_BG)
        c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
        # Aurora-like gradient glow top-left (multiple translucent circles simulate blur)
        for r, a in [(9 * cm, 0.15), (6 * cm, 0.12), (3.5 * cm, 0.10)]:
            c.setFillColor(BLUE)
            c.setFillAlpha(a)
            c.circle(3 * cm, PAGE_H - 3 * cm, r, fill=1, stroke=0)
        for r, a in [(8 * cm, 0.12), (5 * cm, 0.10)]:
            c.setFillColor(PURPLE)
            c.setFillAlpha(a)
            c.circle(PAGE_W - 2 * cm, PAGE_H - 8 * cm, r, fill=1, stroke=0)
        c.setFillAlpha(1.0)

        # Gradient bands top (3 thin stripes)
        for i, col in enumerate([BLUE, PURPLE, PINK]):
            c.setFillColor(col)
            c.setFillAlpha(0.85 - i * 0.12)
            c.rect(0, PAGE_H - 4 - i * 3, PAGE_W, 2.5, fill=1, stroke=0)
        c.setFillAlpha(1.0)

        # Huge "20" watermark (background)
        c.setFillColor(colors.HexColor("#60a5fa"))
        c.setFillAlpha(0.06)
        c.setFont("Helvetica-Bold", 380)
        c.drawRightString(PAGE_W - 0.5 * cm, PAGE_H / 2 - 6 * cm, "20")
        c.setFillAlpha(1.0)

        # Logo wordmark
        c.setFillColor(BLUE_L)
        c.setFont("Helvetica-Bold", 30)
        c.drawString(2 * cm, PAGE_H - 3.8 * cm, "VYNEX")
        c.setFillColor(MUTED)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(2 * cm, PAGE_H - 4.5 * cm, "TU CHIUDI.  VYNEX SCRIVE.")

        # Edition tag top-right
        c.setFillColor(colors.HexColor("#1e3a8a"))
        c.setFillAlpha(0.4)
        c.roundRect(PAGE_W - 5.2 * cm, PAGE_H - 4.0 * cm, 3.2 * cm, 0.7 * cm, 0.35 * cm, fill=1, stroke=0)
        c.setFillAlpha(1.0)
        c.setStrokeColor(colors.HexColor("#1d4ed8"))
        c.setLineWidth(0.8)
        c.roundRect(PAGE_W - 5.2 * cm, PAGE_H - 4.0 * cm, 3.2 * cm, 0.7 * cm, 0.35 * cm, fill=0, stroke=1)
        c.setFillColor(BLUE_XL)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(PAGE_W - 5.0 * cm, PAGE_H - 3.55 * cm, "EDIZIONE 2026")

        # Kicker pill
        c.setFillColor(colors.HexColor("#1e3a8a"))
        c.roundRect(2 * cm, PAGE_H - 7.2 * cm, 5.8 * cm, 0.8 * cm, 0.4 * cm, fill=1, stroke=0)
        c.setFillColor(BLUE_XL)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(2.4 * cm, PAGE_H - 6.7 * cm, "GUIDA PRATICA  ·  PDF GRATIS  ·  3 PAGINE")

        # Big title
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 40)
        c.drawString(2 * cm, PAGE_H - 10.3 * cm, "La visita")
        c.drawString(2 * cm, PAGE_H - 12.5 * cm, "commerciale")
        c.setFillColor(BLUE_XL)
        c.drawString(2 * cm, PAGE_H - 14.7 * cm, "perfetta.")
        # Number highlight 20 punti
        c.setFillColor(colors.white)
        c.setFont("Helvetica", 20)
        c.drawString(2 * cm, PAGE_H - 16.4 * cm, "in ")
        c.setFillColor(BLUE_L)
        c.setFont("Helvetica-Bold", 22)
        c.drawString(3.1 * cm, PAGE_H - 16.4 * cm, "20 punti")
        c.setFillColor(colors.white)
        c.setFont("Helvetica", 20)
        c.drawString(6.5 * cm, PAGE_H - 16.4 * cm, "azionabili oggi.")

        # Subtitle
        c.setFillColor(colors.HexColor("#cbd5e1"))
        c.setFont("Helvetica", 11.5)
        c.drawString(2 * cm, PAGE_H - 18.0 * cm, "Checklist testata su reti vendita italiane reali: cosa fare")
        c.drawString(2 * cm, PAGE_H - 18.6 * cm, "prima, durante e dopo la visita per chiudere piu' contratti.")

        # Stats row (3 boxes)
        box_w, box_h, gap = 5.2 * cm, 2.6 * cm, 0.5 * cm
        box_y = PAGE_H - 22.0 * cm
        stats = [
            ("20", "PUNTI AZIONE", "ciascuno con esempio concreto"),
            ("3", "FASI CHIAVE", "prima, durante, dopo"),
            ("3 ORE", "RISPARMIATE/DIE", "se applichi la checklist"),
        ]
        for i, (big, small, tiny) in enumerate(stats):
            x = 2 * cm + i * (box_w + gap)
            c.setFillColor(colors.HexColor("#0f172a"))
            c.setStrokeColor(colors.HexColor("#1e40af"))
            c.setLineWidth(1.2)
            c.roundRect(x, box_y, box_w, box_h, 0.45 * cm, fill=1, stroke=1)
            c.setFillColor(BLUE_L)
            c.setFont("Helvetica-Bold", 26)
            c.drawString(x + 0.7 * cm, box_y + 1.4 * cm, big)
            c.setFillColor(colors.HexColor("#e2e8f0"))
            c.setFont("Helvetica-Bold", 8.5)
            c.drawString(x + 0.7 * cm, box_y + 0.85 * cm, small)
            c.setFillColor(MUTED)
            c.setFont("Helvetica", 7.5)
            c.drawString(x + 0.7 * cm, box_y + 0.35 * cm, tiny)

        # Footer cover
        c.setStrokeColor(colors.HexColor("#1e293b"))
        c.setLineWidth(0.5)
        c.line(2 * cm, 2.4 * cm, PAGE_W - 2 * cm, 2.4 * cm)
        c.setFillColor(BLUE_L)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(2 * cm, 1.7 * cm, "vynex.it")
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 9)
        c.drawString(3.3 * cm, 1.7 * cm, "·  Intelligenza Artificiale italiana per agenti commerciali")
        if lead_name:
            c.setFillColor(colors.HexColor("#94a3b8"))
            c.setFont("Helvetica-Oblique", 9)
            c.drawRightString(PAGE_W - 2 * cm, 1.7 * cm, f"Preparata per {lead_name}")
        c.restoreState()

    def body_page(c, doc):
        c.saveState()
        # Accent bar top (3 thin stripes gradient)
        for i, col in enumerate([BLUE, PURPLE, PINK]):
            c.setFillColor(col)
            c.setFillAlpha(0.9 - i * 0.15)
            c.rect(0, PAGE_H - 3 - i * 2, PAGE_W, 2, fill=1, stroke=0)
        c.setFillAlpha(1.0)

        # Ghost section number as watermark (bottom right, subtle)
        sec = _state.get("section_idx", 0)
        if sec > 0:
            c.setFillColor(BLUE_L)
            c.setFillAlpha(0.04)
            c.setFont("Helvetica-Bold", 300)
            c.drawRightString(PAGE_W - 0.5 * cm, 1.5 * cm, f"{sec:02d}")
            c.setFillAlpha(1.0)

        # Header: VYNEX + path
        c.setFillColor(BLUE_L)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(2 * cm, PAGE_H - 1.5 * cm, "VYNEX")
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 8.5)
        c.drawString(3.3 * cm, PAGE_H - 1.5 * cm, "·  La visita commerciale perfetta in 20 punti")
        c.setFillColor(BLUE_L)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawRightString(PAGE_W - 2 * cm, PAGE_H - 1.5 * cm, "vynex.it")

        # Footer: slogan + page number
        c.setStrokeColor(colors.HexColor("#e2e8f0"))
        c.setLineWidth(0.5)
        c.line(2 * cm, 1.8 * cm, PAGE_W - 2 * cm, 1.8 * cm)
        c.setFillColor(BLUE_L)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(2 * cm, 1.15 * cm, "TU CHIUDI.")
        c.setFillColor(INK)
        c.drawString(4.5 * cm, 1.15 * cm, "VYNEX SCRIVE.")
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 8)
        c.drawRightString(PAGE_W - 2 * cm, 1.15 * cm, f"Pagina {doc.page}")
        c.restoreState()

    doc = BaseDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2.4 * cm, bottomMargin=2.4 * cm,
        title=_CHECKLIST_CONTENT["title"], author="VYNEX",
    )
    frame_body = Frame(
        2 * cm, 2.4 * cm, PAGE_W - 4 * cm, PAGE_H - 4.8 * cm,
        id="body", showBoundary=0,
    )
    doc.addPageTemplates([
        PageTemplate(id="cover", frames=[frame_body], onPage=cover_page),
        PageTemplate(id="body", frames=[frame_body], onPage=body_page),
    ])

    section_title_style = ParagraphStyle(
        "Sec", fontSize=20, leading=26, textColor=INK,
        fontName="Helvetica-Bold", spaceBefore=0, spaceAfter=8,
    )
    section_kicker_style = ParagraphStyle(
        "SecK", fontSize=9.5, leading=14, textColor=BLUE,
        fontName="Helvetica-Bold", spaceBefore=0, spaceAfter=6,
    )
    section_intro_style = ParagraphStyle(
        "SecI", fontSize=10.5, leading=16, textColor=MUTED,
        fontName="Helvetica-Oblique", spaceBefore=0, spaceAfter=14,
    )
    item_text_style = ParagraphStyle(
        "It", fontSize=11, leading=16.5, textColor=INK_SOFT,
        alignment=TA_LEFT,
    )
    bonus_title_style = ParagraphStyle(
        "BT", fontSize=18, leading=24, textColor=INK,
        fontName="Helvetica-Bold", spaceAfter=8,
    )
    bonus_body_style = ParagraphStyle(
        "BB", fontSize=11, leading=16.5, textColor=INK_SOFT, spaceAfter=10,
    )

    section_intros = {
        "PRIMA DELLA VISITA": "La vittoria si prepara in ufficio. 5 mosse che trasformano una visita qualunque in una visita con obiettivo.",
        "DURANTE LA VISITA": "Ascolta piu' di quanto parli. 10 tecniche per estrarre informazioni, quantificare il valore e chiudere con una condizionale.",
        "DOPO LA VISITA": "La differenza tra chi vince e chi perde. 5 azioni nelle 48 ore successive che decidono il contratto.",
    }

    def _case_card(case_data):
        """Mini-case study card: before/after + learning."""
        t = Paragraph(
            f'<b><font color="#0f172a" size="12">{case_data["title"]}</font></b>',
            bonus_body_style,
        )
        before = Paragraph(
            f'<font color="#ef4444" size="10.5"><b>PRIMA:</b></font> '
            f'<font color="#334155" size="10.5">{case_data["before"]}</font>',
            bonus_body_style,
        )
        after = Paragraph(
            f'<font color="#10b981" size="10.5"><b>DOPO:</b></font> '
            f'<font color="#334155" size="10.5">{case_data["after"]}</font>',
            bonus_body_style,
        )
        key = Paragraph(
            f'<i><font color="#64748b" size="10">La chiave: {case_data["key"]}</font></i>',
            bonus_body_style,
        )
        card = Table(
            [[t], [before], [after], [key]],
            colWidths=[PAGE_W - 4 * cm],
        )
        card.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f0f9ff")),
            ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#bae6fd")),
            ("LEFTPADDING", (0, 0), (-1, -1), 18),
            ("RIGHTPADDING", (0, 0), (-1, -1), 18),
            ("TOPPADDING", (0, 0), (0, 0), 14),
            ("BOTTOMPADDING", (0, 0), (0, 0), 6),
            ("TOPPADDING", (0, 1), (0, 1), 2),
            ("BOTTOMPADDING", (0, 1), (0, 1), 2),
            ("TOPPADDING", (0, 2), (0, 2), 2),
            ("BOTTOMPADDING", (0, 2), (0, 2), 8),
            ("TOPPADDING", (0, 3), (0, 3), 6),
            ("BOTTOMPADDING", (0, 3), (0, 3), 14),
        ]))
        return card

    toc_title_style = ParagraphStyle(
        "TocT", fontSize=26, leading=32, textColor=INK,
        fontName="Helvetica-Bold", spaceAfter=14,
    )
    toc_row_style = ParagraphStyle(
        "TocR", fontSize=12, leading=22, textColor=INK_SOFT,
    )
    narrative_style = ParagraphStyle(
        "Nar", fontSize=13, leading=22, textColor=INK_SOFT,
        alignment=TA_LEFT, spaceAfter=14,
    )
    huge_stat_style = ParagraphStyle(
        "HS", fontSize=56, leading=62, textColor=BLUE,
        fontName="Helvetica-Bold", alignment=TA_CENTER,
    )

    flow = []
    # Page 1 cover — cover_page disegna tutto sul canvas, qui serve solo un breaker
    flow.append(Spacer(1, 1))
    flow.append(PageBreak())
    doc.handle_nextPageTemplate("body")

    # ── PAGE 2: INDICE
    flow.append(Paragraph("INDICE", section_kicker_style))
    flow.append(Paragraph("Cosa troverai nelle prossime pagine.", toc_title_style))
    toc_bar = Table([[""]], colWidths=[PAGE_W - 4 * cm], rowHeights=[3])
    toc_bar.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), BLUE)]))
    flow.append(toc_bar)
    flow.append(Spacer(1, 18))
    toc_rows = [
        ("01", "Perche' questa checklist esiste", "pag. 3"),
        ("02", "Fase 1 · Prima della visita (5 punti)", "pag. 4"),
        ("03", "Fase 2 · Durante la visita (10 punti)", "pag. 5"),
        ("04", "Fase 3 · Dopo la visita (5 punti)", "pag. 6"),
        ("05", "Bonus: 3 frasi che chiudono la trattativa", "pag. 7"),
        ("06", "Appendix: template email e offerta pronti", "pag. 8"),
        ("07", "Il passo successivo", "pag. 9"),
    ]
    toc_tbl = Table(
        [[Paragraph(f'<font color="#60a5fa" size="11"><b>{num}</b></font>', toc_row_style),
          Paragraph(f'<font color="#0f172a" size="12">{title}</font>', toc_row_style),
          Paragraph(f'<font color="#64748b" size="10">{page}</font>', toc_row_style)]
         for num, title, page in toc_rows],
        colWidths=[1.2 * cm, PAGE_W - 7 * cm, 2.8 * cm],
    )
    toc_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    flow.append(toc_tbl)

    # ── PAGE 3: INTRO narrativa
    flow.append(PageBreak())
    flow.append(Paragraph("01 · INTRODUZIONE", section_kicker_style))
    flow.append(Paragraph("Perche' questa checklist esiste.", toc_title_style))
    intro_bar = Table([[""]], colWidths=[PAGE_W - 4 * cm], rowHeights=[3])
    intro_bar.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), BLUE)]))
    flow.append(intro_bar)
    flow.append(Spacer(1, 20))
    flow.append(Paragraph(
        "In Italia ci sono <b>220.000 agenti di commercio</b>. Fanno in media 5 visite al giorno, 22 giorni al mese. "
        "Quasi mezzo milione di visite quotidiane. Ma solo il <b>18%</b> di loro ha un processo strutturato.",
        narrative_style,
    ))
    flow.append(Paragraph(
        "Gli altri vanno a braccio. Si preparano in macchina, improvvisano in ufficio del cliente, "
        "scrivono il report la sera alle 22 dopo cena. Non perche' siano pigri — perche' <b>nessuno ha mai "
        "insegnato loro un metodo</b>. Le scuole di vendita costano 2.000 euro a corso, i coach sono lontani, "
        "i mandanti chiedono risultati ma non danno strumenti.",
        narrative_style,
    ))
    # Big stat callout
    flow.append(Spacer(1, 12))
    stat_card = Table(
        [[Paragraph('<font color="#3b82f6" size="52"><b>+40%</b></font>',
                    ParagraphStyle("BS", alignment=TA_CENTER, fontSize=52, leading=58))],
         [Paragraph('<font color="#64748b" size="11">vendite chiuse da chi applica un processo strutturato<br/>vs chi improvvisa · <i>Fonte: Fnaarc, 2023</i></font>',
                    ParagraphStyle("SS", alignment=TA_CENTER, fontSize=11, leading=16))]],
        colWidths=[PAGE_W - 4 * cm],
    )
    stat_card.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eff6ff")),
        ("BOX", (0, 0), (-1, -1), 2, BLUE),
        ("TOPPADDING", (0, 0), (0, 0), 22),
        ("BOTTOMPADDING", (0, 0), (0, 0), 4),
        ("TOPPADDING", (0, 1), (0, 1), 4),
        ("BOTTOMPADDING", (0, 1), (0, 1), 22),
    ]))
    flow.append(stat_card)
    flow.append(Spacer(1, 16))
    flow.append(Paragraph(
        "Questa checklist e' il metodo. 20 punti testati su reti vendita italiane reali — "
        "ferramenta a Bergamo, farmaceutico a Milano, food & beverage a Palermo. "
        "Non e' teoria americana tradotta male. E' pratica italiana, validata sul campo.",
        narrative_style,
    ))
    flow.append(Paragraph(
        "Puoi stamparla, plastificarla, tenerla in auto. Oppure salvarla sul telefono. "
        "L'importante e' <b>applicarla</b>.",
        narrative_style,
    ))

    n = 0
    for si, section in enumerate(_CHECKLIST_CONTENT["sections"]):
        flow.append(PageBreak())
        # Wrapper che updata _state.section_idx ogni volta che viene "disegnato"
        class _SecMarker(Flowable):
            def __init__(self, idx):
                super().__init__()
                self.idx = idx
            def wrap(self, aW, aH): return (0, 0)
            def draw(self):
                _state["section_idx"] = self.idx
        flow.append(_SecMarker(si + 1))

        kicker = f"FASE {si+1} DI {len(_CHECKLIST_CONTENT['sections'])}  ·  {section.get('time', '')}"
        flow.append(Paragraph(kicker, section_kicker_style))
        flow.append(Paragraph(section["heading"].title(), section_title_style))
        # Divider bar gradient
        bar_tbl = Table([[""]], colWidths=[PAGE_W - 4 * cm], rowHeights=[3])
        bar_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), BLUE),
            ("BOX", (0, 0), (-1, -1), 0, colors.white),
        ]))
        flow.append(bar_tbl)
        flow.append(Spacer(1, 12))
        # Section intro — usa quello della checklist content o fallback
        intro_text = section.get("intro") or section_intros.get(section["heading"])
        if intro_text:
            flow.append(Paragraph(intro_text, section_intro_style))

        for item in section["items"]:
            n += 1
            safe = item.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            num_para = Paragraph(
                f'<font color="#ffffff" size="12"><b>{n:02d}</b></font>',
                ParagraphStyle("Num", alignment=TA_CENTER, fontSize=12, leading=14),
            )
            text_para = Paragraph(safe, item_text_style)
            # Check mark verde
            check_para = Paragraph(
                f'<font color="#10b981" size="14"><b>&#10003;</b></font>',
                ParagraphStyle("Chk", alignment=TA_CENTER, fontSize=14, leading=14),
            )
            item_tbl = Table(
                [[num_para, text_para, check_para]],
                colWidths=[1.25 * cm, PAGE_W - 6.5 * cm, 1 * cm],
            )
            item_tbl.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                # Num cell gradient-like: solid blue
                ("BACKGROUND", (0, 0), (0, 0), BLUE),
                # Text + check: light card
                ("BACKGROUND", (1, 0), (2, 0), CARD_BG),
                ("LINEBEFORE", (1, 0), (1, 0), 0, colors.white),
                ("BOX", (0, 0), (-1, -1), 0, colors.white),
                ("LEFTPADDING", (0, 0), (0, 0), 6),
                ("RIGHTPADDING", (0, 0), (0, 0), 6),
                ("LEFTPADDING", (1, 0), (-1, -1), 12),
                ("RIGHTPADDING", (1, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                ("ROUNDEDCORNERS", [6, 6, 6, 6]),
            ]))
            flow.append(KeepTogether([item_tbl, Spacer(1, 7)]))

        # Mini case study alla fine di ogni sezione
        case_data = section.get("case")
        if case_data:
            flow.append(Spacer(1, 12))
            flow.append(_case_card(case_data))

        flow.append(Spacer(1, 22))

    # BONUS PAGE — 3 frasi che chiudono
    flow.append(PageBreak())
    flow.append(Paragraph("BONUS", section_kicker_style))
    flow.append(Paragraph("3 frasi che chiudono la trattativa.", bonus_title_style))
    bonus_bar = Table([[""]], colWidths=[PAGE_W - 4 * cm], rowHeights=[3])
    bonus_bar.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), PURPLE)]))
    flow.append(bonus_bar)
    flow.append(Spacer(1, 14))
    flow.append(Paragraph(
        '<i>"Testate su cento trattative reali. Funzionano quando le condizioni sono gia' + "'" + ' allineate."</i>',
        section_intro_style,
    ))

    bonus_phrases = [
        ("La condizionale", "Se confermo oggi le condizioni che le ho mostrato, possiamo partire con il primo ordine entro 15 giorni?",
         "Trasforma un interesse in una scelta binaria senza pressione."),
        ("Il default sociale", "Altri tre clienti nel suo settore hanno preso questa soluzione a queste condizioni. Ha senso che veda se funziona anche per lei.",
         "Riduce il rischio percepito citando precedenti. Usa nomi reali se il cliente lo permette."),
        ("L'inversione", "Cosa dovrebbe succedere oggi, durante questa visita, perche' lei firmi entro venerdi'?",
         "Fa esprimere al cliente i suoi criteri di decisione. Ottieni la mappa per chiudere."),
    ]
    for i, (title, phrase, why) in enumerate(bonus_phrases):
        t_para = Paragraph(f'<b><font color="#0f172a" size="13">{i+1:02d}. {title}</font></b>', bonus_body_style)
        phrase_para = Paragraph(f'<font color="#1e40af" size="12"><i>"{phrase}"</i></font>', bonus_body_style)
        why_para = Paragraph(f'<font color="#64748b" size="10">Perche\' funziona: {why}</font>', bonus_body_style)
        card = Table(
            [[t_para], [phrase_para], [why_para]],
            colWidths=[PAGE_W - 4 * cm],
        )
        card.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
            ("LEFTPADDING", (0, 0), (-1, -1), 18),
            ("RIGHTPADDING", (0, 0), (-1, -1), 18),
            ("TOPPADDING", (0, 0), (0, 0), 14),
            ("BOTTOMPADDING", (0, 0), (0, 0), 6),
            ("TOPPADDING", (0, 1), (0, 1), 6),
            ("BOTTOMPADDING", (0, 1), (0, 1), 6),
            ("TOPPADDING", (0, 2), (0, 2), 6),
            ("BOTTOMPADDING", (0, 2), (0, 2), 14),
        ]))
        flow.append(KeepTogether([card, Spacer(1, 10)]))

    # APPENDIX — template pronti all'uso
    flow.append(PageBreak())
    flow.append(Paragraph("APPENDIX", section_kicker_style))
    flow.append(Paragraph("Template pronti — copia, personalizza, invia.", bonus_title_style))
    appendix_bar = Table([[""]], colWidths=[PAGE_W - 4 * cm], rowHeights=[3])
    appendix_bar.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), GREEN)]))
    flow.append(appendix_bar)
    flow.append(Spacer(1, 14))
    flow.append(Paragraph(
        '<i>Scheletri neutri. Sostituisci i campi in [parentesi] con i tuoi dati. Testati in produzione su 200+ mandanti.</i>',
        section_intro_style,
    ))

    # Template 1: Email follow-up
    email_title = Paragraph(
        '<font color="#0f172a" size="12"><b>A · EMAIL DI FOLLOW-UP (2 ore dopo visita)</b></font>',
        bonus_body_style,
    )
    email_body = Paragraph(
        '<font color="#334155" size="10.5" name="Courier"><b>Oggetto:</b> Seguito visita del [data] — [riferimento prodotto]<br/><br/>'
        'Gentile [Sig./Sig.ra cognome],<br/><br/>'
        'grazie per il tempo dedicatomi durante la <b>visita del [data]</b>. '
        'Come concordato, le confermo le condizioni discusse su [prodotto/servizio]:<br/><br/>'
        '&nbsp;&nbsp;• Sconto: [X%] sul primo ordine<br/>'
        '&nbsp;&nbsp;• Pagamento: [N gg d.f.f.m.]<br/>'
        '&nbsp;&nbsp;• Consegna: [N gg lavorativi]<br/><br/>'
        'Resto a disposizione fino al [data validita\']. La richiamo <b>[giorno, ora]</b> per confermare.<br/><br/>'
        'Cordialmente,<br/>[Nome Cognome]</font>',
        bonus_body_style,
    )
    email_card = Table([[email_title], [email_body]], colWidths=[PAGE_W - 4 * cm])
    email_card.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f0fdf4")),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#86efac")),
        ("LEFTPADDING", (0, 0), (-1, -1), 16),
        ("RIGHTPADDING", (0, 0), (-1, -1), 16),
        ("TOPPADDING", (0, 0), (0, 0), 14),
        ("BOTTOMPADDING", (0, 0), (0, 0), 8),
        ("TOPPADDING", (0, 1), (0, 1), 0),
        ("BOTTOMPADDING", (0, 1), (0, 1), 14),
    ]))
    flow.append(KeepTogether([email_card, Spacer(1, 12)]))

    # Template 2: Offerta commerciale
    off_title = Paragraph(
        '<font color="#0f172a" size="12"><b>B · OFFERTA COMMERCIALE (formato legale italiano)</b></font>',
        bonus_body_style,
    )
    off_body = Paragraph(
        '<font color="#334155" size="10.5" name="Courier"><b>PROPOSTA COMMERCIALE N.</b> 2026/PP-[ddmm]/[SIGLA]<br/>'
        '<b>Data emissione:</b> [data]  ·  <b>Validita\':</b> [N giorni]<br/><br/>'
        'Spett.le [Ragione Sociale Cliente]<br/>'
        'All\'attenzione di [Sig./Sig.ra Cognome]<br/><br/>'
        '<b>Oggetto:</b> Fornitura [prodotto/servizio]<br/><br/>'
        '<b>Condizioni proposte:</b><br/>'
        '&nbsp;&nbsp;• Sconto: [X%]<br/>'
        '&nbsp;&nbsp;• Pagamento: [termini]<br/>'
        '&nbsp;&nbsp;• Consegna: [N gg]<br/>'
        '&nbsp;&nbsp;• Ordine minimo: [quantita\'/valore]<br/><br/>'
        'Per accettazione: ______________________  Data: __________</font>',
        bonus_body_style,
    )
    off_card = Table([[off_title], [off_body]], colWidths=[PAGE_W - 4 * cm])
    off_card.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f0fdf4")),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#86efac")),
        ("LEFTPADDING", (0, 0), (-1, -1), 16),
        ("RIGHTPADDING", (0, 0), (-1, -1), 16),
        ("TOPPADDING", (0, 0), (0, 0), 14),
        ("BOTTOMPADDING", (0, 0), (0, 0), 8),
        ("TOPPADDING", (0, 1), (0, 1), 0),
        ("BOTTOMPADDING", (0, 1), (0, 1), 14),
    ]))
    flow.append(KeepTogether([off_card, Spacer(1, 8)]))

    # Nota finale appendix
    flow.append(Paragraph(
        '<font color="#64748b" size="10"><i>Compilare questi template manualmente richiede '
        '~25 minuti per visita. Oppure li genera <b><font color="#10b981">VYNEX</font></b> in 30 secondi '
        'partendo da 2 righe descrittive.</i></font>',
        bonus_body_style,
    ))

    # CTA finale drammatico
    flow.append(PageBreak())
    flow.append(Spacer(1, 3 * cm))
    flow.append(Paragraph(
        '<font color="#64748b" size="10"><b>IL PASSO SUCCESSIVO</b></font>',
        section_kicker_style,
    ))
    flow.append(Paragraph(
        'Hai appena letto 20 punti. Gli ultimi 5 — report, email, offerta — li puoi scrivere da solo ogni sera.',
        ParagraphStyle("Q", fontSize=15, leading=22, textColor=INK, fontName="Helvetica-Bold", spaceAfter=8),
    ))
    flow.append(Paragraph(
        '<font color="#334155"><b>Oppure</b> li puoi scrivere in 30 secondi con VYNEX, l\'Intelligenza Artificiale italiana che ricorda ogni cliente.</font>',
        ParagraphStyle("Q2", fontSize=13, leading=20, textColor=INK_SOFT, spaceAfter=22),
    ))

    # CTA premium button-simulated
    cta_link = Paragraph(
        '<b><font color="#ffffff" size="13">&nbsp;&nbsp;&nbsp;Prova la demo gratis &nbsp;&nbsp;&rarr;&nbsp;&nbsp;&nbsp;</font></b>',
        ParagraphStyle("CTA", alignment=TA_CENTER, fontSize=13),
    )
    cta_tbl = Table([[cta_link]], colWidths=[PAGE_W - 4 * cm], rowHeights=[1.4 * cm])
    cta_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BLUE),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    flow.append(cta_tbl)
    flow.append(Spacer(1, 10))
    flow.append(Paragraph(
        '<font color="#64748b" size="10">vynex.it/demo  ·  senza account  ·  senza carta  ·  30 secondi</font>',
        ParagraphStyle("CTAsub", alignment=TA_CENTER, fontSize=10, leading=16),
    ))
    flow.append(Spacer(1, 30))
    # Quote signature
    flow.append(Paragraph(
        '<font color="#60a5fa" size="14"><b>TU CHIUDI.  VYNEX SCRIVE.</b></font>',
        ParagraphStyle("Sig", alignment=TA_CENTER, fontSize=14, leading=20),
    ))
    flow.append(Paragraph(
        '<font color="#94a3b8" size="9">— Roberto Pizzini, fondatore</font>',
        ParagraphStyle("SigAu", alignment=TA_CENTER, fontSize=9, leading=14),
    ))

    doc.build(flow)
    pdf = buf.getvalue()
    buf.close()
    return pdf


@app.get("/lead-magnet/checklist-visita.pdf")
async def lead_magnet_checklist_pdf():
    pdf = _render_checklist_pdf()
    return Response(
        content=pdf, media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="vynex-checklist-visita-commerciale.pdf"',
            "Cache-Control": "public, max-age=86400",
        },
    )


@app.post("/api/lead-magnet/checklist-visita")
@limiter.limit("10/hour")
async def api_lead_magnet_checklist(
    request: Request,
    email: str = Form(...),
    full_name: str = Form(""),
    accept_terms: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if accept_terms != "on":
        return RedirectResponse("/?error=Devi+accettare+la+privacy+policy", status_code=302)
    try:
        valid = validate_email(email, check_deliverability=False)
        email_norm = valid.normalized.lower()
    except EmailNotValidError:
        return RedirectResponse("/?error=Email+non+valida", status_code=302)

    lead = None
    try:
        lead, _c = await upsert_lead(
            db, email=email_norm,
            full_name=(full_name or "").strip() or None,
            company=None, source="lead_magnet",
            notes="checklist-visita",
        )
        await save_source_attribution(
            db, lead_id=lead.id,
            utm_cookie=request.cookies.get("vynex_utm"),
            ip=(request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else ""))[:45],
            user_agent=request.headers.get("user-agent", "")[:500],
        )
    except Exception:
        logger.exception("lead_magnet capture failed")

    # Invia email con PDF link + enroll nella sequence drip (fire-and-forget,
    # non blocca il redirect al download se l'email fallisce).
    try:
        base = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
        pdf_link = f"{base}/lead-magnet/checklist-visita.pdf"
        await send_lead_magnet_email(email_norm, (full_name or "").strip(), pdf_link)
    except Exception:
        logger.exception("lead_magnet email send failed email=%s", email_norm)

    if lead is not None:
        try:
            await enroll_lead_in_sequence(db, lead, SEQUENCE_LEAD_DEMO)
        except Exception:
            logger.exception("lead_magnet drip enroll failed lead=%s", lead.id)

    # Redirect direct al PDF — download immediato
    return RedirectResponse("/lead-magnet/checklist-visita.pdf", status_code=302)


@app.post("/api/admin/blog/unpublish/{slug}")
async def admin_blog_unpublish(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Nasconde articolo dal pubblico (torna 404). Lasciato in DB per history."""
    _require_admin(request)
    r = await db.execute(
        update(BlogPost).where(BlogPost.slug == slug).values(published=False).returning(BlogPost.id)
    )
    await db.commit()
    ok = r.scalar_one_or_none() is not None
    return {"unpublished": ok, "slug": slug}


@app.post("/api/admin/blog/publish/{slug}")
async def admin_blog_publish(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    _require_admin(request)
    r = await db.execute(
        update(BlogPost).where(BlogPost.slug == slug).values(published=True).returning(BlogPost.id)
    )
    await db.commit()
    ok = r.scalar_one_or_none() is not None
    return {"published": ok, "slug": slug}


@app.get("/api/admin/blog/list")
async def admin_blog_list(request: Request, db: AsyncSession = Depends(get_db)):
    _require_admin(request)
    q = await db.execute(
        select(BlogPost).order_by(BlogPost.published_at.desc()).limit(100)
    )
    return [
        {
            "slug": p.slug,
            "title": p.title,
            "published": p.published,
            "published_at": p.published_at.isoformat() if p.published_at else None,
            "keyword": p.keyword_primary,
        }
        for p in q.scalars().all()
    ]


# ─── NEWSLETTER (subscribe + unsubscribe + admin preview/send) ──────────────

@app.post("/api/newsletter/subscribe")
@limiter.limit("6/minute")
async def api_newsletter_subscribe(
    request: Request,
    email: str = Form(...),
    full_name: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    import secrets as _sec
    try:
        valid = validate_email(email, check_deliverability=False)
        email_norm = valid.normalized.lower()
    except EmailNotValidError:
        return JSONResponse({"ok": False, "error": "Email non valida"}, status_code=400)

    lq = await db.execute(select(Lead).where(Lead.email == email_norm))
    lead = lq.scalar_one_or_none()
    if lead is None:
        lead = Lead(
            email=email_norm,
            full_name=(full_name or "").strip()[:120] or None,
            source="newsletter",
            status="new",
            unsub_token=_sec.token_urlsafe(24),
            newsletter_opted_in=True,
        )
        db.add(lead)
    else:
        lead.newsletter_opted_in = True
        lead.unsubscribed = False
        lead.unsubscribed_at = None
        if full_name and not lead.full_name:
            lead.full_name = full_name.strip()[:120]
    await db.commit()
    return JSONResponse({"ok": True, "message": "Iscrizione confermata. Preparati: Lun/Mer/Ven 08:30."})


@app.get("/newsletter/unsubscribe/lead/{token}", response_class=HTMLResponse)
async def newsletter_unsub_lead(token: str, db: AsyncSession = Depends(get_db)):
    from datetime import datetime as _dt
    r = await db.execute(select(Lead).where(Lead.unsub_token == token))
    lead = r.scalar_one_or_none()
    if lead is not None:
        lead.newsletter_opted_in = False
        lead.unsubscribed = True
        lead.unsubscribed_at = _dt.utcnow()
        await db.commit()
    return HTMLResponse(_unsub_page_html(bool(lead)))


@app.get("/newsletter/unsubscribe/user/{user_id}/{token}", response_class=HTMLResponse)
async def newsletter_unsub_user(user_id: int, token: str, db: AsyncSession = Depends(get_db)):
    from newsletter import verify_user_unsub_token
    if not verify_user_unsub_token(user_id, token):
        return HTMLResponse(_unsub_page_html(False), status_code=403)
    r = await db.execute(select(User).where(User.id == user_id))
    u = r.scalar_one_or_none()
    if u is not None:
        u.newsletter_opted_in = False
        await db.commit()
    return HTMLResponse(_unsub_page_html(bool(u)))


def _unsub_page_html(ok: bool) -> str:
    if ok:
        return """<!doctype html><html lang="it"><head><meta charset="utf-8"><title>Disiscritto</title>
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;background:#04060f;color:#e2e8f0;font-family:-apple-system,Segoe UI,Inter,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px">
<div style="max-width:520px;padding:40px 32px;background:rgba(15,23,42,.7);border:1px solid rgba(96,165,250,.2);border-radius:20px;text-align:center">
<div style="font-size:22px;font-weight:800;letter-spacing:3px;color:#60a5fa;margin-bottom:18px">VYNEX</div>
<h1 style="font-size:26px;font-weight:800;margin:0 0 12px;color:#f1f5f9">Disiscrizione confermata.</h1>
<p style="color:#94a3b8;line-height:1.7;margin:0 0 22px">Non riceverai più la newsletter. Se cambi idea puoi iscriverti di nuovo dal footer di VYNEX.</p>
<a href="/" style="display:inline-block;padding:12px 22px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff;text-decoration:none;border-radius:10px;font-weight:700">Torna alla home</a>
</div></body></html>"""
    return """<!doctype html><html lang="it"><head><meta charset="utf-8"><title>Link non valido</title>
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;background:#04060f;color:#e2e8f0;font-family:-apple-system,Segoe UI,Inter,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px">
<div style="max-width:520px;padding:40px 32px;background:rgba(15,23,42,.7);border:1px solid rgba(239,68,68,.25);border-radius:20px;text-align:center">
<h1 style="font-size:24px;font-weight:800;margin:0 0 12px;color:#fca5a5">Link non valido o scaduto.</h1>
<p style="color:#94a3b8;line-height:1.7">Contatta robertopizzini19@gmail.com se hai bisogno di essere rimosso manualmente.</p>
</div></body></html>"""


@app.get("/admin/newsletter/preview/{issue_id}", response_class=HTMLResponse)
async def admin_newsletter_preview(issue_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    _require_admin(request)
    from models import NewsletterIssue
    r = await db.execute(select(NewsletterIssue).where(NewsletterIssue.id == issue_id))
    issue = r.scalar_one_or_none()
    if issue is None:
        return HTMLResponse("Not found", status_code=404)
    html = (issue.body_html or "")\
        .replace("{{UNSUB_URL}}", "#preview-unsub")\
        .replace("{{NAME}}", "Roberto")
    return HTMLResponse(html)


@app.post("/admin/newsletter/run/{topic_type}")
async def admin_newsletter_run(topic_type: str, request: Request):
    _require_admin(request)
    if topic_type not in ("guide", "template", "insight"):
        return JSONResponse({"error": "topic_type must be guide|template|insight"}, status_code=400)
    from newsletter import generate_and_send
    try:
        counts = await generate_and_send(topic_type)
        return JSONResponse({"ok": True, **counts})
    except Exception as exc:
        logger.exception("admin newsletter run failed")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/admin/newsletter/send/{issue_id}")
async def admin_newsletter_send_existing(issue_id: int, request: Request):
    _require_admin(request)
    from newsletter import send_issue
    try:
        counts = await send_issue(issue_id)
        return JSONResponse({"ok": True, "issue_id": issue_id, **counts})
    except Exception as exc:
        logger.exception("admin newsletter send existing failed id=%s", issue_id)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/admin/newsletter/generate/{topic_type}")
async def admin_newsletter_generate_only(topic_type: str, request: Request):
    _require_admin(request)
    if topic_type not in ("guide", "template", "insight"):
        return JSONResponse({"error": "topic_type must be guide|template|insight"}, status_code=400)
    from newsletter import generate_issue
    try:
        issue = await generate_issue(topic_type=topic_type)
        return JSONResponse({
            "ok": True,
            "issue_id": issue.id,
            "slug": issue.slug,
            "subject": issue.subject,
            "preview_url": f"/admin/newsletter/preview/{issue.id}",
        })
    except Exception as exc:
        logger.exception("admin newsletter generate failed")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


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
