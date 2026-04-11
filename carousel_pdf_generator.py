"""
Carousel PDF LinkedIn Generator — Trasforma report AgentIA in 5-slide PDF ottimizzato per LinkedIn.
Uso: LinkedInCarouselGenerator(report_visita, email, offerta, nome_agente).generate()
"""

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
import textwrap
from datetime import datetime


class LinkedInCarouselGenerator:
    """
    Genera carousel PDF (5 slide) dai 3 documenti di AgentIA.
    Ottimizzato per LinkedIn: layout pulito, CTA chiara, font leggibili.
    """

    # Brand colors
    BRAND_PRIMARY = colors.HexColor("#0052CC")  # LinkedIn blue
    BRAND_SECONDARY = colors.HexColor("#F5A623")  # Accent (opportunità)
    BG_LIGHT = colors.HexColor("#F8FAFC")
    TEXT_DARK = colors.HexColor("#1A202C")
    TEXT_LIGHT = colors.HexColor("#718096")

    def __init__(self, report_visita, email_followup, offerta_commerciale, nome_agente, azienda_cliente=""):
        self.report = report_visita
        self.email = email_followup
        self.offerta = offerta_commerciale
        self.nome_agente = nome_agente
        self.azienda_cliente = azienda_cliente
        self.timestamp = datetime.now().strftime("%d.%m.%Y")

    def _extract_key_info(self):
        """Estrae info critiche dai 3 documenti."""
        # Estrai cliente da report
        cliente_match = self.report.split("Cliente:")[1].split("\n")[0].strip() if "Cliente:" in self.report else ""

        # Estrai opportunità da report
        oppurtunita_lines = []
        if "RISULTATI E OPPORTUNITÀ:" in self.report:
            opp_text = self.report.split("RISULTATI E OPPORTUNITÀ:")[1].split("\n\n")[0]
            oppurtunita_lines = [l.strip() for l in opp_text.split("\n") if l.strip()][:2]

        # Estrai highlights offerta
        highlights = []
        if "CONDIZIONI PROPOSTE:" in self.offerta:
            cond_text = self.offerta.split("CONDIZIONI PROPOSTE:")[1].split("\n\n")[0]
            highlights = [l.strip() for l in cond_text.split("\n") if l.strip()][:3]

        return {
            "cliente": cliente_match or "Cliente",
            "opportunita": oppurtunita_lines or ["Opportunità commerciale identificata"],
            "highlights": highlights or ["Proposte commerciali personalizzate"]
        }

    def generate(self, output_path="carousel.pdf"):
        """Genera PDF 5-slide."""
        doc = SimpleDocTemplate(
            output_path,
            pagesize=letter,
            rightMargin=0.5*inch,
            leftMargin=0.5*inch,
            topMargin=0.5*inch,
            bottomMargin=0.5*inch,
        )

        styles = getSampleStyleSheet()

        # Custom styles per LinkedIn
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=28,
            textColor=self.BRAND_PRIMARY,
            spaceAfter=12,
            alignment=TA_CENTER,
            fontName='Helvetica-Bold'
        )

        subtitle_style = ParagraphStyle(
            'CustomSubtitle',
            parent=styles['Normal'],
            fontSize=14,
            textColor=self.TEXT_LIGHT,
            spaceAfter=6,
            alignment=TA_CENTER
        )

        body_style = ParagraphStyle(
            'CustomBody',
            parent=styles['Normal'],
            fontSize=11,
            textColor=self.TEXT_DARK,
            spaceAfter=8,
            alignment=TA_LEFT,
            leading=14
        )

        info = self._extract_key_info()

        # BUILD SLIDES
        story = []

        # SLIDE 1: Titolo + Hook
        story.extend(self._build_slide_1(title_style, subtitle_style, body_style, info))

        # SLIDE 2: Opportunità (evidenziare risultati visita)
        story.extend(self._build_slide_2(title_style, body_style, info))

        # SLIDE 3: Proposte Commerciali (da offerta)
        story.extend(self._build_slide_3(title_style, body_style, info))

        # SLIDE 4: Expertise + Metodologia
        story.extend(self._build_slide_4(title_style, body_style))

        # SLIDE 5: CTA + Contatti
        story.extend(self._build_slide_5(title_style, subtitle_style, body_style))

        # Generate PDF
        doc.build(story)
        return output_path

    def _build_slide_1(self, title_style, subtitle_style, body_style, info):
        """Slide 1: Titolo accattivante + hook."""
        story = []

        story.append(Spacer(1, 0.8*inch))
        story.append(Paragraph(f"📊 Visita Commerciale Conclusa", title_style))
        story.append(Paragraph(f"con {info['cliente']}", subtitle_style))
        story.append(Spacer(1, 0.3*inch))
        story.append(Paragraph(
            f"<b>Risultati tangibili & Opportunità identificate</b><br/>"
            f"Data: {self.timestamp} | Agente: {self.nome_agente}",
            body_style
        ))
        story.append(Spacer(1, 0.4*inch))
        story.append(Paragraph(
            "👇 Swipe per dettagli della proposta commerciale",
            subtitle_style
        ))

        story.append(PageBreak())
        return story

    def _build_slide_2(self, title_style, body_style, info):
        """Slide 2: Opportunità commerciali."""
        story = []

        story.append(Spacer(1, 0.6*inch))
        story.append(Paragraph("🎯 Opportunità Identificate", title_style))
        story.append(Spacer(1, 0.3*inch))

        for opp in info['opportunita']:
            story.append(Paragraph(f"✓ {opp}", body_style))
            story.append(Spacer(1, 0.15*inch))

        story.append(Spacer(1, 0.3*inch))
        story.append(Paragraph(
            "<i>Potenziale di crescita significativo — Dettagli nella proposta commerciale →</i>",
            body_style
        ))

        story.append(PageBreak())
        return story

    def _build_slide_3(self, title_style, body_style, info):
        """Slide 3: Proposte commerciali (highlights offerta)."""
        story = []

        story.append(Spacer(1, 0.6*inch))
        story.append(Paragraph("💼 Proposte Commerciali", title_style))
        story.append(Spacer(1, 0.3*inch))

        for hl in info['highlights']:
            story.append(Paragraph(f"<b>→</b> {hl}", body_style))
            story.append(Spacer(1, 0.15*inch))

        story.append(Spacer(1, 0.3*inch))
        story.append(Paragraph(
            "<b>Condizioni esclusive per questo cliente</b><br/>"
            "<i>Valide fino alla data specificata nella documentazione</i>",
            body_style
        ))

        story.append(PageBreak())
        return story

    def _build_slide_4(self, title_style, body_style):
        """Slide 4: Expertise + metodologia."""
        story = []

        story.append(Spacer(1, 0.6*inch))
        story.append(Paragraph("🔧 Metodologia Collaudata", title_style))
        story.append(Spacer(1, 0.3*inch))

        story.append(Paragraph(
            "<b>Fase 1: Ascolto</b><br/>"
            "Analisi approfondita di esigenze e pain points<br/><br/>"
            "<b>Fase 2: Proposta</b><br/>"
            "Soluzioni commerciali personalizzate<br/><br/>"
            "<b>Fase 3: Risultati</b><br/>"
            "Implementazione e monitoraggio dei risultati",
            body_style
        ))

        story.append(PageBreak())
        return story

    def _build_slide_5(self, title_style, subtitle_style, body_style):
        """Slide 5: CTA + contatti."""
        story = []

        story.append(Spacer(1, 0.8*inch))
        story.append(Paragraph("✉️ Prossimi Step", title_style))
        story.append(Spacer(1, 0.3*inch))

        story.append(Paragraph(
            "<b>1. Revisione della proposta</b> — Commenti su elementi specifici<br/>"
            "<b>2. Meeting di allineamento</b> — Discussione dettagli commerciali<br/>"
            "<b>3. Approvazione & Implementazione</b> — Go-live tempistica",
            body_style
        ))

        story.append(Spacer(1, 0.3*inch))
        story.append(Paragraph(
            f"<b>Agente:</b> {self.nome_agente}<br/>"
            f"<b>Data creazione:</b> {self.timestamp}",
            subtitle_style
        ))

        story.append(Spacer(1, 0.2*inch))
        story.append(Paragraph(
            "📱 Contattami per domande o chiarimenti — Connessione su LinkedIn",
            body_style
        ))

        return story


