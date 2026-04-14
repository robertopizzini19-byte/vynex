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


async def _call_claude(prompt: str, max_tokens: int = 2048):
    """Calls Claude with retry+exponential backoff. Returns the message object."""
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return await client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt == MAX_RETRIES - 1:
                logger.error("Anthropic call failed after %d attempts: %s", MAX_RETRIES, exc)
                raise
            delay = RETRY_BASE_DELAY_S * (2 ** attempt)
            logger.warning("Anthropic attempt %d failed (%s), retrying in %.1fs", attempt + 1, exc, delay)
            await asyncio.sleep(delay)
    raise last_exc  # unreachable


async def genera_documenti(input_testo: str, nome_agente: str, azienda_mandante: str = "") -> dict:
    """
    Dato il resoconto informale dell'agente, genera 3 documenti professionali.

    Returns:
        dict con chiavi: report_visita, email_followup, offerta_commerciale,
                         cliente_nome, azienda_cliente, tokens_used, generation_time_ms
    """
    mandante_info = f"L'agente rappresenta: {azienda_mandante}" if azienda_mandante else ""

    prompt = f"""L'agente di commercio {nome_agente} ha descritto la sua visita/chiamata in modo informale:

"{input_testo}"

{mandante_info}

Genera i 3 documenti professionali richiesti e restituisci SOLO un JSON con questa struttura esatta:

{{
  "cliente_nome": "nome del cliente/contatto estratto dal testo",
  "azienda_cliente": "nome dell'azienda cliente estratto dal testo",
  "report_visita": "REPORT DI VISITA COMMERCIALE\\n\\nData: [data di oggi o menzionata]\\nCliente: [nome]\\nAzienda: [azienda]\\n\\nOBIETTIVO VISITA:\\n[1-2 righe]\\n\\nSVOLGIMENTO:\\n[3-5 righe descrittive, professionali]\\n\\nRISULTATI E OPPORTUNITÀ:\\n[2-3 righe]\\n\\nNEXT STEPS:\\n[lista puntata di azioni concrete con date]\\n\\nNote riservate: [eventuali note strategiche per il mandante]",
  "email_followup": "Oggetto: [oggetto professionale e specifico]\\n\\nGentile [nome],\\n\\n[corpo email professionale, 3-4 paragrafi, tono caldo ma professionale, fa riferimento a cose specifiche dette durante la visita]\\n\\nIn attesa di un suo riscontro,\\n\\n[nome agente]\\n[firma]",
  "offerta_commerciale": "PROPOSTA COMMERCIALE\\n\\nSpett.le [Azienda],\\nAll'attenzione di [nome],\\n\\n[introduzione contestualizzata, 2 righe]\\n\\nCONDIZIONI PROPOSTE:\\n[dettaglio delle condizioni, sconti, termini discussi, in modo chiaro e strutturato]\\n\\nVALIDITÀ OFFERTA: [data]\\n\\nPer accettazione: ___________________\\nData: ___________________"
}}

IMPORTANTE:
- Estrai tutti i dettagli specifici dal testo (nomi, prodotti, sconti, date, obiezioni)
- Il tono deve essere professionale ma non robotico
- Le date vanno in formato italiano (es. 21 marzo 2026)
- Se mancano informazioni usa [da completare] per i campi obbligatori"""

    t0 = time.perf_counter()
    message = await _call_claude(prompt, max_tokens=2048)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    response_text = message.content[0].text
    result = extract_json(response_text)

    required_fields = ["report_visita", "email_followup", "offerta_commerciale"]
    for field in required_fields:
        if field not in result:
            raise ValueError(f"Campo mancante nella risposta AI: {field}")

    usage = getattr(message, "usage", None)
    tokens_used = (usage.input_tokens + usage.output_tokens) if usage else None

    return {
        "cliente_nome": result.get("cliente_nome", ""),
        "azienda_cliente": result.get("azienda_cliente", ""),
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
