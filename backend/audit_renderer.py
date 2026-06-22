"""
Rendeurs pour le document d'audit IA — PDF et DOCX.

Le texte d'audit retourné par l'IA est en Markdown structuré (# ## ### ####).
Les rendeurs le parsent et le mettent en page professionnellement.

render_audit_pdf(text, date_debut, date_fin)  -> bytes
render_audit_docx(text, date_debut, date_fin) -> bytes
"""
from __future__ import annotations

import re
from datetime import date
from io import BytesIO


# ══════════════════════════════════════════════════════════════════════════════
# Parser Markdown → blocs structurés
# ══════════════════════════════════════════════════════════════════════════════

def _parse(text: str) -> list[dict]:
    blocks = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            blocks.append({"t": "space"})
        elif s.startswith("#### "):
            blocks.append({"t": "h4", "c": s[5:]})
        elif s.startswith("### "):
            blocks.append({"t": "h3", "c": s[4:]})
        elif s.startswith("## "):
            blocks.append({"t": "h2", "c": s[3:]})
        elif s.startswith("# "):
            blocks.append({"t": "h1", "c": s[2:]})
        elif re.match(r"^---+$", s):
            blocks.append({"t": "hr"})
        elif re.match(r"^(\d+\.|[•\-\*])\s", s):
            clean = re.sub(r"^(\d+\.\s*|[•\-\*]\s*)", "", s)
            blocks.append({"t": "li", "c": clean})
        elif s.startswith("*") and s.endswith("*") and not s.startswith("**"):
            blocks.append({"t": "em", "c": s.strip("*")})
        else:
            blocks.append({"t": "p", "c": s})
    return blocks


