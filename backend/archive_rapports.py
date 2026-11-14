"""
Archive des rapports pompistes — PDF et format texte simple.

Chaque rapport pompiste porte un nom de fichier fixe :
    <Nom_du_pompiste>_<AAAA-MM-JJ>_<Matin|Apres-midi>   (+ _n<id> si besoin)

- PDF du rapport, fabriqué à partir des données du système (index, gallons,
  argent compté, dépenses, écart) : GET .../rapports/{rid}/pdf
- Le même rapport en texte simple (JSON lisible, une clé par information) :
  GET .../rapports/{rid}/texte
- Fiche papier scannée (PDF, photo) jointe au rapport, renommée
  automatiquement au même format : POST/GET .../rapports/{rid}/fiches
- Archive d'une période : GET .../archive?date_debut&date_fin
  (format=json → un fichier texte simple ; format=zip → PDF + JSON de chaque
  rapport, et les fiches jointes).

Tout reste dans l'espace (station) de la requête : chaque station n'archive
que ses propres rapports.
"""
from __future__ import annotations

import base64
import io
import json
import re
import unicodedata
import zipfile
from datetime import date as date_type
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from database import espace_courant, get_db
from models import PieceJointe, RapportPompiste, Utilisateur
from pompiste_routes import _rapport_dict, _require_superviseur

router = APIRouter(prefix="/api/pompiste-supervision", tags=["archive-rapports"])

TYPE_FICHE = "rapport_pompiste"
_MIME_FICHE = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png"}
_TAILLE_MAX = 8 * 1024 * 1024
_STATUTS = {"EN_COURS": "En cours", "SOUMIS": "À vérifier", "PROBLEME": "Problème", "CONFIRME": "Confirmé",
            "RESOLU": "Résolu", "REJETE": "Renvoyé au pompiste"}
_PERIODES = {"Matin": "Matin", "Apres-midi": "Après-midi"}


# ── Nom de fichier ───────────────────────────────────────────────────

def _propre(txt: str) -> str:
    t = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode("ascii")
    t = re.sub(r"[^A-Za-z0-9]+", "_", t).strip("_")
    return t or "Pompiste"


def nom_base(r: RapportPompiste) -> str:
    u = r.utilisateur
    nom = (u.nom_complet or u.username) if u else "Pompiste"
    return f"{_propre(nom)}_{r.date.isoformat()}_{r.periode}"


def _nom_station() -> str:
    sch = espace_courant()
    try:
        from espaces_routes import nom_station
        return nom_station(int(sch.split("_")[1]) if sch else 0) or ""
    except Exception:
        return ""


def _rapport(db: Session, rid: int) -> RapportPompiste:
    r = db.get(RapportPompiste, rid)
    if not r:
        raise HTTPException(404, "Rapport introuvable.")
    return r


# ── Format texte simple ──────────────────────────────────────────────

