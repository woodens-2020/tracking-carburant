"""
Relevés du module Crédits, en PDF et en Excel.

  - Relevé d'un crédit : toutes ses opérations dans l'ordre, avec le solde
    après chacune (montant initial, augmentations, paiements).
  - Relevé des paiements : tous les paiements d'une période (filtrés par
    sens et devise), puis la situation de chaque crédit concerné.

Les montants HTG et USD ne sont jamais additionnés : un total par devise.
"""
from __future__ import annotations

import io
import re
from datetime import date as date_type
from decimal import Decimal
from typing import Optional
from xml.sax.saxutils import escape

from tz_utils import today_haiti

SENS_LIB = {"DETTE": "Nous devons", "CREANCE": "On nous doit"}
STATUT_LIB = {"ACTIF": "Actif", "SOLDE": "Soldé", "ANNULE": "Annulé"}
TYPE_LIB = {
    ("DETTE", "DEPOT"): "Paiement versé",
    ("CREANCE", "DEPOT"): "Paiement reçu",
    ("DETTE", "AUGMENTATION"): "Augmentation",
    ("CREANCE", "AUGMENTATION"): "Augmentation",
}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ── Outils ────────────────────────────────────────────────────────────────────

def _m(v, devise: str = "") -> str:
    """1 234 567,89 HTG (espaces insécables pour ne jamais couper le nombre)."""
    s = f"{float(v):,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} {devise}" if devise else s


def _d(d) -> str:
    return d.strftime("%d/%m/%Y") if d else "—"


def _entreprise() -> dict:
    """Identité imprimée en tête des relevés (variables BRANDING_* du
    déploiement ; à bpccp, les valeurs par défaut sont les siennes)."""
    import main
    b = main._BRANDING
    tels = " · ".join(x for x in (b.get("telephone1"), b.get("telephone2")) if x)
    return {"nom": b.get("raison_sociale") or "", "complexe": b.get("complexe") or "",
            "adresse": b.get("adresse") or "", "tels": tels}


def nom_fichier(*parts) -> str:
    s = "_".join(str(p) for p in parts if p)
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s.encode("ascii", "ignore").decode()).strip("-")
    return s[:120] or "releve"


def lignes_credit(c) -> list[dict]:
    """Opérations d'un crédit dans l'ordre, avec le solde après chacune.
    Première ligne : le montant initial à la date du crédit."""
    solde = Decimal(c.montant_initial)
    out = [{"date": c.date_credit, "operation": "Montant initial", "mode": "", "note": c.description or "",
            "plus": Decimal(c.montant_initial), "moins": None, "solde": solde, "par": ""}]
    for m in sorted(c.mouvements, key=lambda x: (x.date_mouvement, x.id)):
        if m.type == "AUGMENTATION":
            solde += m.montant
        else:
            solde -= m.montant
        out.append({
            "date": m.date_mouvement, "operation": TYPE_LIB[(c.sens, m.type)],
            "mode": " · ".join(x for x in (m.mode_paiement, m.reference) if x),
            "note": m.note or "",
            "plus": m.montant if m.type == "AUGMENTATION" else None,
            "moins": m.montant if m.type == "DEPOT" else None,
            "solde": solde,
            "par": (m.createur.nom_complet or m.createur.username) if m.createur else "",
        })
    return out


def paiements_periode(credits: list, d_debut: date_type, d_fin: date_type) -> dict:
    """Paiements (DEPOT) de la période, par devise, et la situation des
    crédits concernés (ceux qui ont un paiement dans la période)."""
    par_devise: dict = {}
    for c in credits:
        for m in c.mouvements:
            if m.type != "DEPOT" or not (d_debut <= m.date_mouvement <= d_fin):
                continue
            par_devise.setdefault(c.devise, []).append((m, c))
    for dv in par_devise:
        par_devise[dv].sort(key=lambda x: (x[0].date_mouvement, x[0].id))
    ids = {c.id for lst in par_devise.values() for _, c in lst}
    situation = sorted((c for c in credits if c.id in ids), key=lambda c: (c.devise, c.tiers_nom.lower()))
    return {"paiements": par_devise, "situation": situation}


