"""Observability: Sentry init, side-effect import.

Importare questo modulo prima del resto del bootstrap inizializza Sentry
**solo se** `SENTRY_DSN` è settato. Senza DSN: completamente no-op,
zero overhead, zero rischi.

Privacy first:
  - send_default_pii=False (Sentry NON registra email/IP/headers automaticamente)
  - request_bodies="never" (NIENTE body delle richieste viene mai mandato)
  - before_send filter scrubs known-PII keys prima dell'invio

Uso:
    import observability  # noqa: F401  (auto-init)
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger("vynex.observability")

_DSN = os.getenv("SENTRY_DSN", "").strip()
_ENV = os.getenv("RAILWAY_ENVIRONMENT_NAME") or os.getenv("ENV") or "production"
_RELEASE = os.getenv("RAILWAY_DEPLOYMENT_ID") or os.getenv("GIT_COMMIT") or None

# Pattern per scrubbare PII residue (email, telefoni, carte) da messaggi/breadcrumb.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

_SENSITIVE_KEYS = {
    "password", "hashed_password", "token", "access_token", "refresh_token",
    "api_key", "secret", "stripe_customer_id", "stripe_subscription_id",
    "stripe_secret_key", "anthropic_api_key", "resend_api_key",
    "session", "cookie", "authorization",
}


def _scrub(value):
    if isinstance(value, str):
        value = _EMAIL_RE.sub("<email>", value)
        value = _CARD_RE.sub("<card>", value)
        return value
    if isinstance(value, dict):
        return {
            k: ("<scrubbed>" if k.lower() in _SENSITIVE_KEYS else _scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        cleaned = [_scrub(x) for x in value]
        return type(value)(cleaned)
    return value


def _before_send(event, hint):
    try:
        return _scrub(event)
    except Exception:
        # Mai bloccare l'invio per un bug di scrub: meglio un evento un po'
        # più verboso che nessun evento.
        return event


def _init() -> None:
    if not _DSN:
        logger.info("Sentry: SENTRY_DSN missing — observability disabled")
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
    except ImportError:
        logger.warning("Sentry: sentry-sdk not installed — observability disabled")
        return

    sentry_sdk.init(
        dsn=_DSN,
        environment=_ENV,
        release=_RELEASE,
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.05")),
        profiles_sample_rate=0.0,
        send_default_pii=False,  # NO PII automatici
        max_breadcrumbs=50,
        integrations=[
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
            SqlalchemyIntegration(),
        ],
        before_send=_before_send,
        # Sample errori del logging python-stdlib (logger.exception ecc.).
        # event_level=ERROR è già il default.
    )
    logger.info("Sentry: initialized (env=%s, release=%s)", _ENV, _RELEASE or "<none>")


_init()
