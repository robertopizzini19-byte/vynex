import anthropic
import os
import json
import asyncio
import logging
import time

logger = logging.getLogger("vynex.ai")

ANTHROPIC_TIMEOUT_S = 60.0
MAX_RETRIES = 3
RETRY_BASE_DELAY_S = 1.5
MODEL = "claude-haiku-4-5-20251001"

client = anthropic.AsyncAnthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY"),
    timeout=ANTHROPIC_TIMEOUT_S,
)


class _CircuitBreaker:
    THRESHOLD = 5
    RECOVERY_S = 120

    def __init__(self):
        self._failures = 0
        self._open_until = 0.0

    def record_failure(self):
        self._failures += 1
        if self._failures >= self.THRESHOLD:
            self._open_until = time.time() + self.RECOVERY_S
            logger.warning("circuit breaker OPEN — Anthropic down, recovery in %ds", self.RECOVERY_S)

    def record_success(self):
        self._failures = 0
        self._open_until = 0.0

    @property
    def is_open(self) -> bool:
        if self._open_until and time.time() < self._open_until:
            return True
        if self._open_until and time.time() >= self._open_until:
            self._open_until = 0.0
            self._failures = max(0, self.THRESHOLD - 1)
        return False


_breaker = _CircuitBreaker()

SYSTEM_PROMPT = """Sei un assistente specializzato per agenti di commercio italiani.
Devi generare documenti professionali in italiano perfetto, formale ma umano.
Conosci la terminologia commerciale italiana, i termini contrattuali, le pratiche di vendita B2B.
Non usare mai frasi generiche o template ovvi. Ogni documento deve sembrare scritto da un professionista che conosce bene il cliente.

═══════════════════════════════════════════════════════════
REGOLE HARD — NON DEROGARE MAI
═══════════════════════════════════════════════════════════

1. ZERO INVENZIONI
   Usa ESCLUSIVAMENTE i fatti presenti nel testo dell'agente. Non inventare:
   - Date, scadenze aggiuntive, finestre temporali non menzionate (es. "applicabile fino al X" se non detto).
   - Percentuali, quantità, diametri, codici prodotto non presenti.
   - Termini di pagamento (es. "60 gg d.f.f.m.") se non specificati — in quel caso scrivi "Da definire in fase di conferma d'ordine".
   - Clausole contrattuali aggiuntive (responsabilità fino a destino, bobine, colli, garanzie) non concordate nella visita.
   - Parafrasi di vincoli che cambiano il senso giuridico (es. "per l'intera annualità" ≠ "12 mesi dalla firma").
   Se un dato manca e serve, scrivi "[Da confermare]" inline — MAI placeholder tipo "[Titolo]" "[Azienda]" "[da completare]".

2. MANDANTE + AGENTE
   Se il nome dell'agente è fornito, firma SEMPRE con quel nome esatto.
   Se il mandante è fornito, inseriscilo nell'header del REPORT e nella firma di EMAIL/OFFERTA.
   Mai usare placeholder vuoti tipo "[Azienda]", "[firma]", "[contatti]".

3. ORTOGRAFIA ITALIANA
   Rileggi ogni parola. Errori come "Fiduosi" (corretto: Fiduciosi), "prefetto" (perfetto), "pianificare" vs "pianifichiamo" sono intollerabili.
   Usa SEMPRE gli accenti corretti: è, à, ò, ì, ù, perché, però, così, più.

4. NEXT STEPS CONCRETI
   Ogni next step nel report e ogni riferimento nell'email deve avere: data esplicita (giorno/mese) + ora se nota + azione verbo operativo.
   Se l'input contiene un ordine di prova (quantità + referenze), l'OFFERTA deve citarlo alla fine con calcolo valore indicativo (qty × prezzo − sconto) e voce "Ordine di prova suggerito".

5. EMAIL: DATA ESPLICITA
   Inizia richiamando la visita con la data ESATTA ("la visita del 22 aprile"), mai "di oggi" o "odierna".

6. OFFERTA: FORMATO LEGALE ITALIANO
   Include sempre: intestazione "Spett.le" + destinatario, numero proposta (formato 2026/PP-DDMM/SIGLA), data emissione, scadenza validità, oggetto, condizioni (prezzo/sconto/lotto/consegna/pagamento), penali se previste, spazio firma per accettazione.
   Il testo DEVE essere utilizzabile senza ulteriori modifiche da parte dell'agente (zero placeholder da compilare, zero parentesi quadre vuote)."""


