# Security Policy

## Reporting a Vulnerability

Se trovi una vulnerabilità in VYNEX, **non aprire una issue pubblica**.

Invia un'email a **ciao@vynex.it** con oggetto `[SECURITY]` includendo:

- Descrizione della vulnerabilità
- Passi per riprodurla
- Impatto stimato
- Il tuo nome/handle (per riconoscerti nel changelog se vuoi)

Riceverai conferma entro **48 ore lavorative**. Le patch vengono rilasciate entro 7 giorni per vulnerabilità critiche, 30 giorni per le altre.

## Supported Versions

Solo `main` branch riceve patch di sicurezza. VYNEX è un SaaS single-tenant hosted: tutti gli utenti sono sempre sulla versione corrente.

## Security Measures Already in Place

- HSTS + CSP strict + X-Frame-Options DENY + X-Content-Type-Options nosniff
- Rate limiting su auth, registrazione, generazione
- CSRF protection via Origin/Referer check + SameSite=Lax cookies
- Password hash: bcrypt (12 rounds)
- JWT cookie-based httpOnly + Secure in produzione
- Webhook Stripe con HMAC signature verification + idempotency
- Account lockout dopo 5 login falliti
- Admin endpoints bearer token (timing-safe compare)
- Tracking HMAC-signed (no enumeration)
- SQL injection: parameterized queries via SQLAlchemy (zero raw SQL utente-fed)
- GDPR: export (art. 15), delete (art. 17), portability (art. 20)
