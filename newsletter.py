"""
VYNEX newsletter engine — generate + send autonomously Mon/Wed/Fri 08:30.

Flow:
  1. scheduler triggers `generate_and_send_weekly_issue(topic_type)`
  2. `generate_issue()` calls Claude Haiku with a strict prompt
     (HOOK → MICRO-VALORE → DEMO VYNEX → CTA) and persists a NewsletterIssue
  3. `render_html()` wraps the generated content in a responsive HTML email
     with inline CSS (dark VYNEX theme, gradient hero, glass cards, CTA button)
  4. `send_issue()` streams batches of 50 recipients via Resend/Brevo, updates
     counters, honours unsubscribe, logs failures non-blocking

Audience: union of Lead(newsletter_opted_in=TRUE) and User(newsletter_opted_in=TRUE).
Unsubscribe: single token per recipient stored in Lead.unsub_token. Users get a
generated token via HMAC on user_id (no DB round-trip needed).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import ai_engine
from database import AsyncSessionLocal
from emailer import send_raw
from models import Lead, NewsletterIssue, User

logger = logging.getLogger("vynex.newsletter")

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-do-not-use-in-prod")

TOPIC_PROMPTS = {
    "guide": {
        "label": "Guida pratica",
        "direction": (
            "Scegli UNA situazione operativa che un agente di commercio italiano "
            "vive davvero ogni settimana (es: preparare una visita a freddo, gestire "
            "un'obiezione sul prezzo, ricontattare un cliente silente, chiudere una "
            "trattativa lunga, smarcare un rifiuto cortese). Dai 3-5 azioni concrete, "
            "numerate, eseguibili OGGI stesso."
        ),
    },
    "template": {
        "label": "Template pronto",
        "direction": (
            "Fornisci UN template riutilizzabile per agenti italiani (es: email di "
            "follow-up dopo visita, messaggio WhatsApp per fissare appuntamento, "
            "offerta di riapertura dopo no, recap settimanale al mandante). "
            "Dammi il testo esatto da copiare, con placeholder [NOME], [AZIENDA] ecc. "
            "Tono: formale ma umano, italiano commerciale nativo."
        ),
    },
    "insight": {
        "label": "Insight di mercato",
        "direction": (
            "Scegli UN dato o tendenza concreta del mercato B2B italiano 2026 (es: "
            "tempi medi di chiusura in manifatturiero, % agenti che usa ancora "
            "solo Excel, canali preferiti dai buyer italiani). Presentalo in modo "
            "pratico: cosa significa per un agente, cosa dovrebbe cambiare nel suo modo di lavorare."
        ),
    },
}


# ─── generation ────────────────────────────────────────────────────────────────

def _slug(text: str, max_len: int = 80) -> str:
    s = re.sub(r"[^a-z0-9\s-]", "", text.lower())
    s = re.sub(r"\s+", "-", s.strip())
    return s[:max_len] or "issue"


async def generate_issue(topic_type: str = "guide", db: Optional[AsyncSession] = None) -> NewsletterIssue:
    """Ask Claude for hook+body+CTA for a given topic. Persist as draft."""
    if topic_type not in TOPIC_PROMPTS:
        raise ValueError(f"Unknown topic_type: {topic_type}")

    conf = TOPIC_PROMPTS[topic_type]
    prompt = f"""Stai scrivendo la newsletter settimanale di VYNEX, rivolta ad agenti di commercio italiani.

Tipologia di questa issue: **{conf['label']}**.

Direzione:
{conf['direction']}

═══════════════════════════════════════════════════
VINCOLI HARD — NON DEROGARE MAI
═══════════════════════════════════════════════════

OFFERTA VYNEX (copiala letteralmente, non inventare numeri):
- Piano Free: 10 documenti gratis ogni mese, per sempre, senza carta.
- Piano Pro: 10 giorni di prova gratuita, poi €49/mese, disdici quando vuoi.
- NON scrivere mai "15 giorni", "14 giorni", "trial di X giorni" diversi da "10 giorni".
- NON inventare bonus, sconti, coupon o promozioni non esistenti.

