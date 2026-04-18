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


async def send_raw(to: str, subject: str, html: str) -> bool:
    """Public helper per il motore di acquisizione — template già renderizzato."""
    return await _send(to, subject, html)


async def _send(to: str, subject: str, html: str) -> bool:
    if not RESEND_API_KEY:
        logger.info("RESEND_API_KEY not set — skipping email to %s (%s)", to, subject)
        return False
    try:
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
    except httpx.TimeoutException:
        logger.error("Resend timeout for %s (%s)", to, subject)
        return False
    except httpx.HTTPError as exc:
        logger.error("Resend transport error for %s: %s", to, exc)
        return False
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
    <p style="color:#94a3b8;line-height:1.7">Ciao {name.split()[0]}, l'ultimo pagamento del tuo abbonamento VYNEX non è andato a buon fine. Hai 7 giorni di tempo per aggiornare il metodo di pagamento prima che il piano venga sospeso.</p>
    <p><a href="{BASE_URL}/portale-fatturazione" style="display:inline-block;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff;padding:12px 24px;border-radius:10px;text-decoration:none;font-weight:600">Aggiorna pagamento</a></p>
    """
    return await _send(to, "VYNEX — Pagamento non riuscito", _wrap(body))


async def send_verification_email(to: str, name: str, verify_link: str) -> bool:
    first = name.split()[0] if name else "ciao"
    body = f"""
    <h1 style="font-size:26px;color:#f1f5f9;font-weight:800;line-height:1.3;margin:0 0 16px">Verifica il tuo indirizzo email</h1>
    <p style="color:#cbd5e1;line-height:1.7;font-size:15px;margin:0 0 24px">Ciao {first}, grazie per esserti registrato su VYNEX. Per completare l'attivazione dell'account e iniziare a generare documenti commerciali in 30 secondi, conferma il tuo indirizzo email cliccando sul pulsante qui sotto.</p>

    <div style="text-align:center;margin:32px 0">
      <a href="{verify_link}" style="display:inline-block;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff;padding:16px 36px;border-radius:12px;text-decoration:none;font-weight:700;font-size:15px;letter-spacing:0.3px">Verifica email</a>
    </div>

    <p style="color:#94a3b8;line-height:1.7;font-size:13px;margin:24px 0 8px">Se il pulsante non funziona, copia e incolla questo link nel browser:</p>
    <p style="background:rgba(59,130,246,0.08);border:1px solid rgba(59,130,246,0.2);border-radius:8px;padding:12px;word-break:break-all;font-family:monospace;font-size:12px;color:#60a5fa;margin:0 0 24px">{verify_link}</p>

    <div style="border-top:1px solid #1e293b;padding-top:20px;margin-top:32px">
      <p style="color:#64748b;font-size:12px;line-height:1.6;margin:0">Il link è valido per 48 ore. Se non hai creato un account VYNEX, ignora questa email — nessun account verrà attivato.</p>
      <p style="color:#64748b;font-size:12px;line-height:1.6;margin:8px 0 0">Per sicurezza, non condividere questo link con nessuno.</p>
    </div>
    """
    return await _send(to, "Verifica il tuo account VYNEX", _wrap(body))


async def send_account_locked_email(to: str, name: str) -> bool:
    body = f"""
    <h1 style="font-size:22px;color:#f1f5f9">Account temporaneamente bloccato</h1>
    <p style="color:#94a3b8;line-height:1.7">Ciao {name.split()[0]}, abbiamo rilevato 5 tentativi di login falliti. Per sicurezza il tuo account è bloccato per 15 minuti.</p>
    <p style="color:#94a3b8;line-height:1.7">Se non sei stato tu, ti consigliamo di reimpostare la password.</p>
    <p><a href="{BASE_URL}/password-dimenticata" style="display:inline-block;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff;padding:12px 24px;border-radius:10px;text-decoration:none;font-weight:600">Reimposta password</a></p>
    """
    return await _send(to, "VYNEX — Account temporaneamente bloccato", _wrap(body))


async def send_subscription_past_due_email(to: str, name: str, days_remaining: int) -> bool:
    body = f"""
    <h1 style="font-size:22px;color:#f1f5f9">Abbonamento in sospeso</h1>
    <p style="color:#94a3b8;line-height:1.7">Ciao {name.split()[0]}, l'abbonamento VYNEX è in stato "past due". Hai ancora <strong>{days_remaining} giorni</strong> di accesso completo prima del downgrade automatico al piano Free.</p>
    <p><a href="{BASE_URL}/portale-fatturazione" style="display:inline-block;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff;padding:12px 24px;border-radius:10px;text-decoration:none;font-weight:600">Aggiorna pagamento ora</a></p>
    """
    return await _send(to, "VYNEX — Abbonamento in sospeso", _wrap(body))
