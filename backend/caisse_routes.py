"""
Routes de gestion des sessions de caisse.

Préfixe : /api/pos/caisse

Endpoints caissière :
  GET  /caissieres                         — liste des employés caissiers
  GET  /dashboard?caissier_id=N&date=...   — stats du jour pour une caissière
  GET  /sessions                           — liste des sessions (admin + caissière)
  GET  /sessions/{id}                      — détail d'une session
  POST /sessions/ouvrir                    — ouvrir / retrouver la session du jour
  POST /sessions/{id}/soumettre            — caissière soumet son rapport
  POST /sessions/{id}/valider              — admin valide la session
  GET  /sessions/{id}/rapport.pdf          — export PDF
  GET  /sessions/{id}/rapport.xlsx         — export XLSX
"""
from __future__ import annotations

import io
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import BarVente, BarLigneVente, BarSessionCaisse, Employe, Utilisateur

router = APIRouter(prefix="/api/pos/caisse", tags=["caisse"])


# ── helpers ──────────────────────────────────────────────────────────

def _uid(request: Request) -> Optional[int]:
    return getattr(request.state, "utilisateur_id", None)


def _session_ou_404(session_id: int, db: Session) -> BarSessionCaisse:
    s = db.query(BarSessionCaisse).filter_by(id=session_id).first()
    if not s:
        raise HTTPException(404, "Session introuvable")
    return s


def _ventes_session(session: BarSessionCaisse, db: Session):
    from sqlalchemy import func as _f
    today_start = datetime.combine(session.date_session, datetime.min.time()).replace(tzinfo=timezone.utc)
    today_end   = datetime.combine(session.date_session, datetime.max.time()).replace(tzinfo=timezone.utc)
    return (
        db.query(BarVente)
        .filter(
            BarVente.caissier_id == session.caissier_id,
            BarVente.date_heure  >= today_start,
            BarVente.date_heure  <= today_end,
            BarVente.statut      != "ANNULEE",
        )
        .order_by(BarVente.date_heure)
        .all()
    )


def _stats_ventes(ventes):
    total      = sum(float(v.montant_total) for v in ventes)
    cash       = sum(float(v.montant_paye)  for v in ventes if v.mode_paiement in ("CASH", "MIXTE"))
    credit_tot = sum(float(v.montant_total) for v in ventes if v.mode_paiement == "CREDIT")
    modes      = {}
    for v in ventes:
        modes[v.mode_paiement] = modes.get(v.mode_paiement, 0) + float(v.montant_total)

    produits: dict = {}
    for v in ventes:
        for l in v.lignes:
            nom = (l.produit.nom if l.produit else None) or (l.cuisine_plat.nom if l.cuisine_plat else "?")
            if nom not in produits:
                produits[nom] = {"nom": nom, "quantite": 0.0, "total": 0.0}
            produits[nom]["quantite"] += float(l.quantite)
            produits[nom]["total"]    += float(l.sous_total)

    top = sorted(produits.values(), key=lambda x: x["total"], reverse=True)[:10]
    return {
        "nb_ventes":  len(ventes),
        "total":      total,
        "cash":       cash,
        "credit":     credit_tot,
        "par_mode":   modes,
        "top_produits": top,
    }


def _session_dict(s: BarSessionCaisse, stats: dict | None = None) -> dict:
    return {
        "id":            s.id,
        "caissier_id":   s.caissier_id,
        "caissier_nom":  (s.caissier.nom + " " + s.caissier.prenom) if s.caissier else None,
        "date_session":  str(s.date_session),
        "statut":        s.statut,
        "soumis_at":     s.soumis_at.isoformat() if s.soumis_at else None,
        "valide_at":     s.valide_at.isoformat() if s.valide_at else None,
        "valide_par":    (s.valide_par.nom + " " + s.valide_par.prenom) if s.valide_par else None,
        "notes_admin":   s.notes_admin,
        **(stats or {}),
    }


# ── endpoints ────────────────────────────────────────────────────────

@router.get("/caissieres")
def liste_caissieres(db: Session = Depends(get_db)):
    """Tous les employés actifs (peuvent être affectés comme caissier)."""
    employes = (
        db.query(Employe)
        .filter(Employe.actif == True)
        .order_by(Employe.nom, Employe.prenom)
        .all()
    )
    return [
        {
            "id":             e.id,
            "nom":            e.nom,
            "prenom":         e.prenom,
            "poste":          e.poste,
            "utilisateur_id": e.utilisateur_id,
        }
        for e in employes
    ]