CTA (regola critica):
- La CTA deve essere COERENTE col contenuto dell'email: se parli di follow-up, la CTA riguarda i follow-up; se parli di offerte, la CTA riguarda le offerte.
- La CTA deve contenere un VERBO OPERATIVO in imperativo (Automatizza, Genera, Smetti di, Riduci, Crea, Recupera, Trasforma).
- NON usare CTA generiche tipo "Prova gratis", "Prova VYNEX", "Registrati". Deve sempre specchiare il problema trattato.
- Massimo 6 parole, senza "→" (la freccia la aggiunge il template).
- Esempio per email su follow-up: "Automatizza i follow-up dei clienti".
- Esempio per email su offerte commerciali: "Genera offerte in 30 secondi".
- Esempio per email su report di visita: "Smetti di scrivere report la sera".

VALORE OPERATIVO (regola critica):
- Devi includere ALMENO UNA frase concreta (script, template, email, domanda) che l'utente può copiare e usare subito in una visita/chiamata/email reale.
- Racchiudila tra virgolette così si riconosce come "testo da usare".

STILE:
- Italiano commerciale professionale, mai maccheronico.
- Mai generico ("il digitale sta cambiando", "l'AI è il futuro"): sempre concreto e operativo.
- Lunghezza corpo: 180-260 parole totali.
- NO emoji nel testo. NO saluti iniziali/finali (li aggiunge il template).

STRUTTURA OBBLIGATORIA (4 blocchi):
- HOOK: 1-2 frasi che agganciano il problema reale dell'agente.
- VALORE: il consiglio/template/insight vero, utile anche senza VYNEX, con frase copiabile.
- DEMO_VYNEX: 1-2 frasi naturali che mostrano come VYNEX automatizza proprio questo. NO pitch aggressivo.
- CTA: vedi regola sopra.

═══════════════════════════════════════════════════
OUTPUT
═══════════════════════════════════════════════════

Restituisci SOLO un JSON con questa struttura esatta:

{{
  "subject": "Oggetto email, massimo 60 caratteri, senza emoji, specifico e curioso",
  "preheader": "Preview text ≤100 caratteri che completa il subject",
  "slug": "slug-url-friendly-kebab-case-max-70-char",
  "hook": "Il blocco HOOK, 1-2 frasi",
  "valore_html": "Il blocco VALORE in HTML (usa <p>, <ul><li>, <strong>, <em>, eventuali <code>). Deve contenere almeno UNA frase tra virgolette pronta da copiare. No style inline, no h1/h2, no classi CSS. Lunghezza 120-180 parole.",
  "demo_vynex": "Il blocco DEMO_VYNEX come paragrafo unico, 1-2 frasi, tono naturale",
  "cta_text": "Verbo operativo + oggetto coerente col contenuto. Max 6 parole. Esempi validi: 'Automatizza i follow-up dei clienti' / 'Genera offerte in 30 secondi' / 'Smetti di scrivere report la sera'",
  "cta_url_suffix": "suffisso URL: usa /registrati se CTA invita a creare account, /demo se invita a provare senza account"
}}

