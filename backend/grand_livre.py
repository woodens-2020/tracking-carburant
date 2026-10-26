"""
Journal de la caisse principale (grand livre de caisse, à la QuickBooks).

Chaque mouvement d'argent du système devient une ligne datée, au débit
(argent qui entre) ou au crédit (argent qui sort) du compte « Caisse
principale », avec un solde cumulé :

  Débit  : ventes carburant (relevés validés du journal des compteurs),
           entrées des sources (Zo Vincent, Champin…), renflouements,
           ventes Bar / Cuisine / Hôtel encaissées
  Crédit : dépenses, transferts (dépôt en banque…), achats (station, bar,
           cuisine), dépenses Cuisine / Hôtel, salaires payés, paiements Bar

Mêmes règles que la synthèse cash de l'institution
(main._synthese_cash_institution) — les totaux du journal sont contrôlés
contre elle — plus les entrées de la caisse principale. Rien n'est saisi
ici : le journal se construit à partir des modules, il ne peut donc rien
oublier ni compter deux fois. Une entrée annulée reste visible (barrée)
mais ne compte pas.
"""
from __future__ import annotations

from datetime import date as date_type, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from tz_utils import HAITI_TZ, bounds_haiti, today_haiti

PERIODES = {"Matin": 0, "Apres-midi": 1}

# type → (libellé, sens)  — sens « D » = débit (entrée), « C » = crédit (sortie)
TYPES = {
    "vente_carburant": ("Ventes carburant", "D"),
    "entree_source":   ("Entrée caisse principale", "D"),
    "renflouement":    ("Renflouement", "D"),
    "vente_bar":       ("Ventes Bar", "D"),
    "credit_bar":      ("Remboursement crédit Bar", "D"),
    "vente_cuisine":   ("Ventes Cuisine", "D"),
    "vente_hotel":     ("Hôtel", "D"),
    "depense":         ("Dépense", "C"),
    "transfert":       ("Transfert / dépôt", "C"),
    "achat":           ("Achat", "C"),
    "achat_bar":       ("Achat Bar", "C"),
    "achat_cuisine":   ("Achat Cuisine", "C"),
    "depense_cuisine": ("Dépense Cuisine", "C"),
    "depense_hotel":   ("Dépense Hôtel", "C"),
    "salaire":         ("Salaire", "C"),
    "paiement_bar":    ("Paiement employé Bar", "C"),
}


def _jour(v) -> Optional[date_type]:
    """Date (heure d'Haïti) d'une date ou d'un horodatage."""
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(HAITI_TZ).date()
    return v


def _iso(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime) and v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.isoformat()


def _nom(u) -> Optional[str]:
    return (getattr(u, "nom_complet", None) or getattr(u, "username", None)) if u else None


def _ligne(cle, jour, type_, montant, *, compte, description, tiers=None, reference=None,
           module="Station", saisi_par=None, saisi_le=None, annulee=False, motif_annulation=None,
           lien=None, produit_id=None, detail=None) -> dict:
    lib, sens = TYPES[type_]
    m = round(float(montant or 0), 2)
    return {
        "cle": cle, "date": str(jour), "type": type_, "type_lib": lib, "sens": sens,
        "compte": compte, "description": description, "tiers": tiers, "reference": reference,
        "module": module, "saisi_par": saisi_par, "saisi_le": _iso(saisi_le),
        "debit": m if sens == "D" else 0.0, "credit": m if sens == "C" else 0.0,
        "annulee": bool(annulee), "motif_annulation": motif_annulation,
        "lien": lien, "produit_id": produit_id, "detail": detail,
    }


