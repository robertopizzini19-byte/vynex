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
Non usare mai frasi generiche o template ovvi. Ogni documento deve sembrare scritto da un professionista che conosce bene il cliente."""


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
    mandante_info = f"\nL'agente rappresenta il mandante: {azienda_mandante}" if azienda_mandante else ""

    prompt = f"""L'agente di commercio {nome_agente} ha descritto così la sua visita/chiamata:

"{input_testo}"
{mandante_info}

Usa lo strumento `crea_documenti_commerciali` per generare i 3 documenti.

Linee guida obbligatorie:
- Estrai cliente, azienda, prodotti, sconti, date, obiezioni dal testo
- Italiano professionale ma naturale, non robotico né template ovvio
- Date in formato italiano (es. 5 aprile 2026)
- Se un'informazione manca usa [da completare] come placeholder
- Il report è per il mandante (interno), l'email è per il cliente (calda ma pro),
  l'offerta è formale con condizioni chiare"""

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