JSON valido (stringhe escape corretto, nessun commento).
cta_url_suffix deve iniziare con / e valere: /registrati | /demo | /prezzi | /come-funziona.
"""

    message = await ai_engine._call_claude(prompt, max_tokens=2048)
    raw = message.content[0].text
    data = ai_engine.extract_json(raw)

    required = ("subject", "preheader", "slug", "hook", "valore_html", "demo_vynex", "cta_text", "cta_url_suffix")
    for k in required:
        if not data.get(k):
            raise ValueError(f"Claude newsletter: missing '{k}' in response")

    cta_url = data["cta_url_suffix"]
    if not cta_url.startswith("/"):
        cta_url = "/" + cta_url
    if not cta_url.startswith(("/registrati", "/demo", "/prezzi", "/come-funziona", "/blog", "/")):
        cta_url = "/registrati"

    # Post-validation: CTA must be operative + topic-coherent, not generic.
    # If Claude ignored the rule, force a second pass that rewrites ONLY the CTA.
    data["cta_text"] = await _enforce_cta_quality(
        cta_text=data["cta_text"],
        subject=data["subject"],
        hook=data["hook"],
        valore_html=data["valore_html"],
    )

    now = datetime.utcnow()
    slug_base = _slug(data["slug"] or data["subject"])
    slug = f"{now.strftime('%Y%m%d')}-{slug_base}"

    body_html = render_html(
        subject=data["subject"],
        preheader=data["preheader"],
        hook=data["hook"],
        valore_html=data["valore_html"],
        demo_vynex=data["demo_vynex"],
        cta_text=data["cta_text"],
        cta_url=cta_url,
        topic_label=conf["label"],
    )
    body_plain = _strip_html(f"{data['hook']}\n\n{data['valore_html']}\n\n{data['demo_vynex']}\n\n→ {data['cta_text']}: {BASE_URL}{cta_url}")

    issue = NewsletterIssue(
        slug=slug,
        topic_type=topic_type,
        subject=data["subject"][:200],
        preheader=data["preheader"][:200],
        hook=data["hook"],
        body_html=body_html,
        body_plain=body_plain,
        cta_text=data["cta_text"][:80],
        cta_url=cta_url[:500],
        status="draft",
        scheduled_for=now,
    )

    own_db = db is None
    session = AsyncSessionLocal() if own_db else db
    try:
        session.add(issue)
        await session.commit()
        await session.refresh(issue)
        logger.info("newsletter issue generated: id=%d slug=%s topic=%s", issue.id, issue.slug, topic_type)
        return issue
    finally:
        if own_db:
            await session.close()


_CTA_BANNED_PATTERNS = (
    r"\bprova\s+(vynex|gratis)\b",
    r"\bregistrati\b",
    r"\binizia\s+(gratis|ora)\b",
    r"\biscriviti\b",
    r"\bscopri\s+di\s+pi(ù|u)\b",
    r"\bclicca\s+qui\b",
    r"\bvai\s+al\s+sito\b",
)
_CTA_BANNED_RE = re.compile("|".join(_CTA_BANNED_PATTERNS), re.IGNORECASE)
_CTA_OPERATIVE_VERBS = (
    "automatizza", "genera", "smetti", "riduci", "crea", "recupera",
    "trasforma", "risparmia", "evita", "elimina", "scrivi", "ottieni",
    "chiudi", "accelera", "prepara", "invia", "organizza", "prendi",
)


async def _enforce_cta_quality(*, cta_text: str, subject: str, hook: str, valore_html: str) -> str:
    """If the CTA is generic or missing an operative verb, call Claude again
    for a single-shot rewrite. Falls back to a deterministic template if the
    second pass also fails."""
    raw = (cta_text or "").strip().strip(".!→")
    lower = raw.lower()
    banned = _CTA_BANNED_RE.search(lower) is not None
    has_verb = any(lower.startswith(v) or f" {v} " in f" {lower} " for v in _CTA_OPERATIVE_VERBS)
    too_long = len(raw.split()) > 7
    if raw and not banned and has_verb and not too_long:
        return raw

    logger.info("CTA failed validation (text=%r banned=%s verb=%s len=%d) — rewriting", raw, banned, has_verb, len(raw.split()))

    valore_plain = _strip_html(valore_html)[:500]
    prompt = f"""La CTA attuale per questa newsletter VYNEX non rispetta le regole:

Subject: {subject}
Hook: {hook}
Contenuto (estratto): {valore_plain}
CTA attuale: "{raw}"

