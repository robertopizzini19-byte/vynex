# Changelog

Tutte le modifiche notevoli a VYNEX. Formato: [Keep a Changelog](https://keepachangelog.com/it/1.1.0/).

## [Unreleased]

## [1.1.0] — 2026-04-18

### Added
- **Motore di acquisizione autonoma**: landing `/demo` con generazione 3 documenti senza account
- **Drip email engine** APScheduler (5 min cycle): sequenza 5-step lead + 3-step user onboarding
- **Cold outreach** admin-gated via `POST /api/admin/leads/import` (CSV bearer token)
- **Referral loop**: codice per utente, `/r/{code}`, Stripe `trial_end +30d` ogni 2 conversioni
- **Tracking HMAC** open/click pixel, unsubscribe GDPR
- **Maintenance jobs** (6h cycle): cleanup token scaduti, purge soft-deleted docs, cleanup dead email jobs
- **Stripe reconciliation** (12h cycle): sync stato paganti locale ↔ Stripe
- **Retry logic email** con exponential backoff (15min → 1h → 4h)
- **AuditLog** per eventi sensibili
- **CI** via GitHub Actions: py_compile + import check + E2E tests
- **Dependabot** weekly updates

### Changed
- URL canonical e JSON-LD ora usano `canonical_base` Jinja global (BASE_URL env)
- Repository rinominato `visitai` → `vynex`
- `.env.example`, documentazione, GTM: rebrand VYNEX

### Fixed
- Stats funnel distingue `sent_24h` da `failed_24h`

## [1.0.0] — 2026-04-13

### Added
- Pipeline LIVE in produzione: signup → checkout → webhook firmato → DB → dashboard
- Stripe Checkout + Customer Portal
- Claude Haiku 4.5 per generazione documenti
- Supabase Postgres via asyncpg (session pooler 5432)
- JWT auth, Google OAuth, email verification
- GDPR completo: privacy/termini/cookie/export/delete
- Rate limiting slowapi
- Security headers + CSRF origin check
- Cookie banner GDPR-compliant
