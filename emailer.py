"""
Email transazionali via Resend API.
Graceful no-op se RESEND_API_KEY non settata (dev locale).
"""
import os
import logging
import httpx

logger = logging.getLogger("vynex.emailer")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "VYNEX <ciao@vynex.it>")
EMAIL_REPLY_TO = os.getenv("EMAIL_REPLY_TO", "ciao@vynex.it")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")

RESEND_URL = "https://api.resend.com/emails"


async def _send(to: str, subject: str, html: str) -> bool:
    if not RESEND_API_KEY:
        logger.info("RESEND_API_KEY not set — skipping email to %s (%s)", to, subject)
        return False
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.post(
            RESEND_URL,
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": EMAIL_FROM,
                "to": [to],
                "subject": subject,
                "html": html,
                "reply_to": EMAIL_REPLY_TO,
            },
        )
        if res.status_code >= 400:
            logger.error("Resend error %s: %s", res.status_code, res.text)
            return False
    return True


def _wrap(body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="it"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#04060f;font-family:-apple-system,Segoe UI,Inter,sans-serif;color:#f1f5f9">
<div style="max-width:560px;margin:0 auto;padding:40px 24px">
  <div style="font-size:22px;font-weight:800;letter-spacing:3px;color:#60a5fa;margin-bottom:24px">VYNEX</div>
  {body_html}
  <div style="margin-top:40px;padding-top:24px;border-top:1px solid #1e293b;color:#64748b;font-size:12px">
    VYNEX — AI per agenti commerciali italiani<br>
    <a href="{BASE_URL}" style="color:#60a5fa">{BASE_URL}</a> · <a href="mailto:{EMAIL_REPLY_TO}" style="color:#60a5fa">{EMAIL_REPLY_TO}</a>
  </div>
</div></body></html>"""


async def send_welcome_email(to: str, name: str) -> bool:
    body = f"""
    <h1 style="font-size:24px;color:#f1f5f9">Benvenuto su VYNEX, {name.split()[0]}.</h1>
    <p style="color:#94a3b8;line-height:1.7">Il tuo account è attivo. Hai 10 documenti gratuiti ogni mese, per sempre, senza carta di credito.</p>
    <p style="color:#94a3b8;line-height:1.7">Da ora, ogni visita diventa report, email di follow-up e offerta commerciale in 30 secondi.</p>
    <p><a href="{BASE_URL}/genera" style="display:inline-block;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff;padding:12px 24px;border-radius:10px;text-decoration:none;font-weight:600">Genera il primo documento</a></p>
    """
    return await _send(to, "Benvenuto su VYNEX", _wrap(body))


async def send_password_reset_email(to: str, name: str, reset_link: str) -> bool:
    body = f"""
    <h1 style="font-size:22px;color:#f1f5f9">Reset password VYNEX</h1>
    <p style="color:#94a3b8;line-height:1.7">Ciao {name.split()[0]}, hai richiesto il reset della password. Il link è valido per 1 ora.</p>
    <p><a href="{reset_link}" style="display:inline-block;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff;padding:12px 24px;border-radius:10px;text-decoration:none;font-weight:600">Reimposta password</a></p>
    <p style="color:#64748b;font-size:12px;line-height:1.6">Se non hai richiesto il reset, ignora questa email — la tua password non cambia.</p>
    """
    return await _send(to, "Reset password VYNEX", _wrap(body))


async def send_payment_success_email(to: str, name: str, plan: str) -> bool:
    body = f"""
    <h1 style="font-size:22px;color:#f1f5f9">Piano {plan.upper()} attivato ✓</h1>
    <p style="color:#94a3b8;line-height:1.7">Ciao {name.split()[0]}, il tuo abbonamento è attivo. Da ora hai documenti illimitati.</p>
    <p><a href="{BASE_URL}/dashboard" style="display:inline-block;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff;padding:12px 24px;border-radius:10px;text-decoration:none;font-weight:600">Vai alla dashboard</a></p>
    """
    return await _send(to, f"Piano VYNEX {plan.upper()} attivato", _wrap(body))


async def send_payment_failed_email(to: str, name: str) -> bool:
    body = f"""
    <h1 style="font-size:22px;color:#f1f5f9">Pagamento non riuscito</h1>
    <p style="color:#94a3b8;line-height:1.7">Ciao {name.split()[0]}, l'ultimo pagamento del tuo abbonamento VYNEX non è andato a buon fine.</p>
    <p style="color:#94a3b8;line-height:1.7">Aggiorna il metodo di pagamento dal portale di fatturazione per evitare l'interruzione del servizio.</p>
    <p><a href="{BASE_URL}/portale-fatturazione" style="display:inline-block;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff;padding:12px 24px;border-radius:10px;text-decoration:none;font-weight:600">Aggiorna pagamento</a></p>
    """
    return await _send(to, "VYNEX — Pagamento non riuscito", _wrap(body))