Regole che la CTA deve rispettare (OBBLIGATORIE):
- Inizia con UN verbo imperativo operativo (Automatizza, Genera, Smetti, Riduci, Crea, Recupera, Trasforma, Risparmia, Evita, Elimina, Scrivi, Ottieni, Chiudi, Accelera, Prepara, Invia, Organizza, Prendi).
- È coerente col tema dell'email (se parla di follow-up → riguarda follow-up; se parla di offerte → riguarda offerte; se parla di report → riguarda report).
- Massimo 6 parole.
- NON usa "Prova gratis", "Prova VYNEX", "Registrati", "Iscriviti", "Scopri", "Clicca qui".

Esempi validi:
- Email su follow-up: "Automatizza i follow-up dei clienti"
- Email su offerte: "Genera offerte in 30 secondi"
- Email su report di visita: "Smetti di scrivere report la sera"
- Email su riattivazione clienti: "Recupera i clienti silenziosi"

Restituisci SOLO la nuova CTA, una singola riga di testo (nessuna virgoletta, nessuna spiegazione, nessun emoji, nessuna freccia). Massimo 6 parole."""

    try:
        msg = await ai_engine._call_claude(prompt, max_tokens=60)
        new_raw = msg.content[0].text.strip().strip('"').strip("'").strip(".!→").split("\n")[0].strip()
        new_lower = new_raw.lower()
        new_ok = (
            new_raw
            and not _CTA_BANNED_RE.search(new_lower)
            and any(new_lower.startswith(v) for v in _CTA_OPERATIVE_VERBS)
            and len(new_raw.split()) <= 7
        )
        if new_ok:
            logger.info("CTA rewritten: %r", new_raw)
            return new_raw
        logger.warning("CTA rewrite still invalid: %r — using deterministic fallback", new_raw)
    except Exception:
        logger.exception("CTA rewrite Claude call failed — falling back")

    # Deterministic fallback: pick a safe topical CTA based on keywords in the subject.
    subj_l = (subject or "").lower()
    if any(k in subj_l for k in ("follow-up", "follow up", "silenzio", "riattiv", "ricontatt")):
        return "Automatizza i follow-up dei clienti"
    if any(k in subj_l for k in ("offerta", "offerte", "preventivo", "preventivi")):
        return "Genera offerte in 30 secondi"
    if any(k in subj_l for k in ("report", "visita", "resoconto")):
        return "Smetti di scrivere report la sera"
    if any(k in subj_l for k in ("email", "messaggio", "whatsapp")):
        return "Scrivi email di follow-up subito"
    return "Automatizza i documenti post-visita"


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ─── HTML template (inline CSS, gmail-safe, dark theme) ────────────────────────

def render_html(
    *,
    subject: str,
    preheader: str,
    hook: str,
    valore_html: str,
    demo_vynex: str,
    cta_text: str,
    cta_url: str,
    topic_label: str,
    unsub_url: str = "{{UNSUB_URL}}",
    recipient_name: str = "{{NAME}}",
) -> str:
    cta_full = cta_url if cta_url.startswith("http") else f"{BASE_URL}{cta_url}"
    year = datetime.utcnow().year

    return f"""<!DOCTYPE html>