def extract_json(text: str) -> dict:
    """Estrae JSON da una risposta che potrebbe contenere testo extra."""
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("Nessun JSON trovato nella risposta")
    json_str = text[start:end]
    return json.loads(json_str)


_RETRYABLE = (
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)


async def _call_claude(prompt: str, max_tokens: int = 2048, tools=None, tool_choice=None):
    """Calls Claude with circuit breaker + retry + exponential backoff.

    When tools + tool_choice are passed, Claude is forced to output valid
    JSON matching the tool schema — no more "no JSON in response" errors.
    """
    if _breaker.is_open:
        raise RuntimeError("Servizio AI temporaneamente non disponibile. Riprova tra 2 minuti.")

    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            kwargs = {
                "model": MODEL,
                "max_tokens": max_tokens,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
            }
            if tools:
                kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
            result = await client.messages.create(**kwargs)
            _breaker.record_success()
            return result
        except _RETRYABLE as exc:
            last_exc = exc
            _breaker.record_failure()
            if attempt == MAX_RETRIES - 1:
                logger.error("Anthropic call failed after %d attempts: %s", MAX_RETRIES, exc)
                raise
            delay = RETRY_BASE_DELAY_S * (2 ** attempt)
            logger.warning("Anthropic attempt %d failed (%s), retrying in %.1fs", attempt + 1, exc, delay)
            await asyncio.sleep(delay)
    raise last_exc  # unreachable


_GENERA_TOOL = {
    "name": "crea_documenti_commerciali",
    "description": "Crea i 3 documenti commerciali italiani (report di visita, email di follow-up, offerta commerciale) a partire dalla descrizione informale dell'agente.",
    "input_schema": {
        "type": "object",
        "properties": {
            "cliente_nome": {
                "type": "string",
                "description": "Nome del cliente/contatto estratto dal testo. Stringa vuota se non presente.",
            },
            "azienda_cliente": {
                "type": "string",
                "description": "Nome dell'azienda cliente estratto dal testo. Stringa vuota se non presente.",
            },
            "report_visita": {
                "type": "string",
                "description": (
                    "Report di visita commerciale strutturato in italiano professionale. "
                    "Formato: REPORT DI VISITA COMMERCIALE, Data, Cliente, Azienda, "
                    "OBIETTIVO VISITA (1-2 righe), SVOLGIMENTO (3-5 righe), "
                    "RISULTATI E OPPORTUNITÀ (2-3 righe), NEXT STEPS (lista con date), "
                    "Note riservate. Usa \\n per andare a capo."
                ),
            },
            "email_followup": {
                "type": "string",
                "description": (
                    "Email di follow-up al cliente in italiano professionale caldo. "
                    "Inizia con 'Oggetto: ...'. Poi 'Gentile [nome],' seguito da 3-4 paragrafi "
                    "che fanno riferimento specifico a cose dette durante la visita. "
                    "Firma finale. Usa \\n per i paragrafi."
                ),
            },
            "offerta_commerciale": {
                "type": "string",
                "description": (
                    "Proposta commerciale formale. Formato: PROPOSTA COMMERCIALE, "
                    "Spett.le [Azienda], All'attenzione di [nome], introduzione 2 righe, "
                    "CONDIZIONI PROPOSTE (sconti, termini discussi), VALIDITÀ OFFERTA, "
                    "spazio firma. Usa \\n per andare a capo."
                ),
            },
        },
        "required": [
            "cliente_nome",
            "azienda_cliente",
            "report_visita",
            "email_followup",
            "offerta_commerciale",
        ],
    },
}