# TEST/DEMO
if __name__ == "__main__":
    # Sample data per testing
    sample_report = """REPORT DI VISITA COMMERCIALE

Data: 11 marzo 2026
Cliente: Mario Rossi
Azienda: ACME Srl

OBIETTIVO VISITA:
Presentare soluzione software per automazione processi gestionale.

SVOLGIMENTO:
Discussione sulla situazione attuale del cliente — gestione manuale su spreadsheet. Cliente interessato a soluzione cloud-based.
Dimostrazione live del software con focus su integrazioni.

RISULTATI E OPPORTUNITÀ:
Cliente ha manifestato interesse concreto per l'implementazione entro Q2 2026.
Opportunità di vendita accessoria: training team + supporto customizzazione.

NEXT STEPS:
- Inviare proposta commerciale dettagliata (entro 5 gg)
- Follow-up call (settimana prossima)
- Demo accesso al test environment (con team del cliente)

Note riservate: Cliente ha budget confermato, decision maker è presente."""

    sample_email = """Oggetto: Proposta software gestionale — ACME Srl

Gentile Mario,

Grazie per il tempo dedicato durante la visita di oggi.
La tua descrizione delle attuali criticità è stata molto chiara e la soluzione che abbiamo presentato sembra perfettamente allineata ai tuoi obiettivi.

Come discusso, procederò con l'invio della proposta formale entro 5 giorni.
Nel frattempo, se hai domande specifiche sul funzionamento o integrazioni, resto a tua disposizione.

In attesa di un vostro riscontro,

[Agente]"""

    sample_offerta = """PROPOSTA COMMERCIALE

Spett.le ACME Srl
All'attenzione di Mario Rossi

Vi sottoponiamo una proposta per l'implementazione della soluzione software di gestione aziendale.

CONDIZIONI PROPOSTE:
- Licenza annuale: €2.500 (anziché €3.000 — sconto early adopter 15%)
- Setup e migrazione dati: €500
- Training team (3 giorni): €600
- Supporto premium anno 1: incluso
- ROI stimato: 6 mesi

VALIDITÀ OFFERTA: 30 aprile 2026

Per accettazione: ___________________
Data: ___________________"""

    # Generate PDF
    gen = LinkedInCarouselGenerator(
        report_visita=sample_report,
        email_followup=sample_email,
        offerta_commerciale=sample_offerta,
        nome_agente="Roberto Pizzini",
        azienda_cliente="ACME Srl"
    )

    pdf_path = gen.generate("test_carousel.pdf")
    print(f"OK - PDF generato: {pdf_path}")
