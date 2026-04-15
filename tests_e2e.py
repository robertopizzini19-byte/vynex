"""End-to-end integration tests for VYNEX auth + Google OAuth flow.

Tests run against a fresh local sqlite DB via httpx AsyncClient in-process.
Google OAuth is tested via monkey-patching oauth_google.verify_credential.

Run: python tests_e2e.py
"""
import asyncio
import os
import sys
from pathlib import Path

# Force fresh sqlite DB + dev BASE_URL before importing the app
TEST_DB = Path(__file__).parent / "vynex_test.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB.as_posix()}"
os.environ["BASE_URL"] = "http://localhost:8000"
os.environ["SECRET_KEY"] = "test-secret-key-not-for-prod-" + "x" * 32
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com")

import httpx
from httpx import ASGITransport

import main
import oauth_google
from database import init_db, AsyncSessionLocal
from models import User, EmailVerificationToken
from sqlalchemy import select


# ─── test harness ────────────────────────────────────────────────────────────

PASSED = []
FAILED = []


def passed(name: str, detail: str = ""):
    PASSED.append(name)
    print(f"  PASS  {name}" + (f"  [{detail}]" if detail else ""))


def failed(name: str, detail: str):
    FAILED.append((name, detail))
    print(f"  FAIL  {name}  -> {detail}")


def section(name: str):
    print(f"\n=== {name} ===")


async def fetch_user(email: str) -> User | None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()


async def fetch_verify_token(user_id: int) -> str | None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EmailVerificationToken)
            .where(EmailVerificationToken.user_id == user_id)
            .where(EmailVerificationToken.used_at.is_(None))
            .order_by(EmailVerificationToken.id.desc())
        )
        rec = result.scalars().first()
        return rec.token if rec else None


async def count_active_verify_tokens(user_id: int) -> int:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EmailVerificationToken)
            .where(EmailVerificationToken.user_id == user_id)
            .where(EmailVerificationToken.used_at.is_(None))
        )
        return len(result.scalars().all())


# ─── tests ───────────────────────────────────────────────────────────────────