# ── PDF ───────────────────────────────────────────────────────────────────────

def _pdf_base(titre: str, sous_titre: str, paysage: bool = False):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate

    taille = landscape(A4) if paysage else A4
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=taille, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
                            topMargin=1.4 * cm, bottomMargin=1.4 * cm, title=titre)
    S = {
        "ent": ParagraphStyle("ent", fontName="Helvetica-Bold", fontSize=11, textColor=colors.HexColor("#0f172a")),
        "ent2": ParagraphStyle("ent2", fontName="Helvetica", fontSize=8, textColor=colors.HexColor("#64748b"), leading=10),
        "titre": ParagraphStyle("titre", fontName="Helvetica-Bold", fontSize=17, leading=21,
                                textColor=colors.HexColor("#0f172a"), spaceBefore=10, spaceAfter=4),
        "sous": ParagraphStyle("sous", fontName="Helvetica", fontSize=9.5, textColor=colors.HexColor("#475569"),
                               spaceAfter=8, leading=13),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11.5, textColor=colors.HexColor("#0f172a"),
                             spaceBefore=12, spaceAfter=6),
        "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.5, leading=10.5),
        "cellb": ParagraphStyle("cellb", fontName="Helvetica-Bold", fontSize=8.5, leading=10.5),
        "petit": ParagraphStyle("petit", fontName="Helvetica", fontSize=7.5, textColor=colors.HexColor("#64748b")),
    }
    e = _entreprise()
    story = []
    if e["nom"] or e["complexe"]:
        story.append(Paragraph(escape(" · ".join(x for x in (e["nom"], e["complexe"]) if x)), S["ent"]))
        info = " · ".join(x for x in (e["adresse"], e["tels"]) if x)
        if info:
            story.append(Paragraph(escape(info), S["ent2"]))
    story.append(Paragraph(escape(titre), S["titre"]))
    story.append(Paragraph(sous_titre, S["sous"]))
    story.append(HRFlowable(width="100%", thickness=1.2, color=colors.HexColor("#f97316"), spaceAfter=10))
    larg = taille[0] - 3 * cm
    return buf, doc, story, S, larg


def _pdf_table(data, larg_cols, entete=True, total=False, droite_depuis: Optional[int] = None,
               droite_jusqua: int = -1):
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle
    t = Table(data, colWidths=larg_cols, repeatRows=1 if entete else 0)
    st = [
        ("FONT", (0, 0), (-1, -1), "Helvetica", 8.5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if entete:
        st += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
               ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
               ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8)]
        st += [("ROWBACKGROUNDS", (0, 1), (-1, -2 if total else -1), [colors.white, colors.HexColor("#f8fafc")])]
    if total:
        st += [("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#fff7ed")),
               ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 8.5),
               ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.HexColor("#f97316"))]
    if droite_depuis is not None:
        st += [("ALIGN", (droite_depuis, 0), (droite_jusqua, -1), "RIGHT")]
    t.setStyle(TableStyle(st))
    return t