def _md_bold(text: str) -> str:
    """Convertit **bold** en balises reportlab <b>bold</b>."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)


def _strip_bold(text: str) -> str:
    """Supprime les ** du texte (pour les titres Word)."""
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text)


# ══════════════════════════════════════════════════════════════════════════════
# Rendu PDF
# ══════════════════════════════════════════════════════════════════════════════

def render_audit_pdf(text: str, date_debut: date, date_fin: date) -> bytes:
    from reportlab.lib        import colors
    from reportlab.lib.units  import cm
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums  import TA_LEFT, TA_CENTER, TA_JUSTIFY
    from reportlab.platypus   import (
        SimpleDocTemplate, Paragraph, Spacer,
        HRFlowable, PageBreak, KeepTogether,
    )

    NAVY   = colors.HexColor("#0f1e35")
    AMBER  = colors.HexColor("#f7a93b")
    VIOLET = colors.HexColor("#7c3aed")
    TEAL   = colors.HexColor("#3fb6a8")
    GRAY   = colors.HexColor("#555555")
    LGRAY  = colors.HexColor("#888888")
    WHITE  = colors.white

    def _s(name, **kw):
        return ParagraphStyle(name, **kw)

    s_meta   = _s("meta",   fontName="Helvetica-Oblique",    fontSize=9,  textColor=LGRAY, spaceAfter=3, alignment=TA_CENTER)
    s_cover1 = _s("cov1",   fontName="Helvetica-Bold",       fontSize=22, textColor=NAVY,  spaceAfter=6, alignment=TA_CENTER)
    s_cover2 = _s("cov2",   fontName="Helvetica-Bold",       fontSize=13, textColor=VIOLET,spaceAfter=4, alignment=TA_CENTER)
    s_h1     = _s("H1",     fontName="Helvetica-Bold",       fontSize=14, textColor=WHITE,
                  backColor=NAVY, spaceBefore=18, spaceAfter=10, leftIndent=8, leading=22)
    s_h2     = _s("H2",     fontName="Helvetica-Bold",       fontSize=12, textColor=NAVY,
                  spaceBefore=14, spaceAfter=5)
    s_h3     = _s("H3",     fontName="Helvetica-Bold",       fontSize=11, textColor=VIOLET,
                  spaceBefore=10, spaceAfter=4)
    s_h4     = _s("H4",     fontName="Helvetica-BoldOblique",fontSize=10, textColor=TEAL,
                  spaceBefore=7,  spaceAfter=3)
    s_para   = _s("para",   fontName="Helvetica",            fontSize=10, textColor=colors.black,
                  leading=16, spaceAfter=6, alignment=TA_JUSTIFY)
    s_li     = _s("li",     fontName="Helvetica",            fontSize=10, textColor=colors.black,
                  leading=15, spaceAfter=4, leftIndent=22, bulletIndent=8)
    s_em     = _s("em",     fontName="Helvetica-Oblique",    fontSize=9,  textColor=LGRAY,
                  spaceAfter=4, alignment=TA_CENTER)

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2.2*cm, rightMargin=2.2*cm,
        topMargin=2.5*cm,  bottomMargin=2.2*cm,
    )

    story = []

    # ── Page de couverture ──
    story.append(Spacer(1, 1.2*cm))
    story.append(Paragraph("DOCUMENT D'AUDIT CONFIDENTIEL", s_meta))
    story.append(Spacer(1, 0.4*cm))
    story.append(Paragraph("PétroSync · Station Carburant", s_cover1))
    story.append(Spacer(1, 0.3*cm))
    story.append(Paragraph(
        f"Période analysée : {date_debut.strftime('%d/%m/%Y')} – {date_fin.strftime('%d/%m/%Y')}",
        s_cover2,
    ))
    story.append(Spacer(1, 0.2*cm))
    story.append(Paragraph(f"Généré le {date.today().strftime('%d/%m/%Y')} par Intelligence Artificielle", s_meta))
    story.append(HRFlowable(width="100%", thickness=2.5, color=AMBER, spaceAfter=20, spaceBefore=14))
    story.append(Spacer(1, 0.6*cm))

    # ── Contenu IA ──
    blocks = _parse(text)
    skip_next_cover = True  # le premier # est le titre de couverture déjà rendu

    for b in blocks:
        t = b["t"]

        if t == "space":
            story.append(Spacer(1, 4))

        elif t == "hr":
            story.append(HRFlowable(width="100%", thickness=0.6, color=GRAY, spaceAfter=8, spaceBefore=6))

        elif t == "h1":
            if skip_next_cover:
                skip_next_cover = False
                continue
            story.append(PageBreak())
            story.append(Paragraph(_strip_bold(b["c"]), s_h1))

        elif t == "h2":
            story.append(KeepTogether([
                Paragraph(_strip_bold(b["c"]), s_h2),
                HRFlowable(width="100%", thickness=1, color=AMBER, spaceAfter=4),
            ]))

        elif t == "h3":
            story.append(Paragraph(_strip_bold(b["c"]), s_h3))

        elif t == "h4":
            story.append(Paragraph(_strip_bold(b["c"]), s_h4))

        elif t == "li":
            c = _md_bold(b["c"])
            story.append(Paragraph(f"• {c}", s_li))

        elif t == "em":
            story.append(Paragraph(f"<i>{b['c']}</i>", s_em))

        elif t == "p":
            if not b["c"]:
                continue
            c = _md_bold(b["c"])
            story.append(Paragraph(c, s_para))

    doc.build(story)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
# Rendu DOCX
# ══════════════════════════════════════════════════════════════════════════════

def render_audit_docx(text: str, date_debut: date, date_fin: date) -> bytes:
    from docx              import Document
    from docx.shared       import Pt, RGBColor, Cm
    from docx.enum.text    import WD_ALIGN_PARAGRAPH

    NAVY   = RGBColor(0x0f, 0x1e, 0x35)
    AMBER  = RGBColor(0xf7, 0xa9, 0x3b)
    VIOLET = RGBColor(0x7c, 0x3a, 0xed)
    TEAL   = RGBColor(0x3f, 0xb6, 0xa8)
    GRAY   = RGBColor(0x88, 0x88, 0x88)

    doc = Document()

    for section in doc.sections:
        section.left_margin   = Cm(2.5)
        section.right_margin  = Cm(2.5)
        section.top_margin    = Cm(2.5)
        section.bottom_margin = Cm(2.2)

    def _set_color(run, rgb):
        run.font.color.rgb = rgb

    def _add_run_md(para, content):
        """Ajoute du texte avec support **gras** inline."""
        parts = re.split(r"(\*\*[^*]+\*\*)", content)
        for part in parts:
            if part.startswith("**") and part.endswith("**"):
                r = para.add_run(part[2:-2])
                r.bold = True
            elif part:
                para.add_run(part)

    # ── Couverture ──
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("DOCUMENT D'AUDIT CONFIDENTIEL")
    r.font.size = Pt(9); r.font.italic = True; _set_color(r, GRAY)

    p2 = doc.add_paragraph()
    p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r2 = p2.add_run("PétroSync · Station Carburant")
    r2.font.size = Pt(22); r2.bold = True; _set_color(r2, NAVY)

    p3 = doc.add_paragraph()
    p3.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r3 = p3.add_run(
        f"Période : {date_debut.strftime('%d/%m/%Y')} – {date_fin.strftime('%d/%m/%Y')}"
    )
    r3.font.size = Pt(14); r3.bold = True; _set_color(r3, VIOLET)

    p4 = doc.add_paragraph()
    p4.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r4 = p4.add_run(f"Généré le {date.today().strftime('%d/%m/%Y')} par Intelligence Artificielle")
    r4.font.size = Pt(9); r4.italic = True; _set_color(r4, GRAY)

    doc.add_paragraph()

    # ── Contenu IA ──
    blocks = _parse(text)
    skip_h1 = True

    for b in blocks:
        t = b["t"]

        if t == "space":
            continue

        elif t == "hr":
            p = doc.add_paragraph()
            r = p.add_run("─" * 65)
            r.font.size = Pt(7); _set_color(r, GRAY)

        elif t == "h1":
            if skip_h1:
                skip_h1 = False
                continue
            h = doc.add_heading(_strip_bold(b["c"]), level=1)
            for run in h.runs:
                _set_color(run, NAVY)

        elif t == "h2":
            h = doc.add_heading(_strip_bold(b["c"]), level=2)
            for run in h.runs:
                _set_color(run, NAVY)

        elif t == "h3":
            h = doc.add_heading(_strip_bold(b["c"]), level=3)
            for run in h.runs:
                _set_color(run, VIOLET)

        elif t == "h4":
            h = doc.add_heading(_strip_bold(b["c"]), level=4)
            for run in h.runs:
                _set_color(run, TEAL)

        elif t == "li":
            p = doc.add_paragraph(style="List Bullet")
            _add_run_md(p, b["c"])

        elif t == "em":
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r = p.add_run(b["c"])
            r.font.size = Pt(9); r.italic = True; _set_color(r, GRAY)

        elif t == "p":
            if not b["c"]:
                continue
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            _add_run_md(p, b["c"])

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()
