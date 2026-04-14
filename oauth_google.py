"""Google OAuth 2.0 signup/login via Authlib.

Setup richiesto:
1. Vai su https://console.cloud.google.com/
2. Crea un progetto (es. "VYNEX Auth")
3. APIs & Services → OAuth consent screen → External → compila nome app, support email, domini autorizzati
4. Credentials → Create OAuth client ID → Web application
5. Authorized redirect URIs:
   - https://agentia-production-fb78.up.railway.app/auth/google/callback
   - http://localhost:8000/auth/google/callback  (solo dev)
6. Copia Client ID e Client Secret → su Railway env vars:
   GOOGLE_CLIENT_ID=<client-id>
   GOOGLE_CLIENT_SECRET=<client-secret>
"""
import os
import logging
from authlib.integrations.starlette_client import OAuth

logger = logging.getLogger("vynex.oauth")

oauth = OAuth()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    logger.info("Google OAuth configured")
else:
    logger.info("Google OAuth not configured (GOOGLE_CLIENT_ID/SECRET missing)")


def is_enabled() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