@router.get("/dashboard")
def dashboard_caissiere(
    caissier_id: int = Query(...),
    date_sel:    str = Query(None, alias="date"),
    db: Session = Depends(get_db),
):
    """Stats du jour (ou date choisie) pour une caissière."""
    if date_sel:
        try:
            jour = date.fromisoformat(date_sel)
        except ValueError:
            raise HTTPException(422, "date invalide (YYYY-MM-DD)")
    else:
        jour = datetime.now(tz=timezone.utc).date()

    employe = db.query(Employe).filter_by(id=caissier_id).first()
    if not employe:
        raise HTTPException(404, "Caissier introuvable")

    dt_start = datetime.combine(jour, datetime.min.time()).replace(tzinfo=timezone.utc)
    dt_end   = datetime.combine(jour, datetime.max.time()).replace(tzinfo=timezone.utc)

    ventes = (
        db.query(BarVente)
        .filter(
            BarVente.caissier_id == caissier_id,
            BarVente.date_heure  >= dt_start,
            BarVente.date_heure  <= dt_end,
            BarVente.statut      != "ANNULEE",
        )
        .order_by(BarVente.date_heure)
        .all()
    )

    session = (
        db.query(BarSessionCaisse)
        .filter_by(caissier_id=caissier_id, date_session=jour)
        .first()
    )

    # Evolution horaire (pour graphe)
    evolution: list = []
    cumul = 0.0
    for v in ventes:
        cumul += float(v.montant_total)
        evolution.append({
            "heure":   v.date_heure.astimezone(timezone.utc).strftime("%H:%M"),
            "montant": float(v.montant_total),
            "cumul":   cumul,
            "ticket":  v.numero_ticket,
        })

    stats = _stats_ventes(ventes)
    return {
        "date":          str(jour),
        "caissier_id":   caissier_id,
        "caissier_nom":  employe.nom + " " + employe.prenom,
        "session_id":    session.id if session else None,
        "session_statut": session.statut if session else None,
        "evolution":     evolution,
        **stats,
    }


