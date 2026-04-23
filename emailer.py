"""
Email transazionali — supporta Brevo (primario) e Resend (fallback).
Graceful no-op se nessuna API key è configurata (dev locale).
"""
import os
import logging
import httpx

logger = logging.getLogger("vynex.emailer")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "VYNEX <noreply@vynex.it>")
EMAIL_REPLY_TO = os.getenv("EMAIL_REPLY_TO", "robertopizzini19@gmail.com")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")

RESEND_URL = "https://api.resend.com/emails"
BREVO_URL = "https://api.brevo.com/v3/smtp/email"


async def send_raw(to: str, subject: str, html: str) -> bool:
    """Public helper per il motore di acquisizione — template già renderizzato."""
    return await _send(to, subject, html)


async def _send(to: str, subject: str, html: str) -> bool:
    # Try Brevo first, fall back to Resend at runtime if Brevo fails (e.g.
    # account not yet activated). Ensures newsletter + transactional work
    # the moment Resend is configured, without waiting for Brevo approval.
    if BREVO_API_KEY:
        ok = await _send_brevo(to, subject, html)
        if ok:
            return True
        if RESEND_API_KEY:
            logger.info("Brevo failed for %s, falling back to Resend", to)
            return await _send_resend(to, subject, html)
        return False
    if RESEND_API_KEY:
        return await _send_resend(to, subject, html)
    logger.info("No email provider configured — skipping email to %s (%s)", to, subject)
    return False


async def _send_brevo(to: str, subject: str, html: str) -> bool:
    from_parts = EMAIL_FROM.split("<")
    from_name = from_parts[0].strip() if len(from_parts) > 1 else "VYNEX"
    from_email = from_parts[-1].rstrip(">").strip() if len(from_parts) > 1 else "noreply@vynex.it"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(
                BREVO_URL,
                headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
                json={
                    "sender": {"name": from_name, "email": from_email},
                    "to": [{"email": to}],
                    "subject": subject,
                    "htmlContent": html,
                    "replyTo": {"email": EMAIL_REPLY_TO},
                },
            )
    except httpx.TimeoutException:
        logger.error("Brevo timeout for %s", to)
        return False
    except httpx.HTTPError as exc:
        logger.error("Brevo transport error: %s", exc)
        return False
    if res.status_code >= 400:
        # 403 permission_denied = account not activated (blocking for ALL emails, not this one).
        # Log as WARNING (not ERROR) so logs are cleaner while waiting for manual activation.
        if res.status_code == 403 and "not yet activated" in res.text:
            logger.warning("Brevo SMTP not activated (account-wide): %s", res.text[:200])
        else:
            logger.error("Brevo error %s: %s", res.status_code, res.text)
        # Return error detail in second tuple element via logger context? Pass it via raising.
        # Keep the bool API for backwards compat and let _schedule_retry detect patterns from
        # the error message we log. Mark as failed for the caller.
        return False
    return True


RESEND_FALLBACK_FROM = "VYNEX <onboarding@resend.dev>"


async def _send_resend(to: str, subject: str, html: str, _from: str | None = None) -> bool:
    sender = _from or EMAIL_FROM
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(
                RESEND_URL,
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={"from": sender, "to": [to], "subject": subject, "html": html, "reply_to": EMAIL_REPLY_TO},
            )
    except httpx.TimeoutException:
        logger.error("Resend timeout for %s", to)
        return False
    except httpx.HTTPError as exc:
        logger.error("Resend transport error: %s", exc)
        return False
    if res.status_code >= 400:
        # Domain not verified → retry once with Resend's pre-verified sandbox sender
        # so first-sends deliver without waiting for DNS propagation.
        if res.status_code == 403 and "not verified" in res.text.lower() and sender != RESEND_FALLBACK_FROM:
            logger.info("Resend domain not verified for %s, retrying with %s", sender, RESEND_FALLBACK_FROM)
            return await _send_resend(to, subject, html, _from=RESEND_FALLBACK_FROM)
        logger.error("Resend error %s: %s", res.status_code, res.text)
        return False
    return True