def texte_simple(db: Session, r: RapportPompiste) -> dict:
    """Le rapport à plat, avec des clés en clair : facile à relire par une
    personne comme par un programme."""
    d = _rapport_dict(db, r)
    return {
        "fichier": nom_base(r),
        "station": _nom_station(),
        "rapport_numero": r.id,
        "pompiste": d["pompiste_nom"],
        "date": d["date"],
        "periode": _PERIODES.get(r.periode, r.periode),
        "statut": _STATUTS.get(r.statut, r.statut),
        "supprime": bool(d["supprime"]),
        "heures": (r.feuille or {}).get("heures"),
        "meeters": [{
            "carburant": x["produit_nom"], "pompe": x["pompe_nom"],
            "index_debut": x["metter_avant_corrige"] if x["metter_avant_corrige"] is not None else x["metter_avant"],
            "index_fin": x["metter_apres_corrige"] if x["metter_apres_corrige"] is not None else x["metter_apres"],
            "index_debut_saisi_par_pompiste": x["metter_avant"],
            "index_fin_saisi_par_pompiste": x["metter_apres"],
            "corrige_par_la_direction": x["metter_avant_corrige"] is not None or x["metter_apres_corrige"] is not None,
            "gallons": x["gallons"], "prix_gallon": x["prix_gallon"], "montant": x["montant"],
            "anomalies": (x.get("rapprochement") or {}).get("anomalies") or [],
        } for x in d["lignes"]],
        "total_gallons": d["total_gallons"],
        "ventes_attendues": d["montant_attendu_final"],
        "argent_compte": d["montant_compte"],
        "billetterie": d["billetterie"],
        "monnaie": d["monnaie"],
        "devises": d["devises"],
        "depenses": [{"description": x.get("description"), "montant": x.get("montant"),
                      "autorise_par": x.get("autorise_par"), "statut": x.get("statut")} for x in d["depenses"]],
        "total_depenses_retenues": d["total_depenses_retenues"],
        "bons_cheques": d["totaux_feuille"]["bons"],
        "credits": d["totaux_feuille"]["credits"],
        "fonds_caisse": d["totaux_feuille"]["fonds_caisse"],
        "ecart": d["ecart_final"],
        "note_pompiste": d["note_pompiste"],
        "commentaire_direction": d["commentaire_superviseur"],
        "traite_par": d["traite_par_nom"],
        "soumis_le": d["soumis_at"],
    }


# ── PDF ──────────────────────────────────────────────────────────────

def _g(v) -> str:
    return f"{float(v or 0):,.2f} G".replace(",", " ")


def pdf_rapport(db: Session, r: RapportPompiste) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    t = texte_simple(db, r)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
                            topMargin=1.4 * cm, bottomMargin=1.4 * cm, title=t["fichier"])
    st = getSampleStyleSheet()
    esc = lambda s: (str(s if s is not None else "—")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    grille = TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef5")),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ])
    el = [
        Paragraph(f"<b>Rapport pompiste n°{t['rapport_numero']}</b>", st["Title"]),
        Paragraph(esc(t["station"]), st["Normal"]),
        Spacer(1, 6),
    ]
    entete = [["Pompiste", t["pompiste"]], ["Date", r.date.strftime("%d/%m/%Y")], ["Période", t["periode"]],
              ["Heures", t["heures"] or "—"], ["Statut", t["statut"] + (" (supprimé)" if t["supprime"] else "")]]
    tb = Table([[a, esc(b)] for a, b in entete], colWidths=[4 * cm, 13 * cm])
    tb.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 9),
                            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#e8eef5"))]))
    el += [tb, Spacer(1, 10), Paragraph("<b>Meeters</b>", st["Heading3"])]

    rows = [["Carburant", "Pompe", "Index début", "Index fin", "Gallons", "Prix", "Montant"]]
    for m in t["meeters"]:
        rows.append([m["carburant"], m["pompe"], f"{m['index_debut']:.3f}", f"{m['index_fin']:.3f}",
                     f"{m['gallons']:.3f}", _g(m["prix_gallon"]), _g(m["montant"])])
    rows.append(["Total", "", "", "", f"{t['total_gallons']:.3f}", "", _g(t["ventes_attendues"])])
    tb = Table(rows, repeatRows=1)
    tb.setStyle(grille)
    tb.setStyle(TableStyle([("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"), ("ALIGN", (2, 1), (-1, -1), "RIGHT")]))
    el.append(tb)
    corriges = [m for m in t["meeters"] if m["corrige_par_la_direction"]]
    if corriges:
        el.append(Paragraph("<i>Index corrigés par la direction : "
                            + "; ".join(f"{esc(m['pompe'])} (saisi {m['index_debut_saisi_par_pompiste']:.3f} → "
                                        f"{m['index_fin_saisi_par_pompiste']:.3f})" for m in corriges)
                            + "</i>", st["Normal"]))

    if t["depenses"]:
        el += [Spacer(1, 10), Paragraph("<b>Dépenses payées par le pompiste</b>", st["Heading3"])]
        rows = [["Description", "Autorisé par", "Statut", "Montant"]]
        rows += [[esc(x["description"]), esc(x["autorise_par"]), esc(x["statut"]), _g(x["montant"])] for x in t["depenses"]]
        tb = Table(rows, repeatRows=1)
        tb.setStyle(grille)
        el.append(tb)

    el += [Spacer(1, 10), Paragraph("<b>Argent</b>", st["Heading3"])]
    rows = [["Ventes attendues (gallons × prix)", _g(t["ventes_attendues"])],
            ["Argent compté", _g(t["argent_compte"])],
            ["Dépenses retenues", _g(t["total_depenses_retenues"])],
            ["Bons et chèques", _g(t["bons_cheques"])],
            ["Crédits", _g(t["credits"])],
            ["Crédit en caisse (à rendre)", _g(t["fonds_caisse"])],
            ["Écart", _g(t["ecart"])]]
    tb = Table(rows, colWidths=[9 * cm, 5 * cm])
    tb.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 9),
                            ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                            ("TEXTCOLOR", (1, -1), (1, -1), colors.red if t["ecart"] < 0 else colors.black)]))
    el.append(tb)
    for lib, k in (("Note du pompiste", "note_pompiste"), ("Commentaire de la direction", "commentaire_direction")):
        if t[k]:
            el += [Spacer(1, 6), Paragraph(f"<b>{lib} :</b> {esc(t[k])}", st["Normal"])]
    if t["traite_par"]:
        el += [Spacer(1, 6), Paragraph(f"Traité par : {esc(t['traite_par'])}", st["Normal"])]
    doc.build(el)
    return buf.getvalue()


