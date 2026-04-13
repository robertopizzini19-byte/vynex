# VYNEX — Documenti commerciali in 30 secondi

SaaS italiano per agenti di commercio: da una descrizione informale della visita (testo) genera in 30 secondi **report di visita + email di follow-up + offerta commerciale** pronti da inviare. Pensato per i ~220.000 *agenti di commercio* italiani.

> **Nome:** VYNEX (con la Y) — breve, memorizzabile, pronunciabile ovunque.
> **Posizionamento:** zero concorrenza verticale in Italia. AI commerciale pensata per chi vende, non per chi programma.

## Stack
- **Backend:** FastAPI 0.115 + SQLAlchemy 2.0 async
- **DB:** Supabase Postgres via asyncpg (session pooler 5432) · fallback SQLite locale
- **AI:** Claude Haiku 4.5 via Anthropic SDK
- **Pagamenti:** Stripe (checkout, billing portal, webhooks idempotenti)
- **Auth:** JWT in cookie HttpOnly + bcrypt · password reset via email
- **Email:** Resend (transazionali) con fallback no-op se chiave assente
- **Rate limiting:** slowapi su endpoint pubblici
- **GDPR:** privacy/termini/cookie policy, cookie banner, accept_terms in registrazione
- **Deploy:** Railway via Dockerfile multi-stage Python 3.11

## Pricing
| Piano | Prezzo | Limite |
|-------|-------|-------|
| Free  | €0 | 10 documenti/mese |
| Pro   | €49/mese | Documenti illimitati + Affina con AI |
| Team  | €89/agente/mese (min 3 agenti) | Dashboard team + brandizzazione |

Prova 10 giorni inclusa su Pro e Team.

## Features
- Descrizione naturale → 3 documenti professionali in < 30s
- Affina con istruzioni AI ("rendi più formale", "aggiungi sconto 10%", ecc.)
- Storico documenti per cliente
- Stripe subscription + billing portal self-service
- Password reset via email
- Email transazionali (welcome, reset, pagamento ok/ko)
- Cookie banner + pagine legali conformi GDPR

## Run locale
```bash
pip install -r requirements.txt
cp .env.example .env    # aggiungi ANTHROPIC_API_KEY, Stripe, Resend
uvicorn main:app --reload
```

## Deploy
```bash
railway up --ci
```

Production: <https://vynex-production-fb78.up.railway.app>

---
*Zero concorrenti verticali in Italia. Costruito da Roberto Pizzini.*