<html lang="it"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<title>{_escape(subject)}</title>
<style>
@media (max-width: 620px) {{
  .wrap {{ padding: 16px 8px !important; }}
  .card {{ padding: 24px 20px !important; }}
  .hero-title {{ font-size: 24px !important; line-height: 1.25 !important; }}
  .btn-cta {{ font-size: 15px !important; padding: 14px 22px !important; }}
}}
</style>
</head>
<body style="margin:0;padding:0;background:#04060f;font-family:-apple-system,'Segoe UI',Inter,Roboto,sans-serif;color:#e2e8f0;">
<!-- preheader -->
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;color:transparent;font-size:1px;line-height:1px;opacity:0">{_escape(preheader)}</div>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#04060f;padding:0;margin:0"><tr><td align="center">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" class="wrap" style="max-width:600px;width:100%;padding:28px 12px">
    <!-- logo row -->
    <tr><td style="padding:8px 12px 20px 12px">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="vertical-align:middle">
            <span style="font-size:22px;font-weight:800;letter-spacing:4px;color:#60a5fa">VYNEX</span>
          </td>
          <td align="right" style="vertical-align:middle">
            <span style="font-size:10px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:#64748b;padding:5px 10px;border:1px solid #1e293b;border-radius:100px">{_escape(topic_label)}</span>
          </td>
        </tr>
      </table>
    </td></tr>

    <!-- HERO card -->
    <tr><td>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="card" style="background:linear-gradient(180deg,#0f172a 0%,#0a0f1e 100%);border:1px solid rgba(96,165,250,.2);border-radius:20px;padding:36px 34px">
        <tr><td>
          <h1 class="hero-title" style="margin:0 0 14px;font-size:28px;line-height:1.2;font-weight:800;color:#f1f5f9;letter-spacing:-.01em">
            {_escape(subject)}
          </h1>
          <p style="margin:0 0 18px;font-size:15px;color:#60a5fa;font-weight:600">
            {_escape(hook)}
          </p>
          <div style="height:1px;background:linear-gradient(90deg,transparent,rgba(96,165,250,.35),transparent);margin:18px 0 22px"></div>

          <!-- VALORE -->
          <div style="color:#cbd5e1;font-size:15.5px;line-height:1.75">
            {valore_html}
          </div>

          <!-- DEMO_VYNEX -->
          <div style="margin:26px 0 4px;padding:18px 20px;background:linear-gradient(135deg,rgba(59,130,246,.1),rgba(139,92,246,.1));border:1px solid rgba(96,165,250,.25);border-radius:14px">
            <div style="font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#93c5fd;margin-bottom:8px">Con VYNEX</div>
            <div style="color:#e2e8f0;font-size:14.5px;line-height:1.65">
              {_escape(demo_vynex)}
            </div>
          </div>

          <!-- CTA -->
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:28px"><tr><td align="center">
            <a href="{cta_full}" class="btn-cta" style="display:inline-block;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#ffffff;text-decoration:none;padding:16px 32px;border-radius:12px;font-size:16px;font-weight:700;letter-spacing:.3px;box-shadow:0 8px 24px rgba(59,130,246,.35)">
              {_escape(cta_text)} &rarr;
            </a>
          </td></tr></table>

          <p style="margin:22px 0 0;text-align:center;font-size:12.5px;color:#64748b;line-height:1.6">
            10 documenti gratis ogni mese · Nessuna carta di credito · Attivo in 30 secondi
          </p>
        </td></tr>
      </table>
    </td></tr>

    <!-- footer -->
    <tr><td style="padding:28px 18px 40px 18px;text-align:center">
      <p style="margin:0 0 10px;color:#94a3b8;font-size:13px;line-height:1.6">
        Ricevi questa email perché ti sei iscritto alla newsletter VYNEX.
      </p>
      <p style="margin:0 0 16px;color:#64748b;font-size:12px;line-height:1.6">
        <a href="{unsub_url}" style="color:#60a5fa;text-decoration:underline">Disiscriviti</a> ·
        <a href="{BASE_URL}" style="color:#60a5fa;text-decoration:none">vynex.it</a> ·
        <a href="mailto:robertopizzini19@gmail.com" style="color:#60a5fa;text-decoration:none">Contatti</a>
      </p>
      <p style="margin:0;color:#475569;font-size:11px;line-height:1.5">
        © {year} VYNEX · AI italiana per agenti commerciali · Dati in EU (Frankfurt)
      </p>
    </td></tr>
  </table>
</td></tr></table>