def _telechargement(contenu: bytes, mime: str, nom: str) -> Response:
    return Response(content=contenu, media_type=mime,
                    headers={"Content-Disposition": f'attachment; filename="{nom}"'})


@router.get("/rapports/{rid}/pdf")
def rapport_pdf(rid: int, _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    r = _rapport(db, rid)
    return _telechargement(pdf_rapport(db, r), "application/pdf", nom_base(r) + ".pdf")


@router.get("/rapports/{rid}/texte")
def rapport_texte(rid: int, _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    r = _rapport(db, rid)
    contenu = json.dumps(texte_simple(db, r), ensure_ascii=False, indent=2).encode("utf-8")
    return _telechargement(contenu, "application/json; charset=utf-8", nom_base(r) + ".json")


# ── Fiches papier jointes ────────────────────────────────────────────

def _fiche_dict(p: PieceJointe) -> dict:
    return {"id": p.id, "nom_fichier": p.nom_fichier, "type_mime": p.type_mime, "taille_octets": p.taille_octets,
            "ajoute_par": (p.uploaded_par.nom_complet or p.uploaded_par.username) if p.uploaded_par else None,
            "created_at": p.created_at.isoformat() if p.created_at else None}


def _fiches(db: Session, rid: int) -> list[PieceJointe]:
    return (db.query(PieceJointe).filter_by(type_entite=TYPE_FICHE, entite_id=rid)
            .order_by(PieceJointe.id).all())


@router.get("/rapports/{rid}/fiches")
def lister_fiches(rid: int, _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    _rapport(db, rid)
    return [_fiche_dict(p) for p in _fiches(db, rid)]


@router.post("/rapports/{rid}/fiches", status_code=201)
async def ajouter_fiche(rid: int, fichier: UploadFile = File(...),
                        s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Fiche papier du pompiste (PDF ou photo), renommée
    <pompiste>_<date>_<période>.<ext> (puis _2, _3… si plusieurs pages)."""
    r = _rapport(db, rid)
    mime = (fichier.content_type or "").lower()
    if mime not in _MIME_FICHE:
        raise HTTPException(400, "Format non supporté : PDF, JPG ou PNG uniquement.")
    contenu = await fichier.read()
    if not contenu:
        raise HTTPException(400, "Fichier vide.")
    if len(contenu) > _TAILLE_MAX:
        raise HTTPException(413, "Fichier trop volumineux (8 Mo au plus).")
    n = len(_fiches(db, rid)) + 1
    nom = nom_base(r) + (f"_{n}" if n > 1 else "") + _MIME_FICHE[mime]
    pj = PieceJointe(type_entite=TYPE_FICHE, entite_id=rid, nom_fichier=nom, type_mime=mime,
                     taille_octets=len(contenu), contenu_base64=base64.b64encode(contenu).decode("ascii"),
                     uploaded_par_id=s.id)
    db.add(pj)
    db.commit()
    db.refresh(pj)
    return _fiche_dict(pj)


@router.get("/rapports/{rid}/fiches/{fid}")
def telecharger_fiche(rid: int, fid: int, _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    p = db.query(PieceJointe).filter_by(id=fid, type_entite=TYPE_FICHE, entite_id=rid).first()
    if not p:
        raise HTTPException(404, "Fiche introuvable.")
    return Response(content=base64.b64decode(p.contenu_base64), media_type=p.type_mime,
                    headers={"Content-Disposition": f'inline; filename="{p.nom_fichier}"'})


@router.delete("/rapports/{rid}/fiches/{fid}")
def supprimer_fiche(rid: int, fid: int, _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    p = db.query(PieceJointe).filter_by(id=fid, type_entite=TYPE_FICHE, entite_id=rid).first()
    if not p:
        raise HTTPException(404, "Fiche introuvable.")
    db.delete(p)
    db.commit()
    return {"ok": True}


# ── Archive d'une période ────────────────────────────────────────────

@router.get("/archive")
def archive(date_debut: date_type, date_fin: date_type, format: str = "json",
            avec_supprimes: bool = False,
            _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    if date_fin < date_debut:
        raise HTTPException(400, "La date de fin est avant la date de début.")
    if (date_fin - date_debut).days > 92:
        raise HTTPException(400, "Trois mois au plus par archive.")
    if format not in ("json", "zip"):
        raise HTTPException(400, "format : « json » ou « zip ».")
    q = (db.query(RapportPompiste).filter(RapportPompiste.date >= date_debut, RapportPompiste.date <= date_fin,
                                          RapportPompiste.statut != "EN_COURS"))
    if not avec_supprimes:
        q = q.filter(RapportPompiste.supprime_at.is_(None))
    rapports = sorted(q.all(), key=lambda r: (r.date, 0 if r.periode == "Matin" else 1, nom_base(r), r.id))
    # Deux rapports du même pompiste sur le même créneau : on ajoute le numéro
    vus, noms = {}, {}
    for r in rapports:
        vus[nom_base(r)] = vus.get(nom_base(r), 0) + 1
    for r in rapports:
        noms[r.id] = nom_base(r) + (f"_n{r.id}" if vus[nom_base(r)] > 1 else "")
    periode = f"{date_debut.isoformat()}_{date_fin.isoformat()}"
    if format == "json":
        corps = {"station": _nom_station(), "du": date_debut.isoformat(), "au": date_fin.isoformat(),
                 "nb_rapports": len(rapports), "rapports": [dict(texte_simple(db, r), fichier=noms[r.id]) for r in rapports]}
        return _telechargement(json.dumps(corps, ensure_ascii=False, indent=2).encode("utf-8"),
                               "application/json; charset=utf-8", f"Rapports_pompistes_{periode}.json")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in rapports:
            n = noms[r.id]
            z.writestr(f"pdf/{n}.pdf", pdf_rapport(db, r))
            z.writestr(f"texte/{n}.json", json.dumps(texte_simple(db, r), ensure_ascii=False, indent=2))
            for p in _fiches(db, r.id):
                z.writestr(f"fiches/{p.nom_fichier}", base64.b64decode(p.contenu_base64))
    return _telechargement(buf.getvalue(), "application/zip", f"Rapports_pompistes_{periode}.zip")