async def run():
    await init_db()

    transport = ASGITransport(app=main.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://localhost:8000",
        follow_redirects=False,
    ) as c:

        # ── 1. SIGNUP ────────────────────────────────────────────────────────
        section("1. Signup")

        r = await c.post("/api/registrati", data={
            "email": "test1@vynex.it",
            "password": "weak",
            "full_name": "Mario Rossi",
            "accept_terms": "on",
        })
        if r.status_code == 302 and "error=" in r.headers.get("location", ""):
            passed("weak password rejected")
        else:
            failed("weak password rejected", f"status={r.status_code} loc={r.headers.get('location')}")

        r = await c.post("/api/registrati", data={
            "email": "test1@vynex.it",
            "password": "Strong123",
            "full_name": "M",
            "accept_terms": "on",
        })
        if r.status_code == 302 and "Nome" in r.headers.get("location", ""):
            passed("short name rejected")
        else:
            failed("short name rejected", f"loc={r.headers.get('location')}")

        r = await c.post("/api/registrati", data={
            "email": "not-an-email",
            "password": "Strong123",
            "full_name": "Mario Rossi",
            "accept_terms": "on",
        })
        if r.status_code == 302 and "non+valida" in r.headers.get("location", ""):
            passed("invalid email rejected")
        else:
            failed("invalid email rejected", f"loc={r.headers.get('location')}")

        r = await c.post("/api/registrati", data={
            "email": "test1@vynex.it",
            "password": "Strong123",
            "full_name": "Mario Rossi",
            "accept_terms": "on",
        })
        if r.status_code == 302 and r.headers.get("location") == "/dashboard":
            cookie = r.cookies.get("access_token")
            if cookie:
                passed("signup ok", f"cookie set len={len(cookie)}")
            else:
                failed("signup ok", "no access_token cookie (BUG: Secure cookie rejected on http?)")
        else:
            failed("signup ok", f"status={r.status_code} loc={r.headers.get('location')}")

        user = await fetch_user("test1@vynex.it")
        if user and not user.email_verified:
            passed("user created unverified")
        else:
            failed("user created unverified", f"user={user} verified={getattr(user, 'email_verified', None)}")

        tok = await fetch_verify_token(user.id)
        if tok:
            passed("verify token created", f"len={len(tok)}")
        else:
            failed("verify token created", "no token found")

        # duplicate email
        r = await c.post("/api/registrati", data={
            "email": "test1@vynex.it",
            "password": "Strong123",
            "full_name": "Mario Rossi",
            "accept_terms": "on",
        })
        loc = r.headers.get("location", "")
        if r.status_code == 302 and ("già" in loc or "gi%C3%A0" in loc):
            passed("duplicate email rejected")
        else:
            failed("duplicate email rejected", f"loc={loc}")

        # ── 2. EMAIL VERIFICATION ────────────────────────────────────────────
        section("2. Email verification")

        r = await c.get(f"/verifica-email?token={tok}")
        if r.status_code == 200 and ("verificata" in r.text.lower() or "verificato" in r.text.lower()):
            passed("verify token consumed")
        else:
            failed("verify token consumed", f"status={r.status_code} body={r.text[:200]}")

        u = await fetch_user("test1@vynex.it")
        if u and u.email_verified:
            passed("user marked verified")
        else:
            failed("user marked verified", f"verified={getattr(u, 'email_verified', None)}")

        # replay attack: reuse same token
        r = await c.get(f"/verifica-email?token={tok}")
        if r.status_code == 200 and ("non valido" in r.text.lower() or "scaduto" in r.text.lower() or "error" in r.text.lower()):
            passed("verify token replay blocked")
        else:
            failed("verify token replay blocked", f"body={r.text[:200]}")

        # ── 3. LOGIN / LOGOUT ────────────────────────────────────────────────
        section("3. Login / logout")

        c2 = httpx.AsyncClient(transport=transport, base_url="http://localhost:8000", follow_redirects=False)

        r = await c2.post("/api/login", data={"email": "test1@vynex.it", "password": "Strong123"})
        if r.status_code == 302 and r.headers.get("location") == "/dashboard" and r.cookies.get("access_token"):
            passed("login ok")
        else:
            failed("login ok", f"status={r.status_code} loc={r.headers.get('location')} cookie={r.cookies.get('access_token') is not None}")

        r = await c2.get("/dashboard")
        if r.status_code == 200 and "dashboard" in r.text.lower():
            passed("authenticated access")
        else:
            failed("authenticated access", f"status={r.status_code}")

        # wrong password
        r = await c2.post("/api/login", data={"email": "test1@vynex.it", "password": "WrongPass1"})
        if r.status_code == 302 and "non+corretti" in r.headers.get("location", ""):
            passed("wrong password rejected")
        else:
            failed("wrong password rejected", f"loc={r.headers.get('location')}")

        # logout
        r = await c2.get("/logout")
        if r.status_code == 302 and r.headers.get("location") == "/":
            passed("logout redirects")
        else:
            failed("logout redirects", f"loc={r.headers.get('location')}")

        # after logout, cookie cleared; dashboard should redirect/401
        c3 = httpx.AsyncClient(transport=transport, base_url="http://localhost:8000", follow_redirects=False)
        r = await c3.get("/dashboard")
        if r.status_code == 401:
            passed("dashboard rejects unauth")
        else:
            failed("dashboard rejects unauth", f"status={r.status_code}")

        await c2.aclose()
        await c3.aclose()

        # ── 4. LOCKOUT (5 failed attempts) ──────────────────────────────────
        section("4. Brute force lockout")

        c4 = httpx.AsyncClient(transport=transport, base_url="http://localhost:8000", follow_redirects=False)
        for i in range(5):
            await c4.post("/api/login", data={"email": "test1@vynex.it", "password": f"Wrong{i}Pass"})

        # 6th attempt even with correct password should say locked
        r = await c4.post("/api/login", data={"email": "test1@vynex.it", "password": "Strong123"})
        if r.status_code == 302 and "bloccato" in r.headers.get("location", ""):
            passed("lockout after 5 failed")
        else:
            failed("lockout after 5 failed", f"loc={r.headers.get('location')}")

        # unlock manually (simulate 15min passed)
        async with AsyncSessionLocal() as db:
            u = (await db.execute(select(User).where(User.email == "test1@vynex.it"))).scalar_one()
            u.locked_until = None
            u.failed_login_attempts = 0
            await db.commit()

        r = await c4.post("/api/login", data={"email": "test1@vynex.it", "password": "Strong123"})
        if r.status_code == 302 and r.headers.get("location") == "/dashboard":
            passed("login works after unlock")
        else:
            failed("login works after unlock", f"loc={r.headers.get('location')}")
        await c4.aclose()

        # ── 5. PASSWORD RESET ───────────────────────────────────────────────
        section("5. Password reset")

        r = await c.post("/api/recupera-password", data={"email": "test1@vynex.it"})
        if r.status_code == 302 and "message=" in r.headers.get("location", ""):
            passed("forgot-password ack")
        else:
            failed("forgot-password ack", f"loc={r.headers.get('location')}")

        # forgot for non-existing email — should still ack (enumeration protection)
        r = await c.post("/api/recupera-password", data={"email": "nope@vynex.it"})
        if r.status_code == 302 and "message=" in r.headers.get("location", ""):
            passed("forgot-password enumeration safe")
        else:
            failed("forgot-password enumeration safe", f"loc={r.headers.get('location')}")

        # generate reset token directly for testing — must pass the user's
        # current token_version so the single-use check inside
        # /api/reset-password accepts it (replay protection bumps tv).
        from auth import create_password_reset_token
        u_now = await fetch_user("test1@vynex.it")
        reset_tok = create_password_reset_token(
            "test1@vynex.it", token_version=u_now.token_version or 0
        )

        r = await c.post("/api/reset-password", data={"token": reset_tok, "password": "NewStrong456"})
        if r.status_code == 302 and "aggiornata" in r.headers.get("location", ""):
            passed("reset password ok")
        else:
            failed("reset password ok", f"loc={r.headers.get('location')}")

        # old password shouldn't work
        c5 = httpx.AsyncClient(transport=transport, base_url="http://localhost:8000", follow_redirects=False)
        r = await c5.post("/api/login", data={"email": "test1@vynex.it", "password": "Strong123"})
        if r.status_code == 302 and "non+corretti" in r.headers.get("location", ""):
            passed("old password invalid after reset")
        else:
            failed("old password invalid after reset", f"loc={r.headers.get('location')}")

        r = await c5.post("/api/login", data={"email": "test1@vynex.it", "password": "NewStrong456"})
        if r.status_code == 302 and r.headers.get("location") == "/dashboard":
            passed("new password works")
        else:
            failed("new password works", f"loc={r.headers.get('location')}")

        # ── 6. TOKEN_VERSION / LOGOUT-ALL ───────────────────────────────────
        section("6. logout-all rotates token_version")

        old_cookie = c5.cookies.get("access_token")
        r = await c5.post("/api/logout-all")
        if r.status_code == 200:
            passed("logout-all accepted")
        else:
            failed("logout-all accepted", f"status={r.status_code}")

        # reusing the old token (in a fresh client) should fail
        c6 = httpx.AsyncClient(transport=transport, base_url="http://localhost:8000", follow_redirects=False)
        c6.cookies.set("access_token", old_cookie, domain="localhost")
        r = await c6.get("/dashboard")
        if r.status_code == 401:
            passed("old JWT invalid after logout-all")
        else:
            failed("old JWT invalid after logout-all", f"status={r.status_code}")
        await c5.aclose()
        await c6.aclose()

        # ── 7. ACCOUNT UPDATE ───────────────────────────────────────────────
        section("7. Account update")

        c7 = httpx.AsyncClient(transport=transport, base_url="http://localhost:8000", follow_redirects=False)
        await c7.post("/api/login", data={"email": "test1@vynex.it", "password": "NewStrong456"})

        r = await c7.post("/api/account/update", data={
            "full_name": "Mario Rossi Updated",
            "email": "test1@vynex.it",
            "company_name": "Acme SRL",
        })
        if r.status_code == 302 and "message=" in r.headers.get("location", ""):
            passed("account update name+company")
        else:
            failed("account update name+company", f"loc={r.headers.get('location')}")

        u = await fetch_user("test1@vynex.it")
        if u and u.full_name == "Mario Rossi Updated" and u.company_name == "Acme SRL":
            passed("account update persisted")
        else:
            failed("account update persisted", f"name={getattr(u, 'full_name', None)} co={getattr(u, 'company_name', None)}")

        # change email → verify token re-issued, is_verified reset, token_version bumped
        r = await c7.post("/api/account/update", data={
            "full_name": "Mario Rossi Updated",
            "email": "test1-new@vynex.it",
            "company_name": "Acme SRL",
        })
        if r.status_code == 302 and r.cookies.get("access_token"):
            passed("email change reissues cookie")
        else:
            failed("email change reissues cookie", f"loc={r.headers.get('location')}")

        u = await fetch_user("test1-new@vynex.it")
        if u and not u.email_verified:
            passed("email change unverifies")
        else:
            failed("email change unverifies", f"user={u}")

        # old tokens invalidated (check: count of unused tokens should be 1, the newest)
        n = await count_active_verify_tokens(u.id)
        if n == 1:
            passed("email change invalidates old tokens", f"active={n}")
        else:
            failed("email change invalidates old tokens", f"active={n} (expected 1)")

        # ── 8. PASSWORD CHANGE ──────────────────────────────────────────────
        section("8. Password change")

        # wrong current password
        r = await c7.post("/api/account/password", data={
            "current_password": "wrong",
            "new_password": "Strongest789",
        })
        if r.status_code == 302 and "non+corretta" in r.headers.get("location", ""):
            passed("wrong current password rejected")
        else:
            failed("wrong current password rejected", f"loc={r.headers.get('location')}")

        # weak new password
        r = await c7.post("/api/account/password", data={
            "current_password": "NewStrong456",
            "new_password": "abc",
        })
        if r.status_code == 302 and "error=" in r.headers.get("location", ""):
            passed("weak new password rejected")
        else:
            failed("weak new password rejected", f"loc={r.headers.get('location')}")

        # valid change
        r = await c7.post("/api/account/password", data={
            "current_password": "NewStrong456",
            "new_password": "Strongest789",
        })
        if r.status_code == 302 and "/login" in r.headers.get("location", ""):
            passed("password change ok")
        else:
            failed("password change ok", f"loc={r.headers.get('location')}")

        await c7.aclose()

        # ── 9. GDPR EXPORT ──────────────────────────────────────────────────
        section("9. GDPR export")

        c8 = httpx.AsyncClient(transport=transport, base_url="http://localhost:8000", follow_redirects=False)
        await c8.post("/api/login", data={"email": "test1-new@vynex.it", "password": "Strongest789"})
        r = await c8.get("/api/export-data")
        if r.status_code == 200 and r.json().get("user", {}).get("email") == "test1-new@vynex.it":
            passed("GDPR export returns user data")
        else:
            failed("GDPR export returns user data", f"status={r.status_code} body={r.text[:200]}")

        # ── 10. SOFT DELETE ─────────────────────────────────────────────────
        section("10. Soft delete")

        r = await c8.post("/api/delete-account", data={
            "password": "Strongest789",
            "confirm": "ELIMINA",
        })
        if r.status_code == 302 and "deleted=1" in r.headers.get("location", ""):
            passed("soft delete ok")
        else:
            failed("soft delete ok", f"loc={r.headers.get('location')}")

        u = await fetch_user("test1-new@vynex.it")
        if not u:
            passed("email anonymized on delete")
        else:
            failed("email anonymized on delete", f"user still findable")

        # cannot login after delete
        r = await c8.post("/api/login", data={"email": "test1-new@vynex.it", "password": "Strongest789"})
        if r.status_code == 302 and ("/login" in r.headers.get("location", "")):
            passed("deleted user cannot login")
        else:
            failed("deleted user cannot login", f"loc={r.headers.get('location')}")

        await c8.aclose()

        # ── 11. GOOGLE OAUTH (mocked) ───────────────────────────────────────
        section("11. Google OAuth verify (mocked)")

        # mock verify_credential
        def fake_verify(credential):
            if credential == "valid-token":
                return {
                    "email": "googleuser@gmail.com",
                    "name": "Google User",
                    "email_verified": True,
                    "iss": "accounts.google.com",
                    "sub": "1234567890",
                }
            if credential == "new-account":
                return {
                    "email": "newgoogleuser@gmail.com",
                    "name": "New Google User",
                    "email_verified": True,
                    "iss": "accounts.google.com",
                    "sub": "9876543210",
                }
            return None

        oauth_google.verify_credential = fake_verify
        main.verify_google_credential = fake_verify

        c9 = httpx.AsyncClient(transport=transport, base_url="http://localhost:8000", follow_redirects=False)

        # missing credential
        r = await c9.post("/auth/google/verify", data={})
        if r.status_code == 303 and "mancante" in r.headers.get("location", ""):
            passed("missing credential redirects")
        else:
            failed("missing credential redirects", f"status={r.status_code} loc={r.headers.get('location')}")

        # CSRF mismatch (cookie not set)
        r = await c9.post("/auth/google/verify", data={
            "credential": "valid-token",
            "g_csrf_token": "abc",
        })
        if r.status_code == 303 and "CSRF" in r.headers.get("location", ""):
            passed("CSRF mismatch redirects")
        else:
            failed("CSRF mismatch redirects", f"loc={r.headers.get('location')}")

        # valid CSRF + invalid credential. Pass cookies explicitly on each
        # request instead of relying on the client jar — httpx ASGI transport
        # cookie-jar domain matching for "localhost" is unreliable across
        # httpx versions, so send the Cookie header directly.
        csrf_cookies = {"g_csrf_token": "csrf-xyz"}
        r = await c9.post(
            "/auth/google/verify",
            data={"credential": "invalid", "g_csrf_token": "csrf-xyz"},
            cookies=csrf_cookies,
        )
        if r.status_code == 303 and "Token+Google+non+valido" in r.headers.get("location", ""):
            passed("invalid Google token rejected")
        else:
            failed("invalid Google token rejected", f"loc={r.headers.get('location')}")

        # valid flow — new user
        r = await c9.post(
            "/auth/google/verify",
            data={"credential": "new-account", "g_csrf_token": "csrf-xyz"},
            cookies=csrf_cookies,
        )
        if r.status_code == 302 and r.headers.get("location") == "/dashboard" and r.cookies.get("access_token"):
            passed("Google signup creates user")
        else:
            failed("Google signup creates user", f"status={r.status_code} loc={r.headers.get('location')} cookie={r.cookies.get('access_token') is not None}")

        u = await fetch_user("newgoogleuser@gmail.com")
        if u and u.email_verified and u.full_name == "New Google User":
            passed("Google new user email auto-verified")
        else:
            failed("Google new user email auto-verified", f"user={u}")

        # second call with same account — should login existing
        c10 = httpx.AsyncClient(transport=transport, base_url="http://localhost:8000", follow_redirects=False)
        r = await c10.post(
            "/auth/google/verify",
            data={"credential": "new-account", "g_csrf_token": "csrf-xyz"},
            cookies=csrf_cookies,
        )
        if r.status_code == 302 and r.headers.get("location") == "/dashboard":
            passed("Google re-login existing user")
        else:
            failed("Google re-login existing user", f"loc={r.headers.get('location')}")

        await c9.aclose()
        await c10.aclose()

        # ── 12. CSP HEADER VALIDATION ───────────────────────────────────────
        section("12. CSP header")

        r = await c.get("/login")
        csp = r.headers.get("content-security-policy", "")
        bugs = []
        if "accounts.google.com/gsi/client" in csp:
            bugs.append("script-src has invalid path suffix")
        if "accounts.google.com/gsi/" in csp and "accounts.google.com/gsi/client" not in csp:
            bugs.append("frame/connect-src has invalid path suffix")
        if "accounts.google.com" not in csp:
            bugs.append("accounts.google.com missing from CSP")
        if not bugs:
            passed("CSP allows Google origin cleanly")
        else:
            failed("CSP clean", " | ".join(bugs))

        # widget renders with Client ID
        if 'g_id_onload' in r.text and 'data-client_id="test-client-id' in r.text:
            passed("GIS widget rendered with Client ID")
        else:
            failed("GIS widget rendered with Client ID", "widget not found in login.html")

    # ── summary ─────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print(f"PASSED: {len(PASSED)}")
    print(f"FAILED: {len(FAILED)}")
    if FAILED:
        print()
        for name, detail in FAILED:
            print(f"  FAIL  {name}: {detail}")
    print("=" * 60)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    exit_code = asyncio.run(run())
    sys.exit(exit_code)