@router.get("/sessions")
def liste_sessions(
    caissier_id: Optional[int] = Query(None),
    statut:      Optional[str] = Query(None),
    date_debut:  Optional[str] = Query(None),
    date_fin:    Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(BarSessionCaisse)
    if caissier_id:
        q = q.filter(BarSessionCaisse.caissier_id == caissier_id)
    if statut:
        q = q.filter(BarSessionCaisse.statut == statut.upper())
    if date_debut:
        q = q.filter(BarSessionCaisse.date_session >= date.fromisoformat(date_debut))
    if date_fin:
        q = q.filter(BarSessionCaisse.date_session <= date.fromisoformat(date_fin))

    sessions = q.order_by(BarSessionCaisse.date_session.desc(), BarSessionCaisse.id.desc()).all()

    result = []
    for s in sessions:
        ventes = _ventes_session(s, db)
        stats  = _stats_ventes(ventes)
        result.append(_session_dict(s, stats))
    return result


@router.get("/sessions/{session_id}")
def detail_session(session_id: int, db: Session = Depends(get_db)):
    s      = _session_ou_404(session_id, db)
    ventes = _ventes_session(s, db)
    stats  = _stats_ventes(ventes)
    d      = _session_dict(s, stats)
    d["ventes"] = [
        {
            "id":            v.id,
            "numero_ticket": v.numero_ticket,
            "date_heure":    v.date_heure.isoformat(),
            "montant_total": float(v.montant_total),
            "montant_paye":  float(v.montant_paye),
            "mode_paiement": v.mode_paiement,
            "client_nom":    v.client_nom,
            "lignes": [
                {
                    "produit": (l.produit.nom if l.produit else None) or (l.cuisine_plat.nom if l.cuisine_plat else "?"),
                    "quantite":  float(l.quantite),
                    "prix_unit": float(l.prix_unitaire_applique),
                    "sous_total": float(l.sous_total),
                }
                for l in v.lignes
            ],
        }
        for v in ventes
    ]
    return d


class OuvrirIn(BaseModel):
    caissier_id: int


@router.post("/sessions/ouvrir")
def ouvrir_session(data: OuvrirIn, db: Session = Depends(get_db)):
    """Ouvre (ou retrouve) la session du jour pour une caissière."""
    employe = db.query(Employe).filter_by(id=data.caissier_id).first()
    if not employe:
        raise HTTPException(404, "Caissier introuvable")

    aujourd_hui = datetime.now(tz=timezone.utc).date()
    session = (
        db.query(BarSessionCaisse)
        .filter_by(caissier_id=data.caissier_id, date_session=aujourd_hui)
        .first()
    )
    if not session:
        session = BarSessionCaisse(
            caissier_id  = data.caissier_id,
            date_session = aujourd_hui,
            statut       = "EN_COURS",
        )
        db.add(session)
        db.commit()
        db.refresh(session)

    ventes = _ventes_session(session, db)
    stats  = _stats_ventes(ventes)
    return _session_dict(session, stats)


class NoteIn(BaseModel):
    notes: Optional[str] = None


@router.post("/sessions/{session_id}/soumettre")
def soumettre_session(session_id: int, body: NoteIn = NoteIn(), db: Session = Depends(get_db)):
    """La caissière soumet sa session — passe en SOUMIS."""
    s = _session_ou_404(session_id, db)
    if s.statut == "VALIDE":
        raise HTTPException(400, "Session déjà validée par l'admin.")
    s.statut    = "SOUMIS"
    s.soumis_at = datetime.now(tz=timezone.utc)
    if body.notes:
        s.notes_admin = body.notes
    db.commit()
    ventes = _ventes_session(s, db)
    return _session_dict(s, _stats_ventes(ventes))


class ValiderIn(BaseModel):
    notes: Optional[str] = None


@router.post("/sessions/{session_id}/valider")
def valider_session(session_id: int, body: ValiderIn = ValiderIn(), request: Request = None, db: Session = Depends(get_db)):
    """Admin valide la session — passe en VALIDE."""
    s = _session_ou_404(session_id, db)
    s.statut        = "VALIDE"
    s.valide_at     = datetime.now(tz=timezone.utc)
    s.valide_par_id = _uid(request) if request else None
    if body.notes:
        s.notes_admin = body.notes
    db.commit()
    ventes = _ventes_session(s, db)
    return _session_dict(s, _stats_ventes(ventes))


# ── Exports PDF / XLSX ────────────────────────────────────────────────

def _build_rapport_data(session_id: int, db: Session):
    s      = _session_ou_404(session_id, db)
    ventes = _ventes_session(s, db)
    stats  = _stats_ventes(ventes)
    return s, ventes, stats


@router.get("/sessions/{session_id}/rapport.xlsx")
def export_xlsx(session_id: int, db: Session = Depends(get_db)):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise HTTPException(500, "openpyxl non installé")

    s, ventes, stats = _build_rapport_data(session_id, db)
    caissier_nom     = (s.caissier.nom + " " + s.caissier.prenom) if s.caissier else "Inconnu"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Rapport de caisse"

    # Palette
    ORANGE  = "E8893A"
    DARK    = "1A1A2E"
    LIGHT   = "F5F5F5"
    WHITE   = "FFFFFF"
    BORDER  = Side(style="thin", color="CCCCCC")
    thin    = Border(left=BORDER, right=BORDER, top=BORDER, bottom=BORDER)

    def hdr_cell(ws, row, col, val, bg=ORANGE, fg=WHITE, bold=True, center=True):
        c = ws.cell(row=row, column=col, value=val)
        c.font = Font(bold=bold, color=fg, size=11)
        c.fill = PatternFill("solid", fgColor=bg)
        c.border = thin
        if center:
            c.alignment = Alignment(horizontal="center", vertical="center")
        return c

    def data_cell(ws, row, col, val, bold=False, fmt=None):
        c = ws.cell(row=row, column=col, value=val)
        c.font = Font(bold=bold, size=10)
        c.border = thin
        c.alignment = Alignment(horizontal="center", vertical="center")
        if fmt:
            c.number_format = fmt
        return c

    # Titre
    ws.merge_cells("A1:G1")
    t = ws.cell(row=1, column=1, value=f"Rapport de Caisse — {caissier_nom}")
    t.font = Font(bold=True, size=14, color=WHITE)
    t.fill = PatternFill("solid", fgColor=DARK)
    t.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30

    ws.merge_cells("A2:G2")
    d = ws.cell(row=2, column=1,
                value=f"Date : {s.date_session}  |  Statut : {s.statut}  |  Ventes : {stats['nb_ventes']}  |  Total : G {stats['total']:,.2f}")
    d.font = Font(size=10, color="555555")
    d.alignment = Alignment(horizontal="center")
    ws.row_dimensions[2].height = 22

    # Résumé financier
    r = 4
    hdr_cell(ws, r, 1, "Résumé financier", bg=DARK)
    ws.merge_cells(f"A{r}:B{r}")

    for label, val in [
        ("Total encaissé (G)",   stats["total"]),
        ("Cash / Mixte (G)",     stats["cash"]),
        ("Crédit (G)",           stats["credit"]),
        ("Nombre de ventes",     stats["nb_ventes"]),
    ]:
        r += 1
        ws.cell(row=r, column=1, value=label).font = Font(bold=True)
        ws.cell(row=r, column=1).border = thin
        c = ws.cell(row=r, column=2, value=val)
        c.border = thin
        c.number_format = '#,##0.00' if isinstance(val, float) else '0'

    # Top produits
    r += 2
    hdr_cell(ws, r, 1, "Top produits", bg=DARK)
    ws.merge_cells(f"A{r}:C{r}")
    r += 1
    for lbl, col in [("Produit", 1), ("Qté", 2), ("Total G", 3)]:
        hdr_cell(ws, r, col, lbl)
    for prod in stats["top_produits"]:
        r += 1
        data_cell(ws, r, 1, prod["nom"])
        data_cell(ws, r, 2, prod["quantite"], fmt="0.##")
        data_cell(ws, r, 3, prod["total"],    fmt="#,##0.00")

    # Détail ventes
    r += 2
    headers = ["Ticket", "Heure", "Mode", "Client", "Produits", "Total G", "Payé G"]
    for ci, h in enumerate(headers, 1):
        hdr_cell(ws, r, ci, h)
    for v in ventes:
        r += 1
        produits_str = " / ".join(
            f"{l.produit.nom if l.produit else l.cuisine_plat.nom if l.cuisine_plat else '?'} x{float(l.quantite):.0f}"
            for l in v.lignes
        )
        data_cell(ws, r, 1, v.numero_ticket)
        data_cell(ws, r, 2, v.date_heure.astimezone(timezone.utc).strftime("%H:%M"))
        data_cell(ws, r, 3, v.mode_paiement)
        data_cell(ws, r, 4, v.client_nom or "")
        ws.cell(row=r, column=5, value=produits_str).border = thin
        data_cell(ws, r, 6, float(v.montant_total), fmt="#,##0.00")
        data_cell(ws, r, 7, float(v.montant_paye),  fmt="#,##0.00")

    # Largeurs colonnes
    for col, width in [(1,18),(2,9),(3,10),(4,18),(5,45),(6,14),(7,14)]:
        ws.column_dimensions[get_column_letter(col)].width = width

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"rapport_caisse_{caissier_nom.replace(' ','_')}_{s.date_session}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/sessions/{session_id}/rapport.pdf")
def export_pdf(session_id: int, db: Session = Depends(get_db)):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable,
        )
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
    except ImportError:
        raise HTTPException(500, "reportlab non installé")

    s, ventes, stats = _build_rapport_data(session_id, db)
    caissier_nom     = (s.caissier.nom + " " + s.caissier.prenom) if s.caissier else "Inconnu"

    buf  = io.BytesIO()
    doc  = SimpleDocTemplate(buf, pagesize=A4,
                              leftMargin=1.8*cm, rightMargin=1.8*cm,
                              topMargin=1.8*cm, bottomMargin=1.8*cm)
    styles = getSampleStyleSheet()
    ORANGE = colors.HexColor("#E8893A")
    DARK   = colors.HexColor("#1A1A2E")

    title_style  = ParagraphStyle("title",  fontSize=16, textColor=DARK,   fontName="Helvetica-Bold", spaceAfter=4)
    sub_style    = ParagraphStyle("sub",    fontSize=10, textColor=colors.grey, spaceAfter=12)
    section_style = ParagraphStyle("sect",  fontSize=12, textColor=DARK,   fontName="Helvetica-Bold", spaceBefore=14, spaceAfter=6)

    story = []
    story.append(Paragraph(f"Rapport de Caisse — {caissier_nom}", title_style))
    story.append(Paragraph(
        f"Date : <b>{s.date_session}</b> &nbsp;|&nbsp; Statut : <b>{s.statut}</b>",
        sub_style,
    ))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ORANGE))
    story.append(Spacer(1, 10))

    # Résumé
    story.append(Paragraph("Résumé financier", section_style))
    resume_data = [
        ["Indicateur",         "Valeur"],
        ["Total encaissé",     f"G {stats['total']:,.2f}"],
        ["Cash / Mixte",       f"G {stats['cash']:,.2f}"],
        ["Crédit",             f"G {stats['credit']:,.2f}"],
        ["Nombre de ventes",   str(stats["nb_ventes"])],
    ]
    for mode, val in stats["par_mode"].items():
        resume_data.append([f"Mode {mode}", f"G {val:,.2f}"])

    t_resume = Table(resume_data, colWidths=[8*cm, 5*cm])
    t_resume.setStyle(TableStyle([
        ("BACKGROUND",   (0,0),(1,0), DARK),
        ("TEXTCOLOR",    (0,0),(1,0), colors.white),
        ("FONTNAME",     (0,0),(1,0), "Helvetica-Bold"),
        ("FONTSIZE",     (0,0),(-1,-1), 9),
        ("ROWBACKGROUNDS",(0,1),(-1,-1), [colors.HexColor("#F5F5F5"), colors.white]),
        ("GRID",         (0,0),(-1,-1), 0.4, colors.HexColor("#CCCCCC")),
        ("ALIGN",        (1,0),(1,-1), "RIGHT"),
        ("BOTTOMPADDING",(0,0),(-1,-1), 5),
        ("TOPPADDING",   (0,0),(-1,-1), 5),
    ]))
    story.append(t_resume)
    story.append(Spacer(1, 10))

    # Top produits
    if stats["top_produits"]:
        story.append(Paragraph("Top produits", section_style))
        prod_data = [["Produit", "Qté", "Total G"]]
        for p in stats["top_produits"]:
            prod_data.append([p["nom"], f"{p['quantite']:.1f}", f"G {p['total']:,.2f}"])
        t_prod = Table(prod_data, colWidths=[9*cm, 3*cm, 4*cm])
        t_prod.setStyle(TableStyle([
            ("BACKGROUND",   (0,0),(-1,0), ORANGE),
            ("TEXTCOLOR",    (0,0),(-1,0), colors.white),
            ("FONTNAME",     (0,0),(-1,0), "Helvetica-Bold"),
            ("FONTSIZE",     (0,0),(-1,-1), 9),
            ("ROWBACKGROUNDS",(0,1),(-1,-1), [colors.HexColor("#FFF8F0"), colors.white]),
            ("GRID",         (0,0),(-1,-1), 0.4, colors.HexColor("#CCCCCC")),
            ("ALIGN",        (1,0),(-1,-1), "CENTER"),
            ("BOTTOMPADDING",(0,0),(-1,-1), 5),
            ("TOPPADDING",   (0,0),(-1,-1), 5),
        ]))
        story.append(t_prod)
        story.append(Spacer(1, 10))

    # Détail ventes
    story.append(Paragraph("Détail des ventes", section_style))
    vente_data = [["Ticket", "Heure", "Mode", "Client", "Total G", "Payé G"]]
    for v in ventes:
        vente_data.append([
            v.numero_ticket,
            v.date_heure.astimezone(timezone.utc).strftime("%H:%M"),
            v.mode_paiement,
            (v.client_nom or "")[:20],
            f"G {float(v.montant_total):,.2f}",
            f"G {float(v.montant_paye):,.2f}",
        ])
    t_ventes = Table(vente_data, colWidths=[3.5*cm, 2*cm, 2.5*cm, 3.5*cm, 3.5*cm, 3.5*cm])
    t_ventes.setStyle(TableStyle([
        ("BACKGROUND",   (0,0),(-1,0), DARK),
        ("TEXTCOLOR",    (0,0),(-1,0), colors.white),
        ("FONTNAME",     (0,0),(-1,0), "Helvetica-Bold"),
        ("FONTSIZE",     (0,0),(-1,-1), 8),
        ("ROWBACKGROUNDS",(0,1),(-1,-1), [colors.HexColor("#F5F5F5"), colors.white]),
        ("GRID",         (0,0),(-1,-1), 0.3, colors.HexColor("#DDDDDD")),
        ("ALIGN",        (1,0),(-1,-1), "CENTER"),
        ("BOTTOMPADDING",(0,0),(-1,-1), 4),
        ("TOPPADDING",   (0,0),(-1,-1), 4),
    ]))
    story.append(t_ventes)

    # Footer
    story.append(Spacer(1, 16))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    story.append(Paragraph(
        f"Généré le {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC — Konekta · Bon Prix",
        ParagraphStyle("footer", fontSize=7, textColor=colors.grey, alignment=TA_CENTER, spaceBefore=4),
    ))

    doc.build(story)
    buf.seek(0)
    filename = f"rapport_caisse_{caissier_nom.replace(' ','_')}_{s.date_session}.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