</body></html>"""


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )


# ─── unsubscribe tokens ────────────────────────────────────────────────────────

def user_unsub_token(user_id: int) -> str:
    msg = f"user:{user_id}".encode()
    return hmac.new(SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()[:40]


def verify_user_unsub_token(user_id: int, token: str) -> bool:
    return hmac.compare_digest(user_unsub_token(user_id), token)


# ─── sender ────────────────────────────────────────────────────────────────────

async def _gather_recipients(db: AsyncSession) -> list[tuple[str, str, str]]:
    """Returns list of (email, display_name, unsub_url)."""
    recipients: dict[str, tuple[str, str]] = {}

    # 1. Leads opted-in and not unsubscribed
    lq = await db.execute(
        select(Lead)
        .where(Lead.newsletter_opted_in.is_(True))
        .where(Lead.unsubscribed.is_(False))
    )
    for lead in lq.scalars().all():
        name = (lead.full_name or lead.email.split("@")[0]).split()[0]
        unsub = f"{BASE_URL}/newsletter/unsubscribe/lead/{lead.unsub_token}"
        recipients[lead.email.lower()] = (name, unsub)

    # 2. Active users with newsletter opt-in (defaults TRUE at signup)
    uq = await db.execute(
        select(User)
        .where(User.newsletter_opted_in.is_(True))
        .where(User.is_active.is_(True))
        .where(User.deleted_at.is_(None))
    )
    for user in uq.scalars().all():
        name = (user.full_name or user.email.split("@")[0]).split()[0]
        token = user_unsub_token(user.id)
        unsub = f"{BASE_URL}/newsletter/unsubscribe/user/{user.id}/{token}"
        # user opt-in overrides lead with same email (user is authoritative)
        recipients[user.email.lower()] = (name, unsub)

    return [(email, name, unsub) for email, (name, unsub) in recipients.items()]


async def send_issue(issue_id: int, db: Optional[AsyncSession] = None) -> dict:
    """Send a pre-generated draft issue to all opted-in recipients.

    Returns counts dict: {recipients, sent, failed}.
    """
    own_db = db is None
    session = AsyncSessionLocal() if own_db else db
    try:
        iq = await session.execute(select(NewsletterIssue).where(NewsletterIssue.id == issue_id))
        issue: NewsletterIssue | None = iq.scalar_one_or_none()
        if not issue:
            raise ValueError(f"Newsletter issue {issue_id} not found")
        if issue.status == "sent":
            logger.warning("issue %d already sent, skipping", issue.id)
            return {"recipients": 0, "sent": 0, "failed": 0, "skipped": True}

        recipients = await _gather_recipients(session)
        logger.info("newsletter issue %d: %d recipients", issue.id, len(recipients))

        sent = 0
        failed = 0
        for email, name, unsub_url in recipients:
            try:
                html = (
                    issue.body_html
                    .replace("{{UNSUB_URL}}", unsub_url)
                    .replace("{{NAME}}", name)
                )
                ok = await send_raw(email, issue.subject, html)
                if ok:
                    sent += 1
                else:
                    failed += 1
                # soft throttle: 20 msg/sec max
                if sent % 20 == 0:
                    await asyncio.sleep(1.0)
            except Exception:
                logger.exception("newsletter send failed for %s", email)
                failed += 1

        issue.recipients_count = len(recipients)
        issue.sent_count = sent
        issue.failed_count = failed
        issue.status = "sent" if failed < len(recipients) else "failed"
        issue.sent_at = datetime.utcnow()
        await session.commit()

        logger.info("newsletter issue %d sent: %d ok, %d fail", issue.id, sent, failed)
        return {"recipients": len(recipients), "sent": sent, "failed": failed}
    finally:
        if own_db:
            await session.close()


# ─── orchestrator used by scheduler ────────────────────────────────────────────

async def generate_and_send(topic_type: str) -> dict:
    """One-shot: generate then send. Used by APScheduler jobs."""
    async with AsyncSessionLocal() as db:
        issue = await generate_issue(topic_type=topic_type, db=db)
        counts = await send_issue(issue.id, db=db)
    return {"issue_id": issue.id, "slug": issue.slug, **counts}