def construire_lignes(db: Session, debut: Optional[date_type], fin: Optional[date_type]) -> list:
    """Toutes les lignes du journal entre deux dates (bornes incluses, None =
    sans limite), dans l'ordre : date, entrées avant sorties, type, clé."""
    from models import (
        Achat, BarAchat, BarPaiementEmploye, BarRemboursement, BarVente, CuisineAchat,
        CuisineDepense, CuisineVente, Depense, EntreeCaisse, FichePaie, HotelDepense,
        HotelReservation, Releve, RenflouementCaisse,
    )
    from main import _est_categorie_transfert

    dt_debut = bounds_haiti(debut)[0] if debut else None
    dt_fin = bounds_haiti(fin)[1] if fin else None

    def per_date(q, col):
        if debut: q = q.filter(col >= debut)
        if fin:   q = q.filter(col <= fin)
        return q

    def per_dt(q, col):
        if dt_debut: q = q.filter(col >= dt_debut)
        if dt_fin:   q = q.filter(col <= dt_fin)
        return q

    out = []

    # ── Ventes carburant : une ligne par jour, période et produit ──────
    groupes: dict = {}
    for r in per_date(db.query(Releve), Releve.date).all():
        produit = r.pompe.produit if r.pompe else None
        k = (r.date, r.periode, produit.id if produit else 0)
        g = groupes.setdefault(k, {"produit": produit, "montant": 0.0, "gallons": 0.0, "pompes": set(),
                                   "saisi": set(), "saisi_le": None, "ids": [], "pompiste": 0})
        g["montant"] += float(r.montant_vente)
        g["gallons"] += float(r.quantite)
        g["pompes"].add(r.pompe.nom if r.pompe else str(r.pompe_id))
        if r.cree_par:
            g["saisi"].add(_nom(r.cree_par))
        if r.rapport_pompiste_id:
            g["pompiste"] += 1
        if r.created_at and (g["saisi_le"] is None or r.created_at > g["saisi_le"]):
            g["saisi_le"] = r.created_at
        g["ids"].append(r.id)
    for (jour, periode, pid), g in groupes.items():
        nom = g["produit"].nom if g["produit"] else "Carburant"
        per = "Après-midi" if periode == "Apres-midi" else periode
        out.append(_ligne(
            f"VTE-{jour}-{periode}-{pid}", jour, "vente_carburant", g["montant"],
            compte=f"Ventes {nom}",
            description=f"Ventes {nom} — {per} · {len(g['pompes'])} pompe{'s' if len(g['pompes']) > 1 else ''} · "
                        f"{g['gallons']:,.3f} gal".replace(",", " "),
            module="Station", saisi_par=", ".join(sorted(x for x in g["saisi"] if x)) or None,
            saisi_le=g["saisi_le"], lien="journal", produit_id=pid or None,
            detail={"periode": periode, "gallons": round(g["gallons"], 3), "pompes": sorted(g["pompes"]),
                    "nb_releves": len(g["ids"]), "dont_pompistes": g["pompiste"]}))

    # ── Entrées des sources de la caisse principale ─────────────────────
    for e in per_date(db.query(EntreeCaisse), EntreeCaisse.date_entree).all():
        src = e.source.nom if e.source else "Source"
        out.append(_ligne(
            f"ENT-{e.id}", e.date_entree, "entree_source", e.montant, compte=f"Source · {src}",
            description=e.note or f"Entrée {src}", tiers=src, reference=e.reference,
            module="Caisse principale", saisi_par=_nom(e.saisi_par), saisi_le=e.created_at,
            annulee=e.annulee, motif_annulation=e.motif_annulation, lien="caisse-principale"))

    for r in per_dt(db.query(RenflouementCaisse), RenflouementCaisse.date_renflouement).all():
        out.append(_ligne(
            f"REN-{r.id}", _jour(r.date_renflouement), "renflouement", r.montant,
            compte="Renflouements", description=r.notes or "Renflouement de la caisse",
            tiers=r.source, module="Grande caisse", saisi_par=_nom(r.enregistre_par),
            saisi_le=r.date_renflouement, lien="caisse"))

    # ── Bar / Cuisine / Hôtel (seulement le cash réellement encaissé) ───
    bar: dict = {}
    for v in per_dt(db.query(BarVente).filter(BarVente.statut != "ANNULEE"), BarVente.date_heure).all():
        if v.mode_paiement in ("CASH", "MIXTE"):
            g = bar.setdefault(_jour(v.date_heure), [0.0, 0, None])
            g[0] += float(v.montant_paye or 0)
            g[1] += 1
            g[2] = max(filter(None, [g[2], v.date_heure]), default=None)
    for jour, (m, n, le) in bar.items():
        out.append(_ligne(f"BAR-{jour}", jour, "vente_bar", m, compte="Ventes Bar",
                          description=f"Ventes Bar encaissées · {n} ticket{'s' if n > 1 else ''}",
                          module="Bar", saisi_le=le, lien="pos-ventes"))
    for r in per_dt(db.query(BarRemboursement), BarRemboursement.date_remb).all():
        out.append(_ligne(f"RMB-{r.id}", _jour(r.date_remb), "credit_bar", r.montant, compte="Ventes Bar",
                          description=r.notes or "Remboursement d'un crédit Bar", module="Bar",
                          saisi_le=r.date_remb, lien="pos-credits"))
    cuis: dict = {}
    for v in per_dt(db.query(CuisineVente).filter(CuisineVente.statut == "VALIDEE",
                                                  CuisineVente.mode_paiement == "CASH"),
                    CuisineVente.date_heure).all():
        g = cuis.setdefault(_jour(v.date_heure), [0.0, 0])
        g[0] += float(v.total or 0)
        g[1] += 1
    for jour, (m, n) in cuis.items():
        out.append(_ligne(f"CUI-{jour}", jour, "vente_cuisine", m, compte="Ventes Cuisine",
                          description=f"Ventes Cuisine encaissées · {n} ticket{'s' if n > 1 else ''}",
                          module="Cuisine", lien="cuisine-ventes"))
    for h in per_dt(db.query(HotelReservation), HotelReservation.date_arrivee).all():
        if float(h.montant_paye or 0) > 0:
            out.append(_ligne(f"HOT-{h.id}", _jour(h.date_arrivee), "vente_hotel", h.montant_paye,
                              compte="Hôtel", description=f"Séjour — {h.client_nom or 'client'}",
                              tiers=h.client_nom, module="Hôtel", saisi_le=h.created_at, lien="hotel-sejours"))

    # ── Sorties ─────────────────────────────────────────────────────────
    for d in per_date(db.query(Depense), Depense.date_depense).all():
        transfert = _est_categorie_transfert(db, d.categorie)
        out.append(_ligne(
            f"DEP-{d.id}", d.date_depense, "transfert" if transfert else "depense", d.montant,
            compte=d.categorie, description=d.description, tiers=d.beneficiaire, reference=d.reference,
            module="Station", saisi_par=_nom(d.saisi_par), saisi_le=d.created_at, lien="depenses", produit_id=d.produit_id,
            detail={"caisse": d.produit.nom if d.produit else None, "notes": d.notes}))
    for a in per_date(db.query(Achat), Achat.date_achat).all():
        out.append(_ligne(f"ACH-{a.id}", a.date_achat, "achat", a.montant, compte=a.categorie,
                          description=a.description, tiers=a.fournisseur, reference=a.reference,
                          module="Station", saisi_par=_nom(a.saisi_par), saisi_le=a.created_at, lien="achats"))
    for a in per_dt(db.query(BarAchat).filter(BarAchat.statut == "CONFIRME"), BarAchat.date_achat).all():
        m = float(a.quantite) * float(a.prix_achat_unitaire) + sum(float(x.montant) for x in a.depenses)
        prod = getattr(a.bar_produit, "nom", None) or "marchandise"
        out.append(_ligne(f"ABA-{a.id}", _jour(a.date_achat), "achat_bar", m, compte="Achats Bar",
                          description=f"Achat {prod} ({float(a.quantite):g})", tiers=a.fournisseur,
                          module="Bar", saisi_le=a.date_achat, lien="pos-achats"))
    for a in per_dt(db.query(CuisineAchat), CuisineAchat.date_achat).all():
        out.append(_ligne(f"ACU-{a.id}", _jour(a.date_achat), "achat_cuisine", a.total,
                          compte=a.categorie or "Achats Cuisine", description=a.description or "Achat cuisine",
                          tiers=a.fournisseur, module="Cuisine", saisi_le=a.date_achat, lien="cuisine-achats"))
    for d in per_dt(db.query(CuisineDepense), CuisineDepense.date_depense).all():
        out.append(_ligne(f"DCU-{d.id}", _jour(d.date_depense), "depense_cuisine", d.montant,
                          compte=d.categorie or "Dépenses Cuisine", description=d.description,
                          tiers=d.fournisseur, module="Cuisine", saisi_le=d.date_depense, lien="cuisine-depenses"))
    for d in per_dt(db.query(HotelDepense), HotelDepense.date_depense).all():
        out.append(_ligne(f"DHO-{d.id}", _jour(d.date_depense), "depense_hotel", d.montant,
                          compte=d.categorie or "Dépenses Hôtel", description=d.description,
                          tiers=d.fournisseur, module="Hôtel", saisi_le=d.date_depense, lien="hotel-depenses"))
    for f in per_date(db.query(FichePaie).filter(FichePaie.statut == "paye"), FichePaie.date_paiement).all():
        emp = f.employe
        nom = f"{emp.prenom} {emp.nom}" if emp else "employé"
        out.append(_ligne(f"PAY-{f.id}", f.date_paiement, "salaire", f.net_a_payer, compte="Salaires",
                          description=f"Salaire {nom} ({f.periode_debut:%d/%m} → {f.periode_fin:%d/%m/%Y})",
                          tiers=nom, module="Ressources humaines", saisi_le=f.created_at, lien="payroll"))
    for p in per_dt(db.query(BarPaiementEmploye), BarPaiementEmploye.date_paiement).all():
        emp = p.employe
        nom = (f"{getattr(emp, 'prenom', '')} {getattr(emp, 'nom', '')}".strip() if emp else None) or "employé"
        out.append(_ligne(f"PBA-{p.id}", _jour(p.date_paiement), "paiement_bar", p.montant, compte="Salaires",
                          description=f"Paiement {nom} ({p.type_paiement or 'Bar'})", tiers=nom,
                          module="Bar", saisi_le=p.date_paiement, lien="pos-paie"))

    rang_type = {t: i for i, t in enumerate(TYPES)}
    out.sort(key=lambda l: (l["date"], 0 if l["sens"] == "D" else 1, rang_type[l["type"]],
                            PERIODES.get((l["detail"] or {}).get("periode"), 9) if l["detail"] else 9, l["cle"]))
    return out


