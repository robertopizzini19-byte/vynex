# VYNEX — Backup Strategy

## Database (Supabase PostgreSQL)

Supabase esegue backup automatici del piano Free/Pro:

- **Daily automated backup**: retention 7 giorni (piano Free)
- **Point-in-time recovery**: non incluso nel piano Free, disponibile da Pro
- **Region**: Frankfurt (EU)

### Backup manuale mensile

Il primo di ogni mese eseguire dump completo e archiviare offsite.

```bash
# Richiede: pg_dump, credenziali Supabase session pooler
export DATABASE_URL="postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres"
pg_dump "$DATABASE_URL" \
  --no-owner --no-privileges --format=custom \
  --file="vynex-backup-$(date +%Y%m%d).dump"
```

Cifrare il dump prima di archiviare:

```bash
gpg --symmetric --cipher-algo AES256 vynex-backup-*.dump
```

Archiviare su storage offsite cifrato (OneDrive cifrato, bucket S3 EU con SSE, ecc.).
Retention minima: 12 mesi. Backup più vecchi possono essere ruotati.

### Ripristino

```bash
# Crea DB vuoto target
createdb vynex_restore
pg_restore --dbname=vynex_restore --no-owner --no-privileges vynex-backup-YYYYMMDD.dump
```

Verificare: conteggio `users`, `documents`, `stripe_events` corrisponde al dump.

## Code & Config

- **Code**: repository git (origin: GitHub). Clone locale + Railway deploy sono 2 copie.
- **Secrets**: Railway environment variables + `.env` locale cifrato.
- **Logo, template, static**: versionati nel repo.

## Disaster Recovery

Se Supabase Frankfurt è irraggiungibile (downtime prolungato):

1. Creare nuovo progetto Supabase in region alternativa.
2. Ripristinare ultimo backup `pg_restore`.
3. Aggiornare `DATABASE_URL` su Railway con nuova connection string.
4. Railway riavvia app automaticamente.
5. Verificare `/health/deep` → `database: ok`.

RTO target: 2 ore · RPO target: 24 ore (daily backup).

## Export utente (GDPR art. 15)

Gli utenti possono scaricare i propri dati da `/account` → "Esporta i tuoi dati".
Questo NON è un backup per VYNEX ma soddisfa il diritto di portabilità GDPR.
