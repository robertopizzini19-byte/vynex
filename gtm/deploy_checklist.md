# VYNEX — Deploy Checklist

## STEP 1: Credenziali API

### Anthropic
1. Vai su console.anthropic.com
2. Settings → API Keys → Create Key
3. Copia la chiave (sk-ant-...)
4. Inserisci nel .env: ANTHROPIC_API_KEY=sk-ant-...

### Stripe
1. Vai su dashboard.stripe.com
2. Attiva il tuo account (verifica identità)
3. Developers → API Keys:
   - STRIPE_SECRET_KEY=sk_live_... (o sk_test_ per test)
   - STRIPE_PUBLISHABLE_KEY=pk_live_...
4. Esegui setup prodotti:
   ```
   python setup_stripe.py
   ```
   Copia i price_id generati nel .env.

### Resend (email transazionali)
1. Vai su resend.com → API Keys → Create
2. Copia la chiave (re_...)
3. Inserisci nel .env: RESEND_API_KEY=re_...
4. Verifica dominio mittente (vynex.it) per inviare da `ciao@vynex.it`
5. Se chiave non impostata: emailer fa no-op, l'app continua a funzionare

### JWT Secret
Genera una stringa sicura random:
```
python -c "import secrets; print(secrets.token_hex(32))"
```
Inserisci nel .env: SECRET_KEY=...

---

## STEP 2: GitHub

```bash
# Crea repo su github.com → new repository → "vynex" (private)
# Poi:
git remote add origin https://github.com/TUO_USERNAME/vynex.git
git branch -M main
git push -u origin main
```

---

## STEP 3: Railway Deploy

1. Vai su railway.app → New Project → Deploy from GitHub Repo
2. Seleziona il repo "vynex"
3. Railway detecta il Dockerfile automaticamente

**Variabili d'ambiente da aggiungere in Railway (Settings → Variables):**
```
ANTHROPIC_API_KEY=sk-ant-...
STRIPE_SECRET_KEY=sk_live_...
STRIPE_PUBLISHABLE_KEY=pk_live_...
STRIPE_WEBHOOK_SECRET=whsec_... (vedi step 4)
STRIPE_PRO_PRICE_ID=price_...
STRIPE_TEAM_PRICE_ID=price_...
SECRET_KEY=... (stringa random generata sopra)
BASE_URL=https://agentia-production-fb78.up.railway.app
DATABASE_URL=postgresql+asyncpg://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
RESEND_API_KEY=re_...
EMAIL_FROM=VYNEX <ciao@vynex.it>
EMAIL_REPLY_TO=ciao@vynex.it
```

**Nota DATABASE_URL:** usare Supabase **session pooler (port 5432)**, NON transaction pooler 6543 (incompatibile con prepared statements asyncpg).

---

## STEP 4: Stripe Webhook

1. Stripe Dashboard → Developers → Webhooks → Add endpoint
2. URL: https://agentia-production-fb78.up.railway.app/webhook/stripe
3. Events da ascoltare:
   - checkout.session.completed
   - customer.subscription.deleted
   - customer.subscription.paused
   - customer.subscription.updated
   - invoice.payment_failed
4. Copia il Signing Secret (whsec_...) → Railway env: STRIPE_WEBHOOK_SECRET

Il webhook è idempotente: event_id tracciato in tabella `stripe_events`, replay ignorati.

---

## STEP 5: Dominio (opzionale ma consigliato)

**Opzione A — Railway custom domain:**
- Railway → Settings → Domains → Add Custom Domain
- Punta il DNS: CNAME → agentia-production-fb78.up.railway.app

**Opzione B — Cloudflare + Railway:**
- Compra vynex.it su Namecheap (~€12/anno)
- Attiva Cloudflare (free tier)
- CNAME vynex.it → Railway URL
- SSL automatico via Cloudflare

---

## STEP 6: Test pre-lancio

```bash
# Test health
curl https://agentia-production-fb78.up.railway.app/health

# Test pagine pubbliche
curl -I https://agentia-production-fb78.up.railway.app/
curl -I https://agentia-production-fb78.up.railway.app/prezzi
curl -I https://agentia-production-fb78.up.railway.app/privacy
curl -I https://agentia-production-fb78.up.railway.app/termini
curl -I https://agentia-production-fb78.up.railway.app/cookie

# Test registrazione
# Vai su /registrati → accept_terms checkbox → crea account
# Verifica arrivo email di benvenuto (se Resend configurato)

# Test generazione
# Inserisci testo visita → verifica 3 documenti

# Test Stripe (con card test 4242 4242 4242 4242)
# Vai su /checkout/pro
# Completa checkout → verifica piano aggiornato in dashboard

# Test password reset
# /recupera-password → email → link → /reset-password?token=...
```

---

## STEP 7: Go-live checklist

- [ ] Health check risponde 200
- [ ] Registrazione funziona (con accept_terms obbligatorio)
- [ ] Login funziona
- [ ] Password reset funziona end-to-end
- [ ] AI genera 3 documenti
- [ ] Affina con AI funziona (solo Pro/Team)
- [ ] Stripe checkout funziona (test mode)
- [ ] Webhook aggiorna piano utente
- [ ] Webhook idempotente (replay non duplica)
- [ ] Privacy / Termini / Cookie accessibili
- [ ] Cookie banner appare e si ricorda dell'accettazione
- [ ] Email ciao@vynex.it attiva
- [ ] Rate limit attivo (login/registrati/genera)
- [ ] Outreach LinkedIn iniziato

---

## Costi operativi mensili (stima)

| Servizio | Costo |
|----------|-------|
| Railway Hobby | $5/mese |
| Supabase Free | $0/mese (fino a 500MB) |
| Anthropic API (100 gen/mese Haiku 4.5) | ~$2/mese |
| Resend (free 3k/mese) | $0/mese |
| Stripe | 1.4% + €0.25 per transazione |
| Dominio vynex.it | ~€1/mese |
| **Totale fisso** | **~$8/mese** |

Break-even: 1 utente Pro (€49) copre tutti i costi infrastrutturali per 6 mesi.