def _signe(l: dict) -> float:
    return 0.0 if l["annulee"] else l["debit"] - l["credit"]


def journal(db: Session, debut: date_type, fin: date_type) -> dict:
    """Journal de la période : solde d'ouverture, lignes avec solde cumulé et
    nombre de jours, totaux par jour et par compte, contrôles."""
    auj = today_haiti()
    ouverture = round(sum(_signe(l) for l in construire_lignes(db, None, debut - timedelta(days=1))), 2)
    lignes = construire_lignes(db, debut, fin)

    solde = ouverture
    jours: dict = {}
    comptes: dict = {}
    for l in lignes:
        solde = round(solde + _signe(l), 2)
        l["solde"] = solde
        d = date_type.fromisoformat(l["date"])
        l["jours"] = (auj - d).days
        saisi = _jour(datetime.fromisoformat(l["saisi_le"])) if l["saisi_le"] else None
        l["retard_saisie"] = (saisi - d).days if saisi else None
        j = jours.setdefault(l["date"], {"date": l["date"], "debit": 0.0, "credit": 0.0, "nb": 0, "solde": 0.0})
        if not l["annulee"]:
            j["debit"] += l["debit"]
            j["credit"] += l["credit"]
        j["nb"] += 1
        j["solde"] = solde
        if not l["annulee"]:
            c = comptes.setdefault((l["sens"], l["compte"]), {"compte": l["compte"], "sens": l["sens"],
                                                              "type_lib": l["type_lib"], "debit": 0.0,
                                                              "credit": 0.0, "nb": 0})
            c["debit"] += l["debit"]
            c["credit"] += l["credit"]
            c["nb"] += 1

    valides = [l for l in lignes if not l["annulee"]]
    total_debit = round(sum(l["debit"] for l in valides), 2)
    total_credit = round(sum(l["credit"] for l in valides), 2)
    cloture = round(ouverture + total_debit - total_credit, 2)

    return {
        "date_debut": str(debut), "date_fin": str(fin),
        "solde_ouverture": ouverture, "total_debit": total_debit, "total_credit": total_credit,
        "solde_cloture": cloture, "nb": len(valides), "nb_annulees": len(lignes) - len(valides),
        "lignes": lignes,
        "par_jour": [dict(v, debit=round(v["debit"], 2), credit=round(v["credit"], 2)) for v in jours.values()],
        "par_compte": sorted((dict(v, debit=round(v["debit"], 2), credit=round(v["credit"], 2))
                              for v in comptes.values()),
                             key=lambda c: (c["sens"], -(c["debit"] + c["credit"]))),
        "types": [{"type": t, "lib": lib, "sens": s} for t, (lib, s) in TYPES.items()],
        "controles": controles(db, debut, fin, lignes, total_debit, total_credit),
    }


