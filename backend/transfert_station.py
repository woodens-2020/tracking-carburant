"""Transfert d'un rapport pompiste vers une autre station.

Un pompiste se trompe parfois de station : son rapport (meeters, argent,
dépenses, fiche) est enregistré dans la mauvaise. La direction le transfère
vers la bonne station, au même jour et à la même période :

1. Aperçu (`GET …/transfert/apercu?espace_id=`) : pour chaque meeter du
   rapport, les meeters du même carburant dans la station cible, classés par
   le moteur Smart Meter (reprise exacte de la dernière fin, à un chiffre
   près, plus proche…), ceux déjà rapportés sur ce créneau étant bloqués.
2. Transfert (`POST …/transfert`) : la direction confirme le meeter cible de
   chaque ligne et donne un motif. Dans la station cible, le rapport est
   recréé « à vérifier » (même jour, même période, même argent, dépenses
   à décider de nouveau, fiche) sous le compte du pompiste dans cette station
   (compte créé inactif s'il n'existait pas : aucun accès donné). Dans la
   station d'origine, ses relevés sortent du journal et ses dépenses des
   Dépenses ; il part à la corbeille, marqué « transféré » (il ne peut plus
   être restauré, pour ne jamais compter deux fois). Les deux stations
   gardent la trace dans leur journal d'audit.

Garde-fous : rapport en cours (pompiste pas fini) ou déjà supprimé refusé ;
propane jamais vers carburant ni gazoline vers diesel ; un meeter cible par
ligne ; meeter cible déjà rapporté sur ce créneau refusé ; l'ensemble est
appliqué d'un bloc (la station cible d'abord, l'origine ensuite, retour
arrière si l'une échoue).
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import definir_espace, espace_courant, get_db, retablir_espace, session_espace
from models import AuditLog, Pompe, RapportPompiste, RapportPompisteLigne, Utilisateur
from tz_utils import now_haiti

router = APIRouter()
_NIV = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "NONE": 0}


def _schema(eid: int) -> Optional[str]:
    return None if not eid else f"esp_{int(eid)}"


def _cible(request: Request, db: Session, espace_id: int) -> dict:
    from pompiste_routes import autres_stations
    st = {x["id"]: x for x in autres_stations(request, db)}.get(espace_id)
    if not st:
        raise HTTPException(403, "Station introuvable, ou réservée au propriétaire.")
    return st


def _source(db: Session, rid: int) -> RapportPompiste:
    rap = db.get(RapportPompiste, rid)
    if not rap:
        raise HTTPException(404, "Rapport introuvable")
    if rap.supprime_at is not None:
        raise HTTPException(409, "Ce rapport est à la corbeille : restaurez-le d'abord.")
    if rap.statut == "EN_COURS":
        raise HTTPException(409, "Le pompiste n'a pas terminé ce rapport : il ne peut pas encore être transféré.")
    return rap


def _origine(db: Session, u: Utilisateur):
    """(schéma, compte d'origine détaché) du pompiste du rapport."""
    if not u.compte_origine:
        return espace_courant(), u
    sch, _, uid = u.compte_origine.partition(":")
    sch = None if sch == "principal" else sch
    with session_espace(sch) as odb:
        o = odb.get(Utilisateur, int(uid))
        if not o:
            raise HTTPException(409, "Compte d'origine du pompiste introuvable.")
        _ = o.role_obj
        odb.expunge(o)
    return sch, o


def _lignes_source(rap: RapportPompiste) -> list:
    from placement_metres import activite, famille, libelle
    from pompiste_routes import _valeurs_finales
    out = []
    for l in rap.lignes:
        av, ap = _valeurs_finales(l)
        out.append({"pompe_id": l.pompe_id, "libelle": libelle(l.pompe), "famille": famille(l.pompe),
                    "activite": activite(l.pompe), "av": av, "ap": ap,
                    "metter_avant": float(l.metter_avant), "metter_apres": float(l.metter_apres),
                    "metter_avant_corrige": float(l.metter_avant_corrige) if l.metter_avant_corrige is not None else None,
                    "metter_apres_corrige": float(l.metter_apres_corrige) if l.metter_apres_corrige is not None else None})
    return out


def _candidats(tdb: Session, rap_date, periode, ligne: dict, pris: set) -> list:
    """Meeters de la station cible pour cette ligne, classés par le moteur."""
    import smart_meter
    from placement_metres import activite, famille, libelle
    cfg = smart_meter.config(tdb)
    debut = smart_meter.dec(ligne["av"])
    out = []
    for p in tdb.query(Pompe).filter(Pompe.actif.is_(True)).all():
        if activite(p) != ligne["activite"] or famille(p) != ligne["famille"]:
            continue                                  # jamais d'autre carburant
        ref = smart_meter.reference(tdb, p, rap_date, periode)
        ecart = (debut - ref["valeur"]) if ref["valeur"] is not None else None
        niv = smart_meter._niveau(ecart, debut, ref["valeur"], True, cfg)
        out.append({"pompe_id": p.id, "libelle": libelle(p), "dernier_meter": smart_meter._f(ref["valeur"]),
                    "source": ref["source"], "ecart": smart_meter._f(ecart), "niveau": niv, "prise": p.id in pris})
    out.sort(key=lambda c: (c["prise"], -_NIV[c["niveau"]], abs(c["ecart"]) if c["ecart"] is not None else 1e18))
    return out


def _dans_cible(sch: Optional[str], fn):
    """Exécute fn(tdb) dans la station cible (session et espace courant)."""
    jeton = definir_espace(sch)
    try:
        with session_espace(sch) as tdb:
            return fn(tdb)
    finally:
        retablir_espace(jeton)


@router.get("/api/pompiste-supervision/rapports/{rid}/transfert/apercu")
def apercu_transfert(rid: int, espace_id: int, request: Request, db: Session = Depends(get_db)):
    from pompiste_routes import _creneaux_pris, _require_superviseur
    _require_superviseur(request, db)
    st = _cible(request, db, espace_id)
    rap = _source(db, rid)
    src = _lignes_source(rap)
    sch = _schema(espace_id)

    def calc(tdb):
        pris = {pid for pid, c in _creneaux_pris(tdb, rap.date).items() if rap.periode in c}
        lignes, bloq = [], []
        for l in src:
            c = _candidats(tdb, rap.date, rap.periode, l, pris)
            libres = [x for x in c if not x["prise"]]
            prop = libres[0]["pompe_id"] if libres else None
            if not c:
                bloq.append(f"{l['libelle']} : aucun meeter de {l['famille']} dans {st['nom']}.")
            elif not libres:
                bloq.append(f"{l['libelle']} : tous les meeters de {l['famille']} de {st['nom']} sont déjà rapportés "
                            "sur ce créneau.")
            lignes.append({**l, "proposition": prop, "candidats": c})
        return lignes, bloq
    lignes, bloq = _dans_cible(sch, calc)
    av = []
    if rap.statut in ("CONFIRME", "RESOLU"):
        av.append("Rapport finalisé : ses relevés sortent du journal de cette station et ses dépenses des Dépenses ; "
                  f"dans {st['nom']}, il sera à vérifier et à finaliser de nouveau.")
    if any(x.get("statut") not in (None, "en_attente") for x in rap.depenses or []):
        av.append("Les dépenses déjà décidées seront à décider de nouveau dans l'autre station.")
    return {"rapport_id": rap.id, "date": str(rap.date), "periode": rap.periode, "station": st,
            "lignes": lignes, "bloquants": bloq, "avertissements": av}


class TransfertIn(BaseModel):
    espace_id: int
    motif: str
    lignes: dict          # {pompe_id source: pompe_id cible}


@router.post("/api/pompiste-supervision/rapports/{rid}/transfert")
def transferer(rid: int, data: TransfertIn, request: Request, db: Session = Depends(get_db)):
    import main
    from models import Depense
    from pompiste_routes import (_creneaux_pris, _nom, _prix, _rapprocher_ligne, _recalculer_attendu,
                                 _require_superviseur, _retirer_du_journal, _totaux_feuille)
    s = _require_superviseur(request, db)
    st = _cible(request, db, data.espace_id)
    rap = _source(db, rid)
    motif = " ".join((data.motif or "").split())
    if len(motif) < 5:
        raise HTTPException(400, "Indiquez pourquoi vous transférez ce rapport (5 caractères minimum).")
    src = _lignes_source(rap)
    try:
        mapping = {int(k): int(v) for k, v in (data.lignes or {}).items()}
    except (TypeError, ValueError):
        raise HTTPException(400, "Choix des meeters invalide.")
    if set(mapping) != {l["pompe_id"] for l in src}:
        raise HTTPException(400, "Choisissez le meeter de la station cible pour chaque ligne du rapport.")
    if len(set(mapping.values())) != len(mapping):
        raise HTTPException(400, "Deux lignes iraient sur le même meeter.")
    sch_o, origine = _origine(db, rap.utilisateur)
    sch = _schema(data.espace_id)
    from pompiste_routes import _nom_principale, _nom_station_courante
    station_origine = _nom_station_courante() or _nom_principale()
    instant = {"statut_avant": rap.statut, "date": str(rap.date), "periode": rap.periode,
               "pompiste": _nom(rap.utilisateur)}

    # 1. Station cible : le rapport y est recréé, à vérifier
    def creer(tdb):
        from placement_metres import activite, famille, libelle
        pris = {pid for pid, c in _creneaux_pris(tdb, rap.date).items() if rap.periode in c}
        cibles = {}
        for l in src:
            p = tdb.get(Pompe, mapping[l["pompe_id"]])
            if not p or not p.actif:
                raise HTTPException(404, f"{l['libelle']} : meeter cible introuvable ou inactif.")
            if activite(p) != l["activite"] or famille(p) != l["famille"]:
                raise HTTPException(400, f"{l['libelle']} : le carburant ne correspond pas ({libelle(p)}).")
            if p.id in pris:
                raise HTTPException(409, f"{libelle(p)} est déjà rapporté sur ce créneau dans {st['nom']}.")
            cibles[l["pompe_id"]] = p
        # Compte du pompiste dans la station cible (créé inactif : aucun accès donné)
        if sch == sch_o:
            u = tdb.get(Utilisateur, origine.id)
        else:
            avant = tdb.query(Utilisateur).filter_by(compte_origine=main.marque_origine(sch_o, origine.id)).first()
            u = main._compte_dans_station(tdb, sch_o, origine, creer=True)
            if avant is None:
                u.actif = False
        if u is None:
            raise HTTPException(409, "Compte du pompiste introuvable dans la station cible.")
        depenses = []
        for x in rap.depenses or []:
            y = {k: v for k, v in dict(x).items() if k not in ("decide_par", "decide_at", "envoyee", "depense_id")}
            y["statut"] = "en_attente"
            depenses.append(y)
        n = RapportPompiste(date=rap.date, periode=rap.periode, utilisateur_id=u.id, statut="SOUMIS",
                            charte_acceptee_at=rap.charte_acceptee_at, billetterie=list(rap.billetterie or []),
                            monnaie=rap.monnaie, montant_compte=rap.montant_compte, note_pompiste=rap.note_pompiste,
                            depenses=depenses, total_depenses=rap.total_depenses, devises=list(rap.devises or []),
                            feuille=rap.feuille, soumis_at=rap.soumis_at)
        tdb.add(n)
        for l in src:
            p = cibles[l["pompe_id"]]
            n.lignes.append(RapportPompisteLigne(pompe_id=p.id, prix_gallon=_prix(tdb, p, rap.date),
                                                 metter_avant=l["metter_avant"], metter_apres=l["metter_apres"],
                                                 metter_avant_corrige=l["metter_avant_corrige"],
                                                 metter_apres_corrige=l["metter_apres_corrige"]))
        tdb.flush()
        _recalculer_attendu(n)
        n.ecart = round(float(n.montant_compte) + float(n.total_depenses or 0) + _totaux_feuille(n)["net"]
                        - float(n.montant_attendu), 2)
        for ligne in n.lignes:
            try:
                _rapprocher_ligne(tdb, n, ligne, u, None, None)    # Smart Meter dans la station cible
            except HTTPException:
                pass                                              # le contrôle n'empêche jamais le transfert
        tdb.add(AuditLog(user_id=None, action="rapport_pompiste_transfere_recu",
                         ip_address=request.client.host if request.client else None,
                         details=json.dumps({"rapport_id": n.id, "depuis": {"station": station_origine, "rapport_id": rap.id},
                                             "par": _nom(s), "motif": motif[:300], **instant,
                                             "meeters": {l["libelle"]: libelle(cibles[l["pompe_id"]]) for l in src}},
                                            ensure_ascii=False)))
        tdb.commit()
        return n.id

    nouveau = _dans_cible(sch, creer)

    # 2. Station d'origine : hors du journal et des Dépenses, à la corbeille, marqué transféré
    try:
        n_rel = _retirer_du_journal(db, rap)
        n_dep = db.query(Depense).filter_by(rapport_pompiste_id=rap.id).delete()
        rap.supprime_at = now_haiti()
        rap.suppression = {"motif": f"Transféré vers {st['nom']} : {motif}"[:300], "par": _nom(s),
                           "statut_avant": rap.statut, "releves_retires": n_rel, "depenses_retirees": n_dep,
                           "transfere_vers": {"espace_id": data.espace_id, "station": st["nom"], "rapport_id": nouveau}}
        main._tracer(db, request, "rapport_pompiste_transfere", {
            "rapport_id": rap.id, **instant, "vers": {"station": st["nom"], "rapport_id": nouveau},
            "motif": motif[:300], "releves_retires": n_rel, "depenses_retirees": n_dep})
        db.commit()
    except Exception:
        db.rollback()
        # Retour arrière dans la station cible : le rapport n'existe qu'à un endroit
        def annuler(tdb):
            x = tdb.get(RapportPompiste, nouveau)
            if x:
                tdb.delete(x)
                tdb.commit()
        _dans_cible(sch, annuler)
        raise
    return {"ok": True, "station": st, "rapport_id": nouveau, "date": str(rap.date), "periode": rap.periode,
            "releves_retires": n_rel, "depenses_retirees": n_dep}