def _wrap(body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="it"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VYNEX</title>
</head>
<body style="margin:0;padding:0;background:#04060f;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,sans-serif;color:#f1f5f9;line-height:1.6;-webkit-font-smoothing:antialiased">
<div style="max-width:600px;margin:0 auto;padding:0">
  <!-- HERO gradient banner -->
  <div style="background:linear-gradient(135deg,#3b82f6 0%,#8b5cf6 55%,#ec4899 100%);padding:32px 28px;text-align:center;border-radius:0 0 4px 4px">
    <div style="font-size:28px;font-weight:900;letter-spacing:4px;color:#ffffff;text-shadow:0 2px 12px rgba(0,0,0,0.18);margin:0">VYNEX</div>
    <div style="font-size:10.5px;font-weight:700;letter-spacing:2.4px;color:rgba(255,255,255,0.85);margin-top:8px">TU CHIUDI.&nbsp;&nbsp;VYNEX SCRIVE.</div>
  </div>

  <!-- Card content -->
  <div style="background:#0b1020;border:1px solid #1e293b;border-top:none;padding:36px 32px;color:#e2e8f0">
    {body_html}
  </div>

  <!-- Footer -->
  <div style="padding:24px 28px;background:#04060f;color:#64748b;font-size:12px;text-align:center;line-height:1.7">
    <div style="font-size:13px;font-weight:800;letter-spacing:2px;color:#60a5fa;margin-bottom:8px">VYNEX</div>
    Intelligenza Artificiale italiana per agenti commerciali<br>
    <a href="{BASE_URL}" style="color:#60a5fa;text-decoration:none">{BASE_URL}</a>
    &nbsp;·&nbsp;
    <a href="mailto:{EMAIL_REPLY_TO}" style="color:#60a5fa;text-decoration:none">{EMAIL_REPLY_TO}</a>
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


async def send_lead_magnet_email(to: str, name: str, pdf_link: str) -> bool:
    first = (name.split()[0] if name else "ciao")
    # Preheader (invisible) — shown in inbox preview next to subject
    preheader = "4 pagine pratiche: 20 punti azione, 3 frasi che chiudono, scaricala ora."
    body = f"""
    <!-- preheader -->
    <div style="display:none !important;visibility:hidden;mso-hide:all;font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden">{preheader}</div>

    <!-- Hero emoji badge -->
    <div style="text-align:center;margin:0 0 14px">
      <span style="display:inline-block;padding:8px 16px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);border-radius:100px;font-size:11px;font-weight:800;letter-spacing:1.4px;color:#ffffff;text-transform:uppercase">✨ Checklist premium · PDF 4 pagine</span>
    </div>

    <h1 style="font-size:26px;line-height:1.2;font-weight:900;color:#f1f5f9;margin:0 0 14px;text-align:center;letter-spacing:-.01em">La tua checklist è pronta, {first}.</h1>

    <p style="color:#cbd5e1;line-height:1.7;margin:0 0 22px;font-size:15.5px;text-align:center">Quattro pagine tascabili, zero fuffa. <strong style="color:#f1f5f9">20 punti azionabili</strong> validati su reti vendita italiane reali: prima, durante e dopo la visita.</p>

    <!-- CTA button wrap with pseudo-3D -->
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin:0 0 14px">
      <tr><td align="center">
        <table role="presentation" cellspacing="0" cellpadding="0" border="0">
          <tr><td style="border-radius:12px;background:linear-gradient(135deg,#3b82f6 0%,#8b5cf6 100%);box-shadow:0 8px 24px rgba(59,130,246,.35)">
            <a href="{pdf_link}" target="_blank" style="display:inline-block;padding:16px 36px;font-size:16px;font-weight:800;color:#ffffff;text-decoration:none;letter-spacing:.3px">📄&nbsp;&nbsp;Scarica la checklist PDF</a>
          </td></tr>
        </table>
      </td></tr>
    </table>
    <p style="color:#94a3b8;line-height:1.6;font-size:12.5px;text-align:center;margin:0 0 28px">Il link è permanente. Salva questa email, ritrovi la checklist quando vuoi.</p>

    <!-- Cosa trovi dentro -->
    <div style="padding:24px 22px;background:rgba(96,165,250,0.06);border:1px solid rgba(96,165,250,0.18);border-radius:14px;margin:0 0 24px">
      <div style="font-size:11px;font-weight:800;letter-spacing:1.4px;color:#60a5fa;text-transform:uppercase;margin-bottom:14px">Cosa trovi dentro</div>
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
        <tr><td style="padding:5px 0;color:#e2e8f0;font-size:14px;line-height:1.65"><span style="color:#60a5fa;font-weight:800">01.</span>&nbsp; <strong>5 punti</strong> per preparare la visita (vittoria si prepara in ufficio)</td></tr>
        <tr><td style="padding:5px 0;color:#e2e8f0;font-size:14px;line-height:1.65"><span style="color:#8b5cf6;font-weight:800">02.</span>&nbsp; <strong>10 tecniche</strong> per ascoltare, quantificare e chiudere</td></tr>
        <tr><td style="padding:5px 0;color:#e2e8f0;font-size:14px;line-height:1.65"><span style="color:#ec4899;font-weight:800">03.</span>&nbsp; <strong>5 azioni</strong> nelle 48h dopo che decidono il contratto</td></tr>
        <tr><td style="padding:5px 0;color:#e2e8f0;font-size:14px;line-height:1.65"><span style="color:#10b981;font-weight:800">✦</span>&nbsp; <strong>Bonus:</strong> 3 frasi testate che chiudono la trattativa</td></tr>
      </table>
    </div>

    <!-- Quote block -->
    <div style="padding:22px 24px;background:rgba(15,23,42,0.7);border-left:3px solid #60a5fa;border-radius:0 10px 10px 0;margin:0 0 28px">
      <p style="color:#cbd5e1;line-height:1.7;margin:0;font-style:italic;font-size:14.5px">"Gli ultimi 5 punti — report, email di follow-up, offerta — sono quelli che rubano le ore la sera. Puoi scriverli tu. Oppure li scrive <strong style="color:#f1f5f9;font-style:normal">VYNEX</strong> in 30 secondi."</p>
      <p style="color:#94a3b8;font-size:12px;margin:10px 0 0">— Roberto Pizzini, fondatore VYNEX</p>
    </div>

    <!-- Bonus CTA -->
    <div style="padding:24px 22px;background:linear-gradient(135deg,rgba(59,130,246,0.10),rgba(139,92,246,0.08));border:1px solid rgba(96,165,250,0.25);border-radius:14px;margin:0 0 24px;text-align:center">
      <div style="font-size:11px;font-weight:800;letter-spacing:1.4px;color:#f472b6;text-transform:uppercase;margin-bottom:10px">Bonus — Provalo ora</div>
      <p style="color:#cbd5e1;line-height:1.6;margin:0 0 16px;font-size:14.5px">La parte <strong style="color:#f1f5f9">"dopo la visita"</strong> della checklist? VYNEX la fa in 30 secondi. Scrivi due righe, ricevi 3 documenti pronti.</p>
      <a href="{BASE_URL}/demo" style="display:inline-block;background:rgba(15,23,42,.85);border:1px solid #60a5fa;color:#60a5fa;padding:12px 24px;border-radius:10px;text-decoration:none;font-size:13.5px;font-weight:700">Prova la demo gratis →</a>
      <div style="color:#64748b;font-size:11px;margin-top:10px">Senza account · senza carta · 30 secondi</div>
    </div>

    <p style="color:#64748b;font-size:12.5px;line-height:1.7;margin:0;text-align:center">Nei prossimi 14 giorni ti mando 3 email con template pratici. Zero spam, disiscrizione 1 click.</p>
    """
    return await _send(to, "📄 La tua checklist VYNEX — 20 punti per la visita perfetta", _wrap(body))


async def send_demo_recovery_email(to: str, name: str, result_link: str) -> bool:
    first = name.split()[0] if name else "ciao"
    body = f"""
    <h1 style="font-size:22px;color:#f1f5f9">I tuoi 3 documenti VYNEX</h1>
    <p style="color:#94a3b8;line-height:1.7">Ciao {first}, ecco il link privato per rivedere i documenti che hai generato su VYNEX. Valido 7 giorni.</p>
    <p><a href="{result_link}" style="display:inline-block;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff;padding:12px 24px;border-radius:10px;text-decoration:none;font-weight:600">Apri i documenti</a></p>
    <p style="color:#64748b;font-size:12px;line-height:1.6">Se non hai mai usato VYNEX, ignora questa email.</p>
    """
    return await _send(to, "I tuoi documenti VYNEX — link di accesso", _wrap(body))


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