def controles(db: Session, debut: date_type, fin: date_type, lignes: list,
              total_debit: float, total_credit: float) -> list:
    """Vérifications de la période : chaque contrôle a un niveau (ok, info,
    alerte) et, s'il y a lieu, les lignes ou les jours concernés."""
    from models import EntreeCaisse, FichePaie, Releve, RapportPompiste
    from main import _synthese_cash_institution
    out = []

    # 1. Totaux = synthèse cash de l'institution + entrées des sources
    syn = _synthese_cash_institution(db, debut, fin)
    sources = sum(float(e.montant) for e in db.query(EntreeCaisse).filter(
        EntreeCaisse.date_entree >= debut, EntreeCaisse.date_entree <= fin, EntreeCaisse.annulee.is_(False)).all())
    attendu_d = round(syn["synthese"]["total_entrees"] + sources, 2)
    attendu_c = round(syn["synthese"]["total_sorties"], 2)
    ecart = round(abs(attendu_d - total_debit) + abs(attendu_c - total_credit), 2)
    out.append({"code": "equilibre", "niveau": "ok" if ecart < 0.01 else "alerte",
                "titre": "Totaux rapprochés avec les modules" if ecart < 0.01 else "Écart avec les modules",
                "texte": (f"Entrées {attendu_d:,.2f} G et sorties {attendu_c:,.2f} G : identiques aux "
                          "chiffres des modules (ventes, dépenses, achats, salaires, caisse principale)."
                          if ecart < 0.01 else
                          f"Le journal ne correspond pas aux modules : écart de {ecart:,.2f} G.").replace(",", " ")})

    # 2. Jours sans ventes carburant (rapport non saisi ?)
    jusqu = min(fin, today_haiti())
    avec = {r[0] for r in db.query(Releve.date).filter(Releve.date >= debut, Releve.date <= jusqu).distinct().all()}
    manquants = []
    d = debut
    while d <= jusqu:
        if d not in avec:
            manquants.append(str(d))
        d += timedelta(days=1)
    if db.query(Releve).count():
        out.append({"code": "jours_sans_ventes", "niveau": "alerte" if manquants else "ok",
                    "titre": (f"{len(manquants)} jour{'s' if len(manquants) > 1 else ''} sans ventes carburant"
                              if manquants else "Ventes carburant saisies chaque jour"),
                    "texte": ("Aucun relevé validé ces jours-là : rapport pas encore saisi ou pas encore validé."
                              if manquants else "Chaque jour de la période a ses relevés."),
                    "jours": manquants[:62]})

    # 3. Rapports pompistes en attente : pas encore dans la caisse
    att = (db.query(RapportPompiste).filter(RapportPompiste.statut.in_(("SOUMIS", "PROBLEME")),
                                            RapportPompiste.date >= debut, RapportPompiste.date <= fin).all())
    if att:
        mt = 0.0
        for r in att:
            for l in r.lignes:
                av = float(l.metter_avant_corrige if l.metter_avant_corrige is not None else l.metter_avant)
                ap = float(l.metter_apres_corrige if l.metter_apres_corrige is not None else l.metter_apres)
                mt += (ap - av) * float(l.prix_gallon)
        out.append({"code": "pompistes_attente", "niveau": "info",
                    "titre": f"{len(att)} rapport{'s' if len(att) > 1 else ''} pompiste{'s' if len(att) > 1 else ''} en attente de validation",
                    "texte": f"Environ {mt:,.2f} G de ventes pas encore comptées dans la caisse : elles entreront à la finalisation.".replace(",", " "),
                    "lien": "pompistes-rapports"})

    # 4. Saisies tardives (enregistrées plus de 2 jours après la date)
    tard = [l for l in lignes if (l.get("retard_saisie") or 0) > 2 and not l["annulee"]]
    out.append({"code": "saisies_tardives", "niveau": "alerte" if tard else "ok",
                "titre": (f"{len(tard)} opération{'s' if len(tard) > 1 else ''} enregistrée{'s' if len(tard) > 1 else ''} en retard"
                          if tard else "Opérations enregistrées à temps"),
                "texte": ("Enregistrées plus de 2 jours après leur date : à vérifier." if tard
                          else "Aucune opération enregistrée plus de 2 jours après sa date."),
                "cles": [l["cle"] for l in tard][:200]})

    # 5. Sorties sans référence
    sans_ref = [l for l in lignes if l["sens"] == "C" and not l["reference"] and l["type"] in ("depense", "transfert", "achat")]
    if sans_ref:
        out.append({"code": "sans_reference", "niveau": "info",
                    "titre": f"{len(sans_ref)} sortie{'s' if len(sans_ref) > 1 else ''} sans référence",
                    "texte": "Dépenses, achats ou transferts sans n° de reçu / facture.",
                    "cles": [l["cle"] for l in sans_ref][:200]})

    # 6. Entrées annulées
    ann = [l for l in lignes if l["annulee"]]
    if ann:
        out.append({"code": "annulees", "niveau": "info",
                    "titre": f"{len(ann)} entrée{'s' if len(ann) > 1 else ''} annulée{'s' if len(ann) > 1 else ''}",
                    "texte": "Visibles dans le journal (barrées), jamais comptées.",
                    "cles": [l["cle"] for l in ann]})

    # 7. Solde négatif en fin de journée
    fin_jour = {}
    for l in lignes:
        fin_jour[l["date"]] = l["solde"]
    neg = [j for j, s in fin_jour.items() if s < -0.005]
    out.append({"code": "solde_negatif", "niveau": "alerte" if neg else "ok",
                "titre": (f"Solde négatif {len(neg)} jour{'s' if len(neg) > 1 else ''}" if neg
                          else "Solde jamais négatif"),
                "texte": ("Plus de sorties que d'argent disponible : une entrée manque peut-être." if neg
                          else "La caisse n'est jamais passée sous zéro sur la période."),
                "jours": neg})

    # 8. Salaires préparés mais pas encore payés
    brou = db.query(FichePaie).filter(FichePaie.statut == "brouillon", FichePaie.periode_fin >= debut,
                                      FichePaie.periode_debut <= fin).all()
    if brou:
        out.append({"code": "salaires_engages", "niveau": "info",
                    "titre": f"{len(brou)} fiche{'s' if len(brou) > 1 else ''} de paie pas encore payée{'s' if len(brou) > 1 else ''}",
                    "texte": f"{sum(float(f.net_a_payer) for f in brou):,.2f} G à sortir de la caisse au paiement.".replace(",", " "),
                    "lien": "payroll"})
    return out