def _pdf_cartes(cartes: list[tuple[str, str, str]], larg):
    """Rangée de petites cartes (libellé, valeur, couleur)."""
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, Table, TableStyle
    lib = ParagraphStyle("kl", fontName="Helvetica-Bold", fontSize=7, textColor=colors.HexColor("#64748b"))
    cells = []
    for l, v, col in cartes:
        val = ParagraphStyle("kv", fontName="Helvetica-Bold", fontSize=11.5, textColor=colors.HexColor(col), leading=14)
        cells.append([Paragraph(escape(l.upper()), lib), Paragraph(escape(v), val)])
    t = Table([cells], colWidths=[larg / len(cartes)] * len(cartes))
    t.setStyle(TableStyle([
        ("BOX", (i, 0), (i, 0), 0.6, colors.HexColor("#cbd5e1")) for i in range(len(cartes))
    ] + [("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 6),
         ("BOTTOMPADDING", (0, 0), (-1, -1), 7), ("LEFTPADDING", (0, 0), (-1, -1), 8)]))
    return t


def _pied(story, S):
    from reportlab.platypus import Paragraph, Spacer
    story.append(Spacer(1, 10))
    story.append(Paragraph(escape(f"Document généré le {_d(today_haiti())} — Konekta, module Crédits."), S["petit"]))


def pdf_releve_credit(c) -> bytes:
    from reportlab.platypus import Paragraph
    dv = c.devise
    sous = (f"<b>{SENS_LIB[c.sens]}</b> · {dv} · crédit du {_d(c.date_credit)}"
            + (f" · échéance {_d(c.date_echeance)}" if c.date_echeance else "")
            + f" · statut <b>{STATUT_LIB[c.statut]}</b>")
    if c.tiers_contact:
        sous += f"<br/>Contact : {escape(c.tiers_contact)}"
    if c.description:
        sous += f"<br/>{escape(c.description)}"
    buf, doc, story, S, larg = _pdf_base(f"Relevé — {c.tiers_nom}", sous, paysage=True)
    du, dep = c.total_du(), c.total_depose()
    payer = c.sens == "DETTE"
    story.append(_pdf_cartes([
        ("Montant initial", _m(c.montant_initial, dv), "#0f172a"),
        ("Augmentations", "+ " + _m(du - c.montant_initial, dv), "#c2410c"),
        ("Déjà payé" if payer else "Déjà reçu", _m(dep, dv), "#15803d"),
        ("Reste à payer" if payer else "Reste à encaisser", _m(du - dep, dv), "#b91c1c" if du - dep > 0 else "#15803d"),
    ], larg))
    story.append(Paragraph("Opérations", S["h2"]))
    P = lambda s, st="cell": Paragraph(escape(s or ""), S[st])
    data = [["Date", "Opération", "Mode · Réf.", "Note", "Augmentation", "Paiement", "Solde", "Saisi par"]]
    for l in lignes_credit(c):
        data.append([_d(l["date"]), P(l["operation"], "cellb"), P(l["mode"]), P(l["note"]),
                     _m(l["plus"]) if l["plus"] is not None else "", _m(l["moins"]) if l["moins"] is not None else "",
                     _m(l["solde"]), P(l["par"])])
    data.append(["", f"Total ({dv})", "", "", _m(du), _m(dep), _m(du - dep), ""])
    w = larg
    story.append(_pdf_table(data, [w * .09, w * .13, w * .14, w * .21, w * .11, w * .11, w * .11, w * .10],
                            total=True, droite_depuis=4, droite_jusqua=6))
    if c.notes:
        story.append(Paragraph("Notes", S["h2"]))
        story.append(Paragraph(escape(c.notes).replace("\n", "<br/>"), S["cell"]))
    _pied(story, S)
    doc.build(story)
    return buf.getvalue()


def pdf_paiements(credits: list, d_debut: date_type, d_fin: date_type, sens: Optional[str]) -> bytes:
    from reportlab.platypus import Paragraph
    sel = paiements_periode(credits, d_debut, d_fin)
    quoi = SENS_LIB.get(sens, "Nous devons et on nous doit")
    buf, doc, story, S, larg = _pdf_base(
        "Relevé des paiements", f"<b>{quoi}</b> · du {_d(d_debut)} au {_d(d_fin)}", paysage=True)
    P = lambda s, st="cell": Paragraph(escape(s or ""), S[st])
    if not sel["paiements"]:
        story.append(Paragraph("Aucun paiement sur cette période.", S["sous"]))
    for dv in sorted(sel["paiements"]):
        lst = sel["paiements"][dv]
        story.append(Paragraph(f"Paiements en {dv} — {len(lst)} paiement{'s' if len(lst) > 1 else ''}", S["h2"]))
        data = [["Date", "Tiers", "Sens", "Mode · Réf.", "Note", "Saisi par", "Montant"]]
        total = Decimal("0")
        for m, c in lst:
            total += m.montant
            data.append([_d(m.date_mouvement), P(c.tiers_nom, "cellb"), SENS_LIB[c.sens],
                         P(" · ".join(x for x in (m.mode_paiement, m.reference) if x)), P(m.note),
                         P((m.createur.nom_complet or m.createur.username) if m.createur else ""), _m(m.montant, dv)])
        data.append(["", f"Total {dv}", "", "", "", "", _m(total, dv)])
        w = larg
        story.append(_pdf_table(data, [w * .09, w * .22, w * .1, w * .15, w * .2, w * .11, w * .13],
                                total=True, droite_depuis=6))
    if sel["situation"]:
        story.append(Paragraph("Situation des crédits concernés (à ce jour)", S["h2"]))
        data = [["Tiers", "Sens", "Devise", "Échéance", "Statut", "Total dû", "Déjà payé", "Reste"]]
        for c in sel["situation"]:
            du, dep = c.total_du(), c.total_depose()
            data.append([P(c.tiers_nom, "cellb"), SENS_LIB[c.sens], c.devise, _d(c.date_echeance),
                         STATUT_LIB[c.statut] + (" (en retard)" if c.en_retard() else ""),
                         _m(du, c.devise), _m(dep, c.devise), _m(du - dep, c.devise)])
        w = larg
        story.append(_pdf_table(data, [w * .24, w * .1, w * .06, w * .09, w * .12, w * .13, w * .13, w * .13],
                                droite_depuis=5))
    _pied(story, S)
    doc.build(story)
    return buf.getvalue()


# ── Excel ─────────────────────────────────────────────────────────────────────

def _xl_outils():
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    fin = Side(style="thin", color="D5E0E8")
    return {
        "hdr_fill": PatternFill("solid", fgColor="0F172A"), "hdr_font": Font(name="Calibri", bold=True, color="FFFFFF", size=10),
        "tot_fill": PatternFill("solid", fgColor="FFF0CC"), "bold": Font(name="Calibri", bold=True, size=10),
        "titre": Font(name="Calibri", bold=True, size=15, color="0F172A"), "gris": Font(name="Calibri", size=10, color="64748B"),
        "border": Border(left=fin, right=fin, top=fin, bottom=fin),
        "droite": Alignment(horizontal="right", vertical="center"),
        "wrap": Alignment(wrap_text=True, vertical="top"),
        "MNT": '#,##0.00',
    }


def _xl_entete(ws, titre: str, lignes: list[str], o) -> int:
    e = _entreprise()
    r = 1
    if e["nom"] or e["complexe"]:
        ws.cell(r, 1, " · ".join(x for x in (e["nom"], e["complexe"]) if x)).font = o["bold"]; r += 1
    ws.cell(r, 1, titre).font = o["titre"]; r += 1
    for l in lignes:
        ws.cell(r, 1, l).font = o["gris"]; r += 1
    return r + 1


def _xl_table(ws, r: int, entetes: list[str], lignes: list[list], o, montants: set[int], total: Optional[list] = None) -> int:
    for j, h in enumerate(entetes, 1):
        c = ws.cell(r, j, h)
        c.fill, c.font, c.border = o["hdr_fill"], o["hdr_font"], o["border"]
        if j in montants:
            c.alignment = o["droite"]
    for ligne in lignes:
        r += 1
        for j, v in enumerate(ligne, 1):
            c = ws.cell(r, j, float(v) if isinstance(v, Decimal) else v)
            c.border = o["border"]
            if j in montants:
                c.number_format, c.alignment = o["MNT"], o["droite"]
            elif isinstance(v, date_type):
                c.number_format = "DD/MM/YYYY"
    if total:
        r += 1
        for j, v in enumerate(total, 1):
            c = ws.cell(r, j, float(v) if isinstance(v, Decimal) else v)
            c.fill, c.font, c.border = o["tot_fill"], o["bold"], o["border"]
            if j in montants:
                c.number_format, c.alignment = o["MNT"], o["droite"]
    return r + 2


def _xl_largeurs(ws, largeurs: list[int]):
    from openpyxl.utils import get_column_letter
    for j, w in enumerate(largeurs, 1):
        ws.column_dimensions[get_column_letter(j)].width = w


def xlsx_releve_credit(c) -> bytes:
    from openpyxl import Workbook
    o = _xl_outils()
    wb = Workbook()
    ws = wb.active
    ws.title = "Relevé"
    dv = c.devise
    du, dep = c.total_du(), c.total_depose()
    info = [f"{SENS_LIB[c.sens]} · {dv} · crédit du {_d(c.date_credit)}"
            + (f" · échéance {_d(c.date_echeance)}" if c.date_echeance else "") + f" · {STATUT_LIB[c.statut]}"]
    if c.tiers_contact:
        info.append(f"Contact : {c.tiers_contact}")
    if c.description:
        info.append(c.description)
    r = _xl_entete(ws, f"Relevé — {c.tiers_nom}", info, o)
    payer = c.sens == "DETTE"
    for lib, v in (("Montant initial", c.montant_initial), ("Augmentations", du - c.montant_initial),
                   ("Déjà payé" if payer else "Déjà reçu", dep), ("Reste à payer" if payer else "Reste à encaisser", du - dep)):
        ws.cell(r, 1, lib).font = o["bold"]
        x = ws.cell(r, 2, float(v)); x.number_format = o["MNT"]
        ws.cell(r, 3, dv)
        r += 1
    r += 1
    lignes = [[l["date"], l["operation"], l["mode"], l["note"], l["plus"], l["moins"], l["solde"], l["par"]]
              for l in lignes_credit(c)]
    _xl_table(ws, r, ["Date", "Opération", "Mode · Réf.", "Note", f"Augmentation ({dv})", f"Paiement ({dv})",
                      f"Solde ({dv})", "Saisi par"], lignes, o, {5, 6, 7},
              total=["", "Total", "", "", du, dep, du - dep, ""])
    _xl_largeurs(ws, [12, 18, 22, 34, 17, 17, 17, 20])
    ws.freeze_panes = ws.cell(r + 1, 1)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def xlsx_paiements(credits: list, d_debut: date_type, d_fin: date_type, sens: Optional[str]) -> bytes:
    from openpyxl import Workbook
    o = _xl_outils()
    sel = paiements_periode(credits, d_debut, d_fin)
    wb = Workbook()
    ws = wb.active
    ws.title = "Paiements"
    quoi = SENS_LIB.get(sens, "Nous devons et on nous doit")
    r = _xl_entete(ws, "Relevé des paiements", [f"{quoi} · du {_d(d_debut)} au {_d(d_fin)}"], o)
    if not sel["paiements"]:
        ws.cell(r, 1, "Aucun paiement sur cette période.").font = o["gris"]
    for dv in sorted(sel["paiements"]):
        lst = sel["paiements"][dv]
        ws.cell(r, 1, f"Paiements en {dv}").font = o["bold"]
        lignes, total = [], Decimal("0")
        for m, c in lst:
            total += m.montant
            lignes.append([m.date_mouvement, c.tiers_nom, SENS_LIB[c.sens], m.mode_paiement or "", m.reference or "",
                           m.note or "", (m.createur.nom_complet or m.createur.username) if m.createur else "", m.montant])
        r = _xl_table(ws, r + 1, ["Date", "Tiers", "Sens", "Mode", "Référence", "Note", "Saisi par", f"Montant ({dv})"],
                      lignes, o, {8}, total=["", f"Total {dv}", "", "", "", "", "", total])
    _xl_largeurs(ws, [12, 30, 14, 14, 16, 34, 20, 18])
    ws2 = wb.create_sheet("Situation")
    r2 = _xl_entete(ws2, "Situation des crédits concernés", [f"À ce jour ({_d(today_haiti())})"], o)
    lignes = []
    for c in sel["situation"]:
        du, dep = c.total_du(), c.total_depose()
        lignes.append([c.tiers_nom, SENS_LIB[c.sens], c.devise, c.date_echeance,
                       STATUT_LIB[c.statut] + (" (en retard)" if c.en_retard() else ""), du, dep, du - dep])
    _xl_table(ws2, r2, ["Tiers", "Sens", "Devise", "Échéance", "Statut", "Total dû", "Déjà payé", "Reste"],
              lignes, o, {6, 7, 8})
    _xl_largeurs(ws2, [30, 14, 8, 12, 18, 16, 16, 16])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
