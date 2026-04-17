"""Google Sign-In via Google Identity Services (GIS).

Flow client-side, NO Client Secret:
- Il frontend carica il widget GIS (accounts.google.com/gsi/client)
- L'utente clicca → Google restituisce un JWT ID token al widget
- Il widget POST-a il JWT al backend (data-login_uri)
- Il backend verifica la firma del JWT con la public key di Google (lib google-auth)
- Nessun secret server-side: il Client ID basta, e il JWT è autenticato dalla chiave di Google

Setup Google Cloud Console:
1. https://console.cloud.google.com/ → progetto (vynex-auth consigliato)
2. APIs & Services → OAuth consent screen → External → compila nome + support email
3. Credentials → Create OAuth client ID → Web application
4. Authorized JavaScript origins:
   - https://vynex.it
   - http://localhost:8000  (dev)
5. Authorized redirect URIs:
   - https://vynex.it/auth/google/verify
   - http://localhost:8000/auth/google/verify  (dev)
6. Copia SOLO il Client ID → Railway env var: GOOGLE_CLIENT_ID
"""
import os
import logging
from typing import Optional

from google.oauth2 import id_token
from google.auth.transport import requests as g_requests

logger = logging.getLogger("vynex.oauth")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")

_VALID_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

if GOOGLE_CLIENT_ID:
    logger.info("Google Sign-In configured (GIS, client-side only)")
else:
    logger.info("Google Sign-In not configured (GOOGLE_CLIENT_ID missing)")


def is_enabled() -> bool:
    return bool(GOOGLE_CLIENT_ID)


def client_id() -> str:
    return GOOGLE_CLIENT_ID


def verify_credential(credential: str) -> Optional[dict]:
    """Verifica un ID token JWT restituito dal widget GIS.

    Ritorna il dict dei claims (email, name, email_verified, sub, picture)
    se valido, None altrimenti. google-auth valida firma, audience, exp, iss.
    """
    if not GOOGLE_CLIENT_ID or not credential:
        return None
    try:
        idinfo = id_token.verify_oauth2_token(
            credential,
            g_requests.Request(),
            GOOGLE_CLIENT_ID,
        )
    except ValueError as e:
        logger.warning("Google token verification failed: %s", e)
        return None

    if idinfo.get("iss") not in _VALID_ISSUERS:
        logger.warning("Google token invalid issuer: %s", idinfo.get("iss"))
        return None

    return idinfo
