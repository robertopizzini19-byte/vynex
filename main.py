from fastapi import FastAPI, Request, Depends, Form, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from datetime import datetime
from contextlib import asynccontextmanager
import os

from dotenv import load_dotenv
load_dotenv()

from database import get_db, init_db
from models import User, Document
from auth import (
    hash_password, authenticate_user, create_access_token,
    get_current_user, require_user, get_user_by_email
)
from ai_engine import genera_documenti, rigenera_documento
from stripe_handler import create_checkout_session, create_portal_session, handle_webhook


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="AgentIA", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def redirect_with_cookie(url: str, token: str) -> RedirectResponse:
    response = RedirectResponse(url=url, status_code=302)
    response.set_cookie(
        "access_token", token,
        httponly=True, secure=True, samesite="lax",
        max_age=60 * 60 * 24 * 30  # 30 giorni
    )
    return response


async def get_monthly_usage(db: AsyncSession, user_id: int) -> int:
    start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(func.count(Document.id))
        .where(Document.user_id == user_id)
        .where(Document.created_at >= start)
    )
    return result.scalar() or 0


# ─── PAGINE PUBBLICHE ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/prezzi", response_class=HTMLResponse)
async def prezzi(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    return templates.TemplateResponse("prezzi.html", {"request": request, "user": user})


@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return HTMLResponse("""<!DOCTYPE html><html lang="it"><head><meta charset="UTF-8">
<title>Privacy Policy — AgentIA</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/style.css">
</head><body style="max-width:720px;margin:60px auto;padding:0 24px;font-family:Inter,sans-serif;line-height:1.7;color:#1a1a2e">
<a href="/" style="color:#4f46e5;text-decoration:none">← AgentIA</a>
<h1 style="margin-top:32px">Privacy Policy</h1>
<p>Ultimo aggiornamento: 21 marzo 2026</p>
<h2>Titolare del trattamento</h2>
<p>AgentIA — contatto: <a href="mailto:ciao@agentia.it">ciao@agentia.it</a></p>
<h2>Dati raccolti</h2>
<p>Raccogliamo: nome, email, descrizioni delle visite commerciali inserite dall'utente. I dati sono utilizzati esclusivamente per fornire il servizio.</p>
<h2>Finalità del trattamento</h2>
<p>I dati sono trattati per: erogazione del servizio, fatturazione, comunicazioni relative all'account.</p>
<h2>Conservazione</h2>
<p>I dati sono conservati per la durata del contratto e per gli obblighi di legge successivi alla cancellazione dell'account.</p>
<h2>Diritti dell'utente</h2>
<p>Hai diritto di accesso, rettifica, cancellazione e portabilità dei dati. Scrivici a <a href="mailto:ciao@agentia.it">ciao@agentia.it</a>.</p>
<h2>Terze parti</h2>
<p>Utilizziamo: Anthropic (elaborazione AI), Stripe (pagamenti), Railway (hosting). Ognuno ha proprie politiche sulla privacy conformi al GDPR.</p>
</body></html>""")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    error = request.query_params.get("error", "")
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.get("/registrati", response_class=HTMLResponse)
async def register_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    error = request.query_params.get("error", "")
    return templates.TemplateResponse("register.html", {"request": request, "error": error})


# ─── AUTH ─────────────────────────────────────────────────────────────────────

@app.post("/api/login")
async def api_login(
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    user = await authenticate_user(db, email, password)
    if not user:
        return RedirectResponse("/login?error=Email+o+password+non+corretti", status_code=302)
    token = create_access_token({"sub": user.email})
    return redirect_with_cookie("/dashboard", token)


@app.post("/api/registrati")
async def api_register(
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(...),
    company_name: str = Form(""),
    db: AsyncSession = Depends(get_db)
):
    existing = await get_user_by_email(db, email)
    if existing:
        return RedirectResponse("/registrati?error=Email+già+registrata", status_code=302)

    if len(password) < 8:
        return RedirectResponse("/registrati?error=Password+di+almeno+8+caratteri", status_code=302)

    user = User(
        email=email.lower().strip(),
        hashed_password=hash_password(password),
        full_name=full_name.strip(),
        company_name=company_name.strip() or None,
        plan="free"
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token({"sub": user.email})
    return redirect_with_cookie("/dashboard", token)


@app.get("/logout")
async def logout():
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie("access_token")
    return response


# ─── APP PROTETTA ─────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user)
):
    # Ultimi 10 documenti
    result = await db.execute(
        select(Document)
        .where(Document.user_id == user.id)
        .order_by(Document.created_at.desc())
        .limit(10)
    )
    documenti = result.scalars().all()
    usage = await get_monthly_usage(db, user.id)
    upgrade_msg = request.query_params.get("upgrade", "")

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "documenti": documenti,
        "usage": usage,
        "limit": user.monthly_limit,
        "upgrade_msg": upgrade_msg
    })