def _extract_tool_result(message, tool_name: str) -> dict:
    """Estrae l'input del tool_use block dalla risposta Claude."""
    for block in message.content:
        btype = getattr(block, "type", None)
        if btype == "tool_use" and getattr(block, "name", None) == tool_name:
            return dict(block.input or {})
    # Fallback: prova a parsare text come JSON (se Claude per qualche motivo
    # non ha usato il tool nonostante tool_choice forced).
    for block in message.content:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "") or ""
            start = text.find("{")
            end = text.rfind("}") + 1
            if start != -1 and end > 0:
                try:
                    return json.loads(text[start:end])
                except json.JSONDecodeError:
                    pass
    raise ValueError("Claude non ha restituito né tool_use né JSON parsabile")


async def genera_documenti(input_testo: str, nome_agente: str, azienda_mandante: str = "") -> dict:
    """
    Dato il resoconto informale dell'agente, genera 3 documenti professionali.

    Usa Claude tool_use forzato → output JSON garantito da schema validation.

    Returns:
        dict con chiavi: report_visita, email_followup, offerta_commerciale,
                         cliente_nome, azienda_cliente, tokens_used, generation_time_ms
    """
    # Calcolo date servito al modello così non deve fare aritmetica (dove
    # sbaglia ~30% delle volte): oggi, scadenza offerta +30gg calendario,
    # consegna +10gg lavorativi (skip sab/dom, ignoriamo festivi).
    from datetime import date as _date, timedelta as _td
    _today = _date.today()
    _scad_offerta = _today + _td(days=30)
    _d = _today
    _lav = 0
    while _lav < 10:
        _d = _d + _td(days=1)
        if _d.weekday() < 5:  # 0-4 = lun-ven
            _lav += 1
    _consegna_prova = _d
    _mesi = ["gennaio","febbraio","marzo","aprile","maggio","giugno","luglio","agosto","settembre","ottobre","novembre","dicembre"]
    def _it(d):
        return f"{d.day} {_mesi[d.month-1]} {d.year}"

    mandante_line = (
        f"\n\nMANDANTE (nome esatto da inserire nel REPORT header e firma EMAIL/OFFERTA): {azienda_mandante}"
        if azienda_mandante else
        "\n\nMANDANTE: NON fornito — OMETTI completamente la riga mandante (non scrivere [da completare])."
    )

    prompt = f"""L'agente di commercio "{nome_agente}" ha descritto così la sua visita/chiamata:

\"\"\"
{input_testo}
\"\"\"
{mandante_line}

CONTESTO TEMPORALE (usa ESATTAMENTE queste date, non calcolare a mente):
- Data di oggi (emissione documenti): {_it(_today)}
- Scadenza offerta (+30 gg calendario): {_it(_scad_offerta)}
- Consegna stimata ordine di prova (+10 gg lavorativi): {_it(_consegna_prova)}

Usa lo strumento `crea_documenti_commerciali` per generare i 3 documenti.

LINEE GUIDA OBBLIGATORIE:
1. Estrai cliente, azienda, prodotti, sconti, date, obiezioni dal testo.
2. Italiano professionale ma naturale, non robotico né template ovvio.
3. Date in formato italiano discorsivo (es. "25 aprile 2026").
4. VIETATO usare "[da completare]", "[Azienda]", "[Titolo]", "[Contatti]" o qualsiasi placeholder con parentesi quadre nei documenti. Se un dato non è noto:
   - Se è il MANDANTE e non è fornito sopra → OMETTI la riga.
   - Se sono TERMINI DI PAGAMENTO non specificati → scrivi "Da concordare in fase di conferma d'ordine".
   - Se è la firma → firma SEMPRE con il nome esatto dell'agente ({nome_agente}) + "{azienda_mandante or ''}" se fornito, SENZA placeholder vuoti.
5. FIRMA STANDARD (da usare nelle 3 firme di Report/Email/Offerta):
   Nome: {nome_agente}
   Ruolo: Agente Commerciale
   Mandante: {azienda_mandante or "(nessun mandante → ometti la riga)"}
6. Il REPORT è per il mandante (interno, operativo). Header DEVE includere "Mandante: {azienda_mandante or 'N/D'}" se fornito.
7. L'EMAIL è per il cliente: tono Lei di cortesia ("La ringrazio", "troverà", "La ricontatterò"). MAI "voi/troverete/avrete" in corpo email.
8. L'OFFERTA è formale con condizioni chiare, usa le date esatte dal CONTESTO TEMPORALE sopra — non inventarne altre. Se l'input menziona "ordine di prova" includi sezione finale "Ordine di prova suggerito" con calcolo valore."""

    t0 = time.perf_counter()
    # max_tokens=4096: 3 documenti ~600-900 tokens ognuno + overhead schema
    # → 2048 tronca l'offerta_commerciale (ultimo campo) causando tool_use parziale
    message = await _call_claude(
        prompt,
        max_tokens=4096,
        tools=[_GENERA_TOOL],
        tool_choice={"type": "tool", "name": "crea_documenti_commerciali"},
    )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    stop_reason = getattr(message, "stop_reason", None)
    if stop_reason == "max_tokens":
        logger.warning("Claude tool_use truncated by max_tokens — output incompleto")

    result = _extract_tool_result(message, "crea_documenti_commerciali")

    # Se Claude ha troncato, completa i campi mancanti con placeholder invece di
    # buttare via 2+ documenti già completi. Meglio output parziale che errore totale.
    fallback = {
        "report_visita": "[Report non completato — riprova la generazione per ottenere il testo integrale.]",
        "email_followup": "[Email non completata — riprova la generazione per ottenere il testo integrale.]",
        "offerta_commerciale": "[Offerta non completata — riprova la generazione per ottenere il testo integrale.]",
    }
    completed_fields = 0
    for field in ("report_visita", "email_followup", "offerta_commerciale"):
        if not result.get(field):
            logger.warning("Campo mancante nel tool_use: %s — fallback placeholder", field)
            result[field] = fallback[field]
        else:
            completed_fields += 1

    # Se TUTTI i 3 sono mancanti è un fail vero (tool_use rotto): raise.
    if completed_fields == 0:
        raise ValueError("Claude ha restituito tool_use vuoto — zero documenti generati")

    usage = getattr(message, "usage", None)
    tokens_used = (usage.input_tokens + usage.output_tokens) if usage else None

    return {
        "cliente_nome": result.get("cliente_nome", "") or "",
        "azienda_cliente": result.get("azienda_cliente", "") or "",
        "report_visita": result["report_visita"],
        "email_followup": result["email_followup"],
        "offerta_commerciale": result["offerta_commerciale"],
        "tokens_used": tokens_used,
        "generation_time_ms": elapsed_ms,
    }


async def rigenera_documento(
    tipo: str,
    input_originale: str,
    documento_attuale: str,
    istruzione: str,
    nome_agente: str
) -> str:
    tipo_labels = {
        "report_visita": "report di visita",
        "email_followup": "email di follow-up",
        "offerta_commerciale": "offerta commerciale"
    }

    prompt = f"""Contesto originale dell'agente {nome_agente}:
"{input_originale}"

{tipo_labels[tipo]} attuale:
{documento_attuale}

Istruzione di modifica: "{istruzione}"

Rigenera SOLO il {tipo_labels[tipo]} applicando la modifica richiesta. Mantieni il formato e la struttura originale. Restituisci solo il testo del documento, senza commenti o spiegazioni."""

    message = await _call_claude(prompt, max_tokens=1024)
    return message.content[0].text.strip()
