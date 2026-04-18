"""
Email templates per il motore di acquisizione autonoma.

Ogni template è identificato da una `campaign_key`. Un job in EmailJob porta
solo la chiave + i contesti minimi — il corpo viene renderizzato al momento
dell'invio. Così i template si possono modificare senza migrazione dati.

Audience:
  - `lead`  → template consumati da prospect (demo o cold import), rendering
              con ctx `{name, unsub_url, base_url, ...}`
  - `user`  → template consumati da utenti registrati (drip onboarding),
              rendering con ctx `{name, unsub_url, base_url, dashboard_url}`
"""
from __future__ import annotations

import os
from dataclasses import dataclass


BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")


def _wrap(body_html: str, footer_unsub: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="it"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#04060f;font-family:-apple-system,Segoe UI,Inter,sans-serif;color:#f1f5f9">
<div style="max-width:560px;margin:0 auto;padding:40px 24px">
  <div style="font-size:22px;font-weight:800;letter-spacing:3px;color:#60a5fa;margin-bottom:24px">VYNEX</div>
  {body_html}
  <div style="margin-top:40px;padding-top:24px;border-top:1px solid #1e293b;color:#64748b;font-size:12px;line-height:1.6">
    VYNEX — AI per agenti commerciali italiani<br>
    <a href="{{base_url}}" style="color:#60a5fa">{{base_url}}</a>{footer_unsub}
  </div>
</div></body></html>"""


def _btn(href: str, label: str) -> str:
    return (
        f'<a href="{href}" style="display:inline-block;background:linear-gradient(135deg,#3b82f6,#8b5cf6);'
        f'color:#fff;padding:14px 28px;border-radius:10px;text-decoration:none;font-weight:600">{label}</a>'
    )


def _unsub_footer() -> str:
    return (
        '<br><br><a href="{unsub_url}" style="color:#64748b;text-decoration:underline">'
        "Non voglio più ricevere email</a>"
    )


@dataclass(frozen=True)
class Campaign:
    key: str
    audience: str  # "lead" | "user"
    delay_hours: int
    subject: str
    body_html: str  # contiene placeholder {name}, {base_url}, {unsub_url}, ...


# ──────────────────────────────────────────────────────────────────────────────
# DRIP per LEAD (arrivati via /demo o import admin)
# ──────────────────────────────────────────────────────────────────────────────

_LEAD_DEMO_RESULT = Campaign(
    key="lead_demo_result",
    audience="lead",
    delay_hours=0,
    subject="I tuoi 3 documenti VYNEX sono pronti",
    body_html=_wrap(
        """
        <h1 style="font-size:24px;color:#f1f5f9;line-height:1.3">Ciao {name}, i tuoi documenti sono pronti.</h1>
        <p style="color:#cbd5e1;line-height:1.7">Ti ho generato 3 documenti commerciali partendo dalla tua descrizione — report di visita, email di follow-up e offerta. In totale: <strong>28 secondi</strong>.</p>
        <p style="color:#cbd5e1;line-height:1.7">Li trovi nel tuo link privato qui sotto (valido 7 giorni):</p>
        <p style="text-align:center;margin:28px 0">{cta}</p>
        <p style="color:#94a3b8;line-height:1.7;font-size:14px">Se vuoi generare documenti illimitati sui tuoi clienti reali, l'account gratuito ti dà 10 documenti al mese per sempre, senza carta.</p>
        """,
        _unsub_footer(),
    ).replace("{cta}", _btn("{demo_url}", "Scarica i 3 documenti")),
)

_LEAD_CASE_STUDY = Campaign(
    key="lead_drip_1_case_study",
    audience="lead",
    delay_hours=24,
    subject="Come Marco ha chiuso 3 contratti in più la settimana scorsa",
    body_html=_wrap(
        """
        <h1 style="font-size:22px;color:#f1f5f9;line-height:1.3">Il tempo speso a scrivere report è il tempo che non usi per vendere.</h1>
        <p style="color:#cbd5e1;line-height:1.7">Marco è agente di commercio a Brescia. Faceva 8-10 visite/settimana. Ogni sera perdeva 2 ore a scrivere report e mandare follow-up.</p>
        <p style="color:#cbd5e1;line-height:1.7">Con VYNEX, quei 120 minuti sono diventati 4. Il resto del tempo l'ha messo su 3 clienti nuovi. Due hanno firmato.</p>
        <p style="color:#cbd5e1;line-height:1.7"><strong>Non è AI magia</strong> — è sottrazione di friction. Tu descrivi la visita in 2 righe, VYNEX produce il documento come lo faresti tu. Solo più veloce.</p>
        <p style="text-align:center;margin:28px 0">{cta}</p>
        """,
        _unsub_footer(),
    ).replace("{cta}", _btn("{base_url}/demo", "Prova VYNEX (3 doc gratis)")),
)

_LEAD_FEATURE_FOLLOWUP = Campaign(
    key="lead_drip_2_feature_followup",
    audience="lead",
    delay_hours=72,
    subject="La feature che ti fa chiudere: follow-up email AI",
    body_html=_wrap(
        """
        <h1 style="font-size:22px;color:#f1f5f9;line-height:1.3">L'80% delle trattative si perde nel silenzio post-visita.</h1>
        <p style="color:#cbd5e1;line-height:1.7">Dopo un incontro promettente, 4 agenti su 5 non mandano un follow-up nelle 24 ore. Non per pigrizia — per non sapere come chiuderlo senza sembrare insistenti.</p>
        <p style="color:#cbd5e1;line-height:1.7">VYNEX scrive il follow-up <em>nel tuo tono</em>, richiamando i punti dell'incontro, proponendo il prossimo step senza pressing. Copia, invia, chiudi.</p>
        <p style="text-align:center;margin:28px 0">{cta}</p>
        """,
        _unsub_footer(),
    ).replace("{cta}", _btn("{base_url}/registrati", "Attiva account gratuito")),
)

_LEAD_SOCIAL_PROOF = Campaign(
    key="lead_drip_3_social_proof",
    audience="lead",
    delay_hours=168,
    subject="Siamo 47 agenti. Ti aspettiamo.",
    body_html=_wrap(
        """
        <h1 style="font-size:22px;color:#f1f5f9;line-height:1.3">VYNEX è costruito <em>in</em> Italia, <em>per</em> Italia.</h1>
        <p style="color:#cbd5e1;line-height:1.7">Niente AI generica tradotta male. Il modello è pre-addestrato sul linguaggio della vendita B2B italiana — partite IVA, tono formale ma non ingessato, strutture di offerta adatte ai nostri contratti.</p>
        <p style="color:#cbd5e1;line-height:1.7">In questa settimana: 47 agenti attivi, 412 documenti generati, 23 contratti chiusi riportati.</p>
        <p style="text-align:center;margin:28px 0">{cta}</p>
        <p style="color:#94a3b8;line-height:1.7;font-size:14px">Il piano gratuito include 10 documenti/mese, per sempre. Carta di credito: non richiesta.</p>
        """,
        _unsub_footer(),
    ).replace("{cta}", _btn("{base_url}/registrati", "Unisciti al gruppo")),
)

_LEAD_FINAL_OFFER = Campaign(
    key="lead_drip_4_final_offer",
    audience="lead",
    delay_hours=336,
    subject="-20% sul primo mese Pro (scade tra 48h)",
    body_html=_wrap(
        """
        <h1 style="font-size:22px;color:#f1f5f9;line-height:1.3">Ultima spinta, {name}.</h1>
        <p style="color:#cbd5e1;line-height:1.7">Hai provato VYNEX. Sai che funziona. So che l'ostacolo è quel passo di attivare l'account.</p>
        <p style="color:#cbd5e1;line-height:1.7">Ti sblocco il primo mese Pro a <strong>€39,20 invece di €49</strong> con il codice <code style="background:#1e293b;padding:4px 8px;border-radius:4px;color:#60a5fa">BENVENUTO20</code>. Scade tra 48 ore.</p>
        <p style="text-align:center;margin:28px 0">{cta}</p>
        <p style="color:#94a3b8;line-height:1.7;font-size:14px">Puoi disdire in un click dal portale fatturazione. Nessuna trappola. Solo 30 giorni con documenti illimitati per vedere cosa cambia nel tuo lavoro.</p>
        """,
        _unsub_footer(),
    ).replace("{cta}", _btn("{base_url}/registrati", "Attiva con sconto -20%")),
)


# ──────────────────────────────────────────────────────────────────────────────
# DRIP per USER (onboarding post-signup, NON cold — no unsub link commerciale)
# ──────────────────────────────────────────────────────────────────────────────

_USER_AHA_PUSH = Campaign(
    key="user_drip_1_aha_push",
    audience="user",
    delay_hours=2,
    subject="Genera il primo documento in 30 secondi",
    body_html=_wrap(
        """
        <h1 style="font-size:22px;color:#f1f5f9;line-height:1.3">Ciao {name}, un piccolo invito.</h1>
        <p style="color:#cbd5e1;line-height:1.7">Hai attivato l'account 2 ore fa. Se non hai ancora generato il primo documento, prova ora — bastano 30 secondi e una visita che hai fatto questa settimana.</p>
        <p style="color:#cbd5e1;line-height:1.7">Descrivi in 2-3 righe: chi hai incontrato, cosa hai proposto, cosa ti hanno detto. VYNEX fa il resto.</p>
        <p style="text-align:center;margin:28px 0">{cta}</p>
        """,
    ).replace("{cta}", _btn("{base_url}/genera", "Genera ora")),
)

_USER_FEATURE_DEEP = Campaign(
    key="user_drip_2_feature_deep",
    audience="user",
    delay_hours=72,
    subject="3 modi per usare VYNEX che non sapevi",
    body_html=_wrap(
        """
        <h1 style="font-size:22px;color:#f1f5f9;line-height:1.3">Tre trucchi che i nostri utenti migliori usano ogni giorno.</h1>
        <ol style="color:#cbd5e1;line-height:1.8">
          <li><strong>Riga "Obiezione cliente: X"</strong> → VYNEX scrive la risposta pronta nell'email di follow-up</li>
          <li><strong>Riga "Urgenza: alta/bassa"</strong> → calibra il tono dell'offerta, con o senza pressing</li>
          <li><strong>Riga "Settore: meccanico/sanitario/edile"</strong> → adatta lessico e riferimenti normativi</li>
        </ol>
        <p style="color:#cbd5e1;line-height:1.7">Non sono feature nascoste — sono modi di usare il campo descrizione che moltiplicano la qualità dell'output.</p>
        <p style="text-align:center;margin:28px 0">{cta}</p>
        """,
    ).replace("{cta}", _btn("{base_url}/genera", "Apri generatore")),
)

_USER_UPGRADE_NUDGE = Campaign(
    key="user_drip_3_upgrade_nudge",
    audience="user",
    delay_hours=240,
    subject="Hai spinto VYNEX al limite free — facciamo sul serio?",
    body_html=_wrap(
        """
        <h1 style="font-size:22px;color:#f1f5f9;line-height:1.3">Se usi 10 documenti al mese, ne stai lasciando 30 a terra.</h1>
        <p style="color:#cbd5e1;line-height:1.7">Un agente in attività reale fa 8-12 visite/settimana. A questo ritmo il piano Free copre meno di 3 giorni lavorativi.</p>
        <p style="color:#cbd5e1;line-height:1.7">Il piano Pro sblocca documenti illimitati, a €49/mese. Se in 30 giorni non chiudi <em>almeno un contratto</em> che altrimenti non avresti chiuso, disdici e basta.</p>
        <p style="text-align:center;margin:28px 0">{cta}</p>
        """,
    ).replace("{cta}", _btn("{base_url}/prezzi", "Passa a Pro")),
)


# ──────────────────────────────────────────────────────────────────────────────
# COLD OUTREACH (import admin CSV)
# ──────────────────────────────────────────────────────────────────────────────

_COLD_TOUCH_1 = Campaign(
    key="cold_touch_1_intro",
    audience="lead",
    delay_hours=0,
    subject="Quanto tempo perdi a scrivere report dopo le visite?",
    body_html=_wrap(
        """
        <p style="color:#cbd5e1;line-height:1.7">Ciao {name},</p>
        <p style="color:#cbd5e1;line-height:1.7">Ho visto il tuo profilo nel settore commerciale. Faccio una domanda rapida e poi ti lascio in pace: quanto tempo al giorno spendi a scrivere report di visita, email di follow-up e offerte?</p>
        <p style="color:#cbd5e1;line-height:1.7">Te lo chiedo perché lavoro su uno strumento AI — <strong>VYNEX</strong> — costruito in Italia, pensato solo per chi fa vendita B2B sul territorio. Non è un CRM: è un generatore di documenti che riduce quelle 2 ore a sera a 5 minuti.</p>
        <p style="color:#cbd5e1;line-height:1.7">Ti lascio il link alla demo gratuita (3 documenti senza account): {base_url}/demo</p>
        <p style="color:#cbd5e1;line-height:1.7">Se non è il tuo problema, ignora — ho fatto la mia parte. Se invece risuona, prova e dimmi com'è andata.</p>
        <p style="color:#cbd5e1;line-height:1.7">Roberto</p>
        """,
        _unsub_footer(),
    ),
)

_COLD_TOUCH_2 = Campaign(
    key="cold_touch_2_followup",
    audience="lead",
    delay_hours=96,
    subject="Re: report visite",
    body_html=_wrap(
        """
        <p style="color:#cbd5e1;line-height:1.7">Ciao {name}, ripasso veloce.</p>
        <p style="color:#cbd5e1;line-height:1.7">L'ho mandata a una settantina di agenti questa settimana. Tre risposte interessanti:</p>
        <ul style="color:#cbd5e1;line-height:1.8">
          <li><em>"Ho provato Copilot, ma scriveva in italiano da curriculum"</em></li>
          <li><em>"Il mio problema non è scrivere, è uniformare: ogni cliente ha un formato diverso"</em></li>
          <li><em>"Pago già un CRM che non uso. Se aggiunge lavoro manuale, no grazie"</em></li>
        </ul>
        <p style="color:#cbd5e1;line-height:1.7">VYNEX risolve tutti e tre i punti. La demo è qui, 30 secondi: {base_url}/demo</p>
        <p style="color:#cbd5e1;line-height:1.7">Roberto</p>
        """,
        _unsub_footer(),
    ),
)

_COLD_TOUCH_3 = Campaign(
    key="cold_touch_3_last",
    audience="lead",
    delay_hours=240,
    subject="Ultima email — poi ti cancello",
    body_html=_wrap(
        """
        <p style="color:#cbd5e1;line-height:1.7">{name}, onesto:</p>
        <p style="color:#cbd5e1;line-height:1.7">Se non rispondi a questa, esco io dalla tua casella. Nessun "ti ricordi di me tra 3 mesi" — la mia regola è 3 email, poi basta.</p>
        <p style="color:#cbd5e1;line-height:1.7">Ti lascio due link e decidi tu.</p>
        <p style="color:#cbd5e1;line-height:1.7">1) Demo VYNEX (gratis, no carta): {base_url}/demo</p>
        <p style="color:#cbd5e1;line-height:1.7">2) Disiscrizione immediata dalla mia lista (link qui sotto)</p>
        <p style="color:#cbd5e1;line-height:1.7">Roberto</p>
        """,
        _unsub_footer(),
    ),
)


# ──────────────────────────────────────────────────────────────────────────────
# REFERRAL notifications
# ──────────────────────────────────────────────────────────────────────────────

_USER_NPS_T7 = Campaign(
    key="user_nps_t7",
    audience="user",
    delay_hours=0,
    subject="Come è andata la prima settimana con VYNEX?",
    body_html=_wrap(
        """
        <h1 style="font-size:22px;color:#f1f5f9;line-height:1.3">Ciao {name}, una domanda veloce.</h1>
        <p style="color:#cbd5e1;line-height:1.7">Sei con VYNEX da una settimana. Raccomanderesti lo strumento a un collega agente di commercio? Bastano 3 secondi per rispondere: da 0 a 10.</p>
        <p style="text-align:center;margin:28px 0">{cta}</p>
        <p style="color:#94a3b8;line-height:1.7;font-size:14px">Ogni feedback serve a farti trovare VYNEX più utile. Grazie.</p>
        """,
    ).replace("{cta}", _btn("{nps_url}", "Dammi il mio voto (1 click)")),
)

_USER_NPS_T30 = Campaign(
    key="user_nps_t30",
    audience="user",
    delay_hours=0,
    subject="Un mese di VYNEX — raccomanderesti?",
    body_html=_wrap(
        """
        <h1 style="font-size:22px;color:#f1f5f9;line-height:1.3">Ciao {name}, dopo un mese con VYNEX.</h1>
        <p style="color:#cbd5e1;line-height:1.7">Hai usato VYNEX per un mese intero. A freddo: da 0 a 10, lo raccomanderesti a un collega agente italiano?</p>
        <p style="text-align:center;margin:28px 0">{cta}</p>
        <p style="color:#94a3b8;line-height:1.7;font-size:14px">Un click + 1 frase di commento ci aiuta più di qualsiasi altra cosa. Niente venditori, niente call di 30 minuti.</p>
        """,
    ).replace("{cta}", _btn("{nps_url}", "Dai il tuo voto")),
)

_USER_WINBACK = Campaign(
    key="user_winback",
    audience="user",
    delay_hours=0,
    subject="{name}, tutto ok? Ci sei ancora?",
    body_html=_wrap(
        """
        <h1 style="font-size:22px;color:#f1f5f9;line-height:1.3">Ciao {name},</h1>
        <p style="color:#cbd5e1;line-height:1.7">Il tuo account Pro VYNEX è attivo ma non hai generato documenti nelle ultime 3 settimane. Capisco — la vita dell'agente è imprevedibile.</p>
        <p style="color:#cbd5e1;line-height:1.7">Due opzioni rapide:</p>
        <ul style="color:#cbd5e1;line-height:1.8">
          <li><strong>Se vuoi continuare:</strong> apri {base_url}/genera e testa col codice <code style="background:#1e293b;padding:3px 8px;border-radius:4px;color:#60a5fa">TORNA30</code> — 30 giorni gratis bonus sul tuo prossimo addebito.</li>
          <li><strong>Se non ti serve più:</strong> puoi disdire in 1 click dal portale fatturazione. Nessun problema, nessuna domanda.</li>
        </ul>
        <p style="text-align:center;margin:28px 0">{cta}</p>
        <p style="color:#94a3b8;line-height:1.7;font-size:14px">Se vuoi darmi feedback su cosa non funziona per te, rispondi a questa email. Ci tengo davvero.</p>
        <p style="color:#94a3b8;line-height:1.7;font-size:14px">Roberto</p>
        """,
    ).replace("{cta}", _btn("{base_url}/genera", "Apri generatore")),
)


_REFERRAL_CONVERTED = Campaign(
    key="referral_converted",
    audience="user",
    delay_hours=0,
    subject="Un tuo invito ha funzionato — ecco 1 mese gratis",
    body_html=_wrap(
        """
        <h1 style="font-size:22px;color:#f1f5f9;line-height:1.3">Congratulazioni, {name}.</h1>
        <p style="color:#cbd5e1;line-height:1.7">Un agente che hai invitato ha appena attivato un piano Pro. Come promesso: 30 giorni di VYNEX Pro aggiunti gratis al tuo account.</p>
        <p style="color:#cbd5e1;line-height:1.7">Hai già <strong>{referrals_count}</strong> inviti convertiti. Ogni due conversioni = 1 mese gratis.</p>
        <p style="text-align:center;margin:28px 0">{cta}</p>
        """,
    ).replace("{cta}", _btn("{base_url}/dashboard", "Apri dashboard")),
)


# ──────────────────────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────────────────────

CAMPAIGNS: dict[str, Campaign] = {
    c.key: c
    for c in [
        _LEAD_DEMO_RESULT,
        _LEAD_CASE_STUDY,
        _LEAD_FEATURE_FOLLOWUP,
        _LEAD_SOCIAL_PROOF,
        _LEAD_FINAL_OFFER,
        _USER_AHA_PUSH,
        _USER_FEATURE_DEEP,
        _USER_UPGRADE_NUDGE,
        _COLD_TOUCH_1,
        _COLD_TOUCH_2,
        _COLD_TOUCH_3,
        _USER_NPS_T7,
        _USER_NPS_T30,
        _USER_WINBACK,
        _REFERRAL_CONVERTED,
    ]
}


# Ordered sequences (follow-up chains enqueued together at enrollment time).
SEQUENCE_LEAD_DEMO = [
    "lead_demo_result",
    "lead_drip_1_case_study",
    "lead_drip_2_feature_followup",
    "lead_drip_3_social_proof",
    "lead_drip_4_final_offer",
]

SEQUENCE_USER_SIGNUP = [
    "user_drip_1_aha_push",
    "user_drip_2_feature_deep",
    "user_drip_3_upgrade_nudge",
]

SEQUENCE_COLD = [
    "cold_touch_1_intro",
    "cold_touch_2_followup",
    "cold_touch_3_last",
]


def render(campaign_key: str, ctx: dict) -> tuple[str, str]:
    """Render (subject, html) for a campaign. Missing keys in ctx raise KeyError."""
    c = CAMPAIGNS[campaign_key]
    defaults = {"base_url": BASE_URL, "unsub_url": f"{BASE_URL}/unsubscribe/__MISSING__"}
    merged = {**defaults, **ctx}
    subject = c.subject.format_map(_SafeDict(merged))
    html = c.body_html.format_map(_SafeDict(merged))
    return subject, html


class _SafeDict(dict):
    """dict che sostituisce placeholder mancanti con stringa vuota invece di KeyError.

    Usato in rendering email: se ctx non ha `demo_url` su un template che non lo
    usa davvero, evita un crash in produzione.
    """
    def __missing__(self, key: str) -> str:
        return ""
