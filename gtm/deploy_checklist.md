# AgentIA — Deploy Checklist

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

### JWT Secret
Genera una stringa sicura random:
```
python -c "import secrets; print(secrets.token_hex(32))"
```
Inserisci nel .env: SECRET_KEY=...

---

## STEP 2: GitHub

```bash
# Crea repo su github.com → new repository → "agentia" (private)
# Poi:
git remote add origin https://github.com/TUO_USERNAME/agentia.git
git branch -M main
git push -u origin main
```

---

## STEP 3: Railway Deploy

1. Vai su railway.app → New Project → Deploy from GitHub Repo
2. Seleziona il repo "agentia"
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
BASE_URL=https://TUO-DOMINIO.railway.app
DATABASE_URL=sqlite+aiosqlite:///./agentia.db
```

**Nota DATABASE_URL:** Railway offre PostgreSQL gratis. Per usarlo:
- Aggiungi PostgreSQL plugin in Railway
- Railway fornisce DATABASE_URL automaticamente
- Cambia in .env: DATABASE_URL=postgresql+asyncpg://...
- Aggiungi asyncpg ai requirements: `asyncpg==0.30.0`

---

## STEP 4: Stripe Webhook

1. Stripe Dashboard → Developers → Webhooks → Add endpoint
2. URL: https://TUO-DOMINIO.railway.app/webhook/stripe
3. Events da ascoltare:
   - checkout.session.completed
   - customer.subscription.deleted
   - customer.subscription.paused
   - customer.subscription.updated
4. Copia il Signing Secret (whsec_...) → Railway env: STRIPE_WEBHOOK_SECRET

---

## STEP 5: Dominio (opzionale ma consigliato)

**Opzione A — Railway custom domain:**
- Railway → Settings → Domains → Add Custom Domain
- Punta il DNS: CNAME → TUO-APP.railway.app

**Opzione B — Cloudflare + Railway:**
- Compra agentia.it su Namecheap (~€12/anno)
- Attiva Cloudflare (free tier)
- CNAME agentia.it → Railway URL
- SSL automatico via Cloudflare

---

## STEP 6: Test pre-lancio

```bash
# Test health
curl https://TUO-DOMINIO.railway.app/health

# Test registrazione
# Vai su https://TUO-DOMINIO.railway.app/registrati
# Crea account test

# Test generazione
# Inserisci testo visita → verifica 3 documenti

# Test Stripe (con card test 4242 4242 4242 4242)
# Vai su /checkout/pro
# Completa checkout → verifica piano aggiornato in dashboard
```

---

## STEP 7: Go-live checklist

- [ ] Health check risponde 200
- [ ] Registrazione funziona
- [ ] Login funziona
- [ ] AI genera documenti
- [ ] Stripe checkout funziona (test mode)
- [ ] Webhook aggiorna piano utente
- [ ] Privacy policy accessibile
- [ ] Email ciao@agentia.it attiva (forwarding)
- [ ] Outreach LinkedIn iniziato

---

## Costi operativi mensili (stima)

| Servizio | Costo |
|----------|-------|
| Railway Starter | $5/mese |
| Anthropic API (100 gen/mese) | ~$2/mese (Haiku) |
| Stripe | 1.4% + €0.25 per transazione |
| Dominio agentia.it | €1/mese |
| **Totale fisso** | **~$8/mese** |

Break-even: 1 utente Pro (€39) copre tutti i costi infrastrutturali per 5 mesi.