@app.get("/genera", response_class=HTMLResponse)
async def genera_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user)
):
    usage = await get_monthly_usage(db, user.id)
    can_generate = usage < user.monthly_limit
    return templates.TemplateResponse("genera.html", {
        "request": request,
        "user": user,
        "usage": usage,
        "limit": user.monthly_limit,
        "can_generate": can_generate
    })


@app.get("/documento/{doc_id}", response_class=HTMLResponse)
async def documento_detail(
    doc_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user)
):
    result = await db.execute(
        select(Document).where(Document.id == doc_id, Document.user_id == user.id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(404, "Documento non trovato")
    return templates.TemplateResponse("documento.html", {
        "request": request,
        "user": user,
        "doc": doc
    })


# ─── API JSON ─────────────────────────────────────────────────────────────────

@app.post("/api/genera")
async def api_genera(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user)
):
    usage = await get_monthly_usage(db, user.id)
    if usage >= user.monthly_limit:
        return JSONResponse(
            {"error": "Limite mensile raggiunto. Passa al piano Pro per documenti illimitati."},
            status_code=429
        )

    body = await request.json()
    input_testo = body.get("input_testo", "").strip()
    azienda_mandante = body.get("azienda_mandante", "").strip()

    if not input_testo or len(input_testo) < 20:
        return JSONResponse({"error": "Descrizione troppo corta. Aggiungi più dettagli."}, status_code=400)

    if len(input_testo) > 2000:
        return JSONResponse({"error": "Descrizione troppo lunga (max 2000 caratteri)."}, status_code=400)

    try:
        result = await genera_documenti(
            input_testo=input_testo,
            nome_agente=user.full_name,
            azienda_mandante=azienda_mandante or (user.company_name or "")
        )
    except Exception as e:
        return JSONResponse({"error": f"Errore AI: {str(e)}"}, status_code=500)

    doc = Document(
        user_id=user.id,
        input_text=input_testo,
        report_visita=result["report_visita"],
        email_followup=result["email_followup"],
        offerta_commerciale=result["offerta_commerciale"],
        cliente_nome=result.get("cliente_nome"),
        azienda_cliente=result.get("azienda_cliente"),
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    return JSONResponse({
        "doc_id": doc.id,
        "report_visita": doc.report_visita,
        "email_followup": doc.email_followup,
        "offerta_commerciale": doc.offerta_commerciale,
        "cliente_nome": doc.cliente_nome,
        "azienda_cliente": doc.azienda_cliente,
    })


@app.post("/api/rigenera")
async def api_rigenera(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user)
):
    body = await request.json()
    doc_id = body.get("doc_id")
    tipo = body.get("tipo")
    istruzione = body.get("istruzione", "").strip()

    if tipo not in ("report_visita", "email_followup", "offerta_commerciale"):
        return JSONResponse({"error": "Tipo non valido"}, status_code=400)

    if not istruzione:
        return JSONResponse({"error": "Specifica cosa vuoi modificare."}, status_code=400)

    result = await db.execute(
        select(Document).where(Document.id == doc_id, Document.user_id == user.id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        return JSONResponse({"error": "Documento non trovato"}, status_code=404)

    documento_attuale = getattr(doc, tipo)
    try:
        nuovo_testo = await rigenera_documento(
            tipo=tipo,
            input_originale=doc.input_text,
            documento_attuale=documento_attuale,
            istruzione=istruzione,
            nome_agente=user.full_name
        )
    except Exception as e:
        return JSONResponse({"error": f"Errore AI: {str(e)}"}, status_code=500)

    setattr(doc, tipo, nuovo_testo)
    await db.commit()

    return JSONResponse({"testo": nuovo_testo})


# ─── STRIPE ───────────────────────────────────────────────────────────────────

@app.get("/checkout/{plan}")
async def checkout(
    plan: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user)
):
    if plan not in ("pro", "team"):
        raise HTTPException(400, "Piano non valido")
    if not os.getenv("STRIPE_SECRET_KEY"):
        raise HTTPException(503, "Pagamenti non ancora configurati")

    checkout_url = await create_checkout_session(user, plan)
    return RedirectResponse(checkout_url, status_code=302)


@app.get("/portale-fatturazione")
async def portale_fatturazione(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user)
):
    if not user.stripe_customer_id:
        raise HTTPException(400, "Nessun abbonamento attivo")
    portal_url = await create_portal_session(user)
    return RedirectResponse(portal_url, status_code=302)


@app.post("/webhook/stripe")
async def webhook_stripe(request: Request, db: AsyncSession = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        await handle_webhook(payload, sig_header, db)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "ok"}


# ─── HEALTH ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "agentia"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
