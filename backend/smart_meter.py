"""SMART METER INTELLIGENCE & RECONCILIATION ENGINE.

Le pompiste choisit parfois la mauvaise pompe alors que l'index qu'il lit est
juste. Pendant la saisie de l'index de début, le moteur compare cet index à
la dernière référence fiable de chaque meeter de sa station et dit à quel
meeter il appartient, avec un niveau de confiance :

  HIGH    l'index reprend exactement la référence (≤ ecart_exact) et le
          volume est dans la limite du meeter ;
  MEDIUM  reprise à un chiffre près (faute de frappe), ou écart ≤ ecart_medium,
          ou reprise exacte mais volume inhabituel ;
  LOW     écart ≤ ecart_low (même carburant) ;
  NONE    rien de fiable → repli : le meeter numériquement le plus proche est
          proposé comme RÉFÉRENCE PROVISOIRE, jamais comme preuve.

Statut de rapprochement d'une ligne (figé à l'envoi) :
  MATCH_CONFIRMED  le meeter choisi est le bon, confiance HIGH ;
  MATCH_REVIEW     un candidat existe mais il faut vérifier (confiance moyenne
                   ou faible, ou un autre meeter correspond mieux) ;
  UNMATCHED        aucune correspondance fiable ;
  INITIAL_READING  le meeter n'a aucun historique ni point de départ.

Le statut de VALIDATION reste celui du rapport (SOUMIS = en attente,
CONFIRME / RESOLU = vérifié, REJETE, corbeille) : un rapport s'enregistre
toujours, mais n'entre dans les totaux officiels qu'après la vérification de
la direction (voir main.releves_comptes). Tous les calculs sont faits ici,
côté serveur, en Decimal ; l'interface ne fait qu'afficher.

Réutilise le moteur de placement (placement_metres.py : faute de frappe,
profil de volume, mémoire des erreurs) et la référence de continuité
(pompiste_routes._index_precedent).
"""
from __future__ import annotations

from datetime import date as date_type
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import (ChangementCompteur, ConfigRapprochement, Pompe, RapportPompiste, RapportPompisteLigne,
                    Releve, Utilisateur)
from tz_utils import now_haiti, today_haiti

router = APIRouter()

NIVEAUX = ("HIGH", "MEDIUM", "LOW", "NONE")
_RANG_NIVEAU = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "NONE": 0}
STATUTS_RAPPROCHEMENT = ("MATCH_CONFIRMED", "MATCH_REVIEW", "UNMATCHED", "INITIAL_READING")
ANOMALIES = {
    "ECART_POSITIF": "Saut : l'index de début est au-dessus de la référence (gallons non rapportés entre-temps ?)",
    "RECUL": "Recul : l'index de début est en dessous de la référence (erreur de saisie, mauvais meeter, compteur changé ?)",
    "VOLUME_ELEVE": "Volume au-dessus de la limite du meeter pour une période",
    "VOLUME_NUL": "Aucun gallon vendu (début = fin)",
    "INITIAL_READING": "Première lecture : aucune référence connue pour ce meeter",
    "COMPTEUR_CHANGE": "Compteur remplacé ou remis à zéro récemment",
    "MAUVAIS_METEER": "L'index correspond mieux à un autre meeter",
}
D0 = Decimal("0")


def dec(x) -> Optional[Decimal]:
    if x is None:
        return None
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError):
        return None


def _f(x: Optional[Decimal], n: int = 3) -> Optional[float]:
    return None if x is None else float(round(x, n))


# ── Configuration ─────────────────────────────────────────────────────
def config(db: Session) -> dict:
    c = db.query(ConfigRapprochement).order_by(ConfigRapprochement.id).first()
    if c is None:
        return {"ecart_exact": Decimal("0.001"), "ecart_medium": Decimal("2"), "ecart_low": Decimal("50"),
                "volume_max_defaut": Decimal("5000"), "volumes_max": {}}
    return {"ecart_exact": dec(c.ecart_exact), "ecart_medium": dec(c.ecart_medium), "ecart_low": dec(c.ecart_low),
            "volume_max_defaut": dec(c.volume_max_defaut), "volumes_max": dict(c.volumes_max or {})}


def volume_max(cfg: dict, pompe_id: int) -> Decimal:
    v = cfg["volumes_max"].get(str(pompe_id))
    return dec(v) if v not in (None, "") else cfg["volume_max_defaut"]


# ── Référence fiable d'un meeter ──────────────────────────────────────
def reference(db: Session, pompe: Pompe, d: date_type, periode: str,
              exclure_rapport_id: Optional[int] = None) -> dict:
    """Dernière clôture servant de référence avant (d, période) : relevé du
    journal (fiable), ligne de rapport en attente qui suit le journal
    (provisoire), ou point de départ du meeter. Rien d'inventé : sans
    historique, `valeur` est None."""
    from pompiste_routes import _RANG, _index_precedent, _nom
    val, src = _index_precedent(db, pompe.id, d, periode, exclure_rapport_id=exclure_rapport_id)
    out = {"valeur": dec(val), "source": src, "fiable": False, "date": None, "periode": None,
           "pompiste": None, "statut": None}
    if val is None:
        return out
    if src and src.startswith("journal"):
        cible = (d, _RANG.get(periode, 9))
        for r in (db.query(Releve).filter(Releve.pompe_id == pompe.id, Releve.date <= d)
                  .order_by(Releve.date.desc()).limit(6).all()):
            if (r.date, _RANG.get(r.periode, 9)) < cible and abs(float(r.metter_apres) - val) < 1e-6:
                out.update(fiable=True, date=str(r.date), periode=r.periode, statut="validé",
                           pompiste=(f"{r.pompiste.prenom} {r.pompiste.nom}".strip() if r.pompiste
                                     else _nom(r.cree_par) if getattr(r, "cree_par", None) else None))
                break
        else:
            out.update(fiable=True, statut="validé")
    elif src and src.startswith("rapport pompiste"):
        out.update(statut="en attente de vérification")
    else:
        out.update(fiable=True, statut="point de départ du meeter")
    return out


def _changement_recent(db: Session, pompe_id: int, d: date_type) -> Optional[ChangementCompteur]:
    return (db.query(ChangementCompteur)
            .filter(ChangementCompteur.pompe_id == pompe_id, ChangementCompteur.date_changement <= d,
                    ChangementCompteur.date_changement >= d - timedelta(days=1))
            .order_by(ChangementCompteur.date_changement.desc()).first())


# ── Niveau de confiance d'un candidat ─────────────────────────────────
def _niveau(ecart: Optional[Decimal], debut: Decimal, ref: Optional[Decimal], vol_ok: bool, cfg: dict) -> str:
    from placement_metres import faute_de_frappe
    if ecart is None:
        return "NONE"
    a = abs(ecart)
    if a <= cfg["ecart_exact"]:
        return "HIGH" if vol_ok else "MEDIUM"
    if faute_de_frappe(float(debut), float(ref)) or a <= cfg["ecart_medium"]:
        return "MEDIUM"
    if a <= cfg["ecart_low"]:
        return "LOW"
    return "NONE"


def _volume_ok(db: Session, pompe: Pompe, d: date_type, vol: Optional[Decimal], cfg: dict, profils: dict) -> bool:
    from placement_metres import _volume_anormal, profil
    if vol is None:
        return True
    if vol > volume_max(cfg, pompe.id):
        return False
    if pompe.id not in profils:
        profils[pompe.id] = profil(db, pompe.id, d)
    return not _volume_anormal(float(vol), profils[pompe.id])


def analyser(db: Session, d: date_type, periode: str, pompe_choisie_id: int, debut, fin=None, *,
             utilisateur: Optional[Utilisateur] = None, exclure_rapport_id: Optional[int] = None,
             pompes_exclues: Optional[set] = None) -> dict:
    """Rapprochement d'un index de début (et de fin, s'il est connu) avec les
    meeters de la station. Retourne le statut, la confiance, le meeter
    recommandé, les candidats classés, le repli « le plus proche » et les
    anomalies du meeter choisi."""
    from placement_metres import _personne, activite, famille, habitudes, libelle
    from pompiste_routes import de_ma_station
    cfg = config(db)
    debut, fin = dec(debut), dec(fin)
    if debut is None or debut < 0:
        raise HTTPException(400, "Index de début invalide.")
    choisie = db.get(Pompe, pompe_choisie_id)
    if not choisie or not choisie.actif:
        raise HTTPException(404, "Meeter introuvable ou inactif.")
    if utilisateur is not None and not de_ma_station(utilisateur, choisie):
        raise HTTPException(403, "Ce meeter n'est pas dans votre station.")
    vol = (fin - debut) if fin is not None else None
    act = activite(choisie)
    # Candidats : meeters actifs de la station, même activité (le propane ne
    # se rapproche jamais du carburant), pas déjà pris sur ce créneau.
    exclues = set(pompes_exclues or set()) - {choisie.id}
    cands = [p for p in db.query(Pompe).filter(Pompe.actif.is_(True)).all()
             if activite(p) == act and p.id not in exclues
             and (utilisateur is None or de_ma_station(utilisateur, p))]
    uids, eids = _personne(db, utilisateur_id=utilisateur.id if utilisateur else None)
    hab = habitudes(db, choisie.id, uids, eids)
    profils, evals = {}, []
    for p in cands:
        ref = reference(db, p, d, periode, exclure_rapport_id)
        ecart = (debut - ref["valeur"]) if ref["valeur"] is not None else None
        vol_ok = _volume_ok(db, p, d, vol, cfg, profils)
        niv = _niveau(ecart, debut, ref["valeur"], vol_ok, cfg)
        autre_carb = famille(p) != famille(choisie)
        if autre_carb and niv != "HIGH":
            niv = "NONE"            # changer de carburant seulement sur reprise exacte
        perso, tous = hab.get(p.id, (0, 0))
        evals.append({"pompe_id": p.id, "libelle": libelle(p), "choisi": p.id == choisie.id,
                      "dernier_meter": _f(ref["valeur"]), "source": ref["source"], "reference_fiable": ref["fiable"],
                      "reference_date": ref["date"], "reference_periode": ref["periode"],
                      "reference_pompiste": ref["pompiste"], "reference_statut": ref["statut"],
                      "ecart": _f(ecart), "_ecart": ecart, "niveau": niv, "autre_carburant": autre_carb,
                      "volume_ok": vol_ok, "_hab": perso * 10 + tous})
    evals.sort(key=lambda e: (-_RANG_NIVEAU[e["niveau"]], not e["choisi"], -e["_hab"],
                              abs(e["_ecart"]) if e["_ecart"] is not None else Decimal("1e18")))
    ch = next(e for e in evals if e["choisi"])
    best = evals[0]
    plus_proche = None
    if best["niveau"] == "NONE":
        avec_ref = [e for e in evals if e["_ecart"] is not None and not e["autre_carburant"]]
        if avec_ref:
            pp = min(avec_ref, key=lambda e: abs(e["_ecart"]))
            plus_proche = {k: pp[k] for k in ("pompe_id", "libelle", "dernier_meter", "ecart", "source")}
    # Statut et recommandation
    recommande = None
    if ch["dernier_meter"] is None and (best is ch or best["niveau"] == "NONE"):
        statut, confiance = "INITIAL_READING", "NONE"
    elif best["niveau"] == "NONE":
        statut, confiance = "UNMATCHED", "NONE"
    elif best is ch:
        statut, confiance = ("MATCH_CONFIRMED" if ch["niveau"] == "HIGH" else "MATCH_REVIEW"), ch["niveau"]
    else:
        statut, confiance, recommande = "MATCH_REVIEW", best["niveau"], best
    # Anomalies du meeter choisi
    anomalies = []
    if ch["_ecart"] is None:
        anomalies.append("INITIAL_READING")
    elif ch["_ecart"] > cfg["ecart_exact"]:
        anomalies.append("ECART_POSITIF")
    elif ch["_ecart"] < -cfg["ecart_exact"]:
        anomalies.append("RECUL")
    if vol is not None and vol > volume_max(cfg, choisie.id):
        anomalies.append("VOLUME_ELEVE")
    if vol is not None and vol == 0:
        anomalies.append("VOLUME_NUL")
    if _changement_recent(db, choisie.id, d):
        anomalies.append("COMPTEUR_CHANGE")
    if recommande is not None:
        anomalies.append("MAUVAIS_METEER")
    proches = [e for e in evals if e["niveau"] != "NONE"][:5]
    nettoie = lambda e: {k: v for k, v in e.items() if not k.startswith("_")}
    return {
        "date": str(d), "periode": periode,
        "statut_rapprochement": statut, "confiance": confiance,
        "choisi": nettoie(ch),
        "recommande": nettoie(recommande) if recommande else None,
        "candidats": [nettoie(e) for e in proches],
        "ambigu": sum(1 for e in proches if e["niveau"] == best["niveau"]) > 1 and best["niveau"] != "NONE",
        "plus_proche": plus_proche,
        "anomalies": [{"code": a, "message": ANOMALIES[a]} for a in anomalies],
        "volume": _f(vol), "volume_max": _f(volume_max(cfg, choisie.id), 2),
        "message": _message(statut, ch, recommande, plus_proche),
    }


def _message(statut, ch, rec, pp) -> str:
    if statut == "MATCH_CONFIRMED":
        return f"Index conforme : il reprend le dernier meeter de {ch['libelle']}."
    if statut == "INITIAL_READING":
        return "Première lecture de ce meeter : aucune référence. Le rapport sera vérifié par la direction."
    if statut == "UNMATCHED":
        if pp:
            return (f"Aucune correspondance fiable détectée. Le système propose {pp['libelle']} comme référence "
                    "numérique la plus proche. Une vérification est nécessaire.")
        return "Aucune correspondance fiable détectée. Une vérification est nécessaire."
    if rec:
        return (f"Cet index correspond mieux à {rec['libelle']} (confiance {rec['niveau']}) "
                f"qu'au meeter choisi ({ch['libelle']}). Confirmez le bon meeter.")
    return f"Index proche du dernier meeter de {ch['libelle']} (confiance {ch['niveau']}) : à vérifier."


def enregistrer_sur_ligne(ligne: RapportPompisteLigne, analyse: dict, decision: Optional[str]) -> None:
    """Fige le résultat du moteur sur la ligne au moment de l'envoi."""
    ch = analyse["choisi"]
    rec = analyse.get("recommande") or analyse.get("plus_proche")
    ligne.statut_rapprochement = analyse["statut_rapprochement"]
    ligne.confiance = analyse["confiance"]
    ligne.score_rapprochement = _RANG_NIVEAU.get(analyse["confiance"], 0)
    ligne.pompe_proposee_id = rec["pompe_id"] if rec else None
    ligne.index_reference = dec(ch["dernier_meter"])
    ligne.source_reference = (ch["source"] or "")[:200] or None
    ligne.ecart_reference = dec(ch["ecart"])
    ligne.decision_pompiste = decision if decision in ("ACCEPTE", "REFUSE") else "AUCUNE"
    ligne.anomalies = [a["code"] for a in analyse["anomalies"]]


# ── API pompiste ──────────────────────────────────────────────────────
class AnalyseIn(BaseModel):
    date: Optional[date_type] = None
    periode: Optional[str] = None
    pompe_id: int
    metter_avant: float = Field(ge=0)
    metter_apres: Optional[float] = Field(default=None, ge=0)


@router.post("/api/pompiste/analyser-index")
def analyser_index(data: AnalyseIn, request: Request, db: Session = Depends(get_db)):
    """Pendant la saisie : à quel meeter appartient cet index ? (pompiste)"""
    from pompiste_routes import (PERIODES, _creneaux_pris, _en_cours, _require_pompiste, _valider_date,
                                 creneau_automatique)
    u = _require_pompiste(request, db)
    rap = _en_cours(db, u)
    d = rap.date if rap else (data.date or today_haiti())
    if not rap:
        _valider_date(d)
    periode = rap.periode if rap else data.periode
    if periode is not None and periode not in PERIODES:
        raise HTTPException(400, "Période invalide")
    if periode is None:
        try:
            periode = creneau_automatique(db, d, [data.pompe_id])
        except HTTPException:
            periode = PERIODES[-1]
    # Meeters déjà rapportés sur ce créneau (autre rapport, ou autre ligne du sien)
    pris = {pid for pid, c in _creneaux_pris(db, d).items() if periode in c}
    if rap:
        pris |= {l.pompe_id for l in rap.lignes}
    return analyser(db, d, periode, data.pompe_id, data.metter_avant, data.metter_apres, utilisateur=u,
                    exclure_rapport_id=rap.id if rap else None, pompes_exclues=pris - {data.pompe_id})


# ── API direction : configuration ─────────────────────────────────────
class ConfigIn(BaseModel):
    ecart_exact: float = Field(gt=0, le=1)
    ecart_medium: float = Field(gt=0, le=1000)
    ecart_low: float = Field(gt=0, le=100000)
    volume_max_defaut: float = Field(gt=0, le=1000000)
    volumes_max: dict = {}


def _superviseur(request: Request, db: Session) -> Utilisateur:
    from pompiste_routes import _require_superviseur
    return _require_superviseur(request, db)


@router.get("/api/smart-meter/config")
def lire_config(request: Request, db: Session = Depends(get_db)):
    _superviseur(request, db)
    c = config(db)
    return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in c.items()}


@router.put("/api/smart-meter/config")
def ecrire_config(data: ConfigIn, request: Request, db: Session = Depends(get_db)):
    import main
    s = _superviseur(request, db)
    if not (data.ecart_exact <= data.ecart_medium <= data.ecart_low):
        raise HTTPException(400, "Les seuils doivent aller croissant : exact ≤ moyen ≤ faible.")
    vm = {}
    for k, v in (data.volumes_max or {}).items():
        if v in (None, ""):
            continue
        if not db.get(Pompe, int(k)) or float(v) <= 0:
            raise HTTPException(400, f"Limite invalide pour le meeter {k}.")
        vm[str(int(k))] = float(v)
    c = db.query(ConfigRapprochement).order_by(ConfigRapprochement.id).first()
    if c is None:
        c = ConfigRapprochement()
        db.add(c)
    ancien = {"ecart_exact": float(c.ecart_exact or 0.001), "ecart_medium": float(c.ecart_medium or 2),
              "ecart_low": float(c.ecart_low or 50), "volume_max_defaut": float(c.volume_max_defaut or 5000),
              "volumes_max": dict(c.volumes_max or {})} if c.id else None
    c.ecart_exact, c.ecart_medium, c.ecart_low = data.ecart_exact, data.ecart_medium, data.ecart_low
    c.volume_max_defaut, c.volumes_max = data.volume_max_defaut, vm
    c.modifie_par_id, c.modifie_at = s.id, now_haiti()
    main._tracer(db, request, "smart_meter_config", {"avant": ancien, "apres": data.model_dump()})
    db.commit()
    return lire_config(request, db)


# ── API direction : changement de compteur ────────────────────────────
class ChangementIn(BaseModel):
    pompe_id: int
    date_changement: date_type
    ancien_numero: Optional[str] = None
    nouveau_numero: Optional[str] = None
    ancien_index_final: Optional[float] = Field(default=None, ge=0)
    nouvel_index_initial: float = Field(ge=0)
    motif: str
    autorise_par: str


def _chg_dict(c: ChangementCompteur) -> dict:
    from placement_metres import libelle
    return {"id": c.id, "pompe_id": c.pompe_id, "meeter": libelle(c.pompe) if c.pompe else None,
            "date_changement": str(c.date_changement), "ancien_numero": c.ancien_numero,
            "nouveau_numero": c.nouveau_numero, "ancien_index_final": _f(dec(c.ancien_index_final)),
            "nouvel_index_initial": _f(dec(c.nouvel_index_initial)), "motif": c.motif,
            "autorise_par": c.autorise_par, "created_at": c.created_at.isoformat() if c.created_at else None}


@router.get("/api/smart-meter/changements-compteur")
def lister_changements(request: Request, db: Session = Depends(get_db)):
    _superviseur(request, db)
    return [_chg_dict(c) for c in db.query(ChangementCompteur).order_by(ChangementCompteur.date_changement.desc(),
                                                                       ChangementCompteur.id.desc()).all()]


@router.post("/api/smart-meter/changements-compteur", status_code=201)
def creer_changement(data: ChangementIn, request: Request, db: Session = Depends(get_db)):
    """Compteur remplacé ou remis à zéro : le nouvel index initial devient le
    point de départ du meeter à partir de `date_changement` (les relevés
    d'avant ne servent plus à la continuité ; rien n'est effacé)."""
    import main
    s = _superviseur(request, db)
    motif, qui = " ".join((data.motif or "").split()), " ".join((data.autorise_par or "").split())
    if len(motif) < 5:
        raise HTTPException(400, "Indiquez le motif du changement (5 caractères minimum).")
    if len(qui) < 2:
        raise HTTPException(400, "Indiquez qui a autorisé le changement.")
    if data.date_changement > today_haiti():
        raise HTTPException(400, "La date ne peut pas être dans le futur.")
    p = db.get(Pompe, data.pompe_id)
    if not p or not p.actif:
        raise HTTPException(404, "Meeter introuvable ou inactif.")
    # Un relevé déjà au journal à cette date ou après casserait la continuité
    apres = (db.query(Releve).filter(Releve.pompe_id == p.id, Releve.date >= data.date_changement)
             .order_by(Releve.date).first())
    if apres:
        raise HTTPException(409, f"Un relevé existe déjà le {apres.date.strftime('%d/%m/%Y')} sur ce meeter : "
                                 "le changement doit être daté avant le premier relevé du nouveau compteur.")
    c = ChangementCompteur(pompe_id=p.id, date_changement=data.date_changement,
                           ancien_numero=(data.ancien_numero or "").strip()[:80] or None,
                           nouveau_numero=(data.nouveau_numero or "").strip()[:80] or None,
                           ancien_index_final=data.ancien_index_final, nouvel_index_initial=data.nouvel_index_initial,
                           motif=motif[:500], autorise_par=qui[:150], cree_par_id=s.id,
                           ancien_metre_depart=p.metre_depart, ancienne_date_depart=p.date_depart)
    db.add(c)
    p.metre_depart = data.nouvel_index_initial
    p.date_depart = data.date_changement - timedelta(days=1)
    main._tracer(db, request, "compteur_change", {
        "meeter": p.nom, "pompe_id": p.id, "date_changement": str(data.date_changement),
        "ancien_numero": c.ancien_numero, "nouveau_numero": c.nouveau_numero,
        "ancien_index_final": data.ancien_index_final, "nouvel_index_initial": data.nouvel_index_initial,
        "motif": motif[:300], "autorise_par": qui,
        "point_de_depart_avant": {"metre": float(c.ancien_metre_depart) if c.ancien_metre_depart is not None else None,
                                  "date": str(c.ancienne_date_depart) if c.ancienne_date_depart else None}})
    db.commit()
    db.refresh(c)
    return _chg_dict(c)


# ── API direction : tableau de réconciliation ─────────────────────────
@router.get("/api/smart-meter/reconciliation")
def reconciliation(request: Request, date_debut: date_type, date_fin: date_type,
                   distributeur_id: Optional[int] = None, pompe_id: Optional[int] = None,
                   pompiste_id: Optional[int] = None, anomalie: Optional[str] = None,
                   statut_rapprochement: Optional[str] = None, statut_validation: Optional[str] = None,
                   db: Session = Depends(get_db)):
    """Lignes de rapports pompistes avec leur rapprochement (figé à la
    saisie), leurs anomalies et leur validation. Filtres combinables.
    `statut_validation` : EN_ATTENTE | VERIFIE | REJETE | SUPPRIME."""
    from placement_metres import libelle
    from pompiste_routes import _nom, _per
    _superviseur(request, db)
    if date_fin < date_debut or (date_fin - date_debut).days > 93:
        raise HTTPException(400, "Période invalide (93 jours au plus).")
    q = (db.query(RapportPompisteLigne, RapportPompiste)
         .join(RapportPompiste, RapportPompisteLigne.rapport_id == RapportPompiste.id)
         .filter(RapportPompiste.date >= date_debut, RapportPompiste.date <= date_fin,
                 RapportPompiste.statut != "EN_COURS"))
    if pompe_id:
        q = q.filter(RapportPompisteLigne.pompe_id == pompe_id)
    if pompiste_id:
        q = q.filter(RapportPompiste.utilisateur_id == pompiste_id)
    if statut_rapprochement:
        q = q.filter(RapportPompisteLigne.statut_rapprochement == statut_rapprochement)
    lignes = []
    for l, r in q.all():
        if distributeur_id and (not l.pompe.face or l.pompe.face.distributeur_id != distributeur_id):
            continue
        anos = list(l.anomalies or [])
        if anomalie and anomalie not in anos:
            continue
        val = ("SUPPRIME" if r.supprime_at else "REJETE" if r.statut == "REJETE"
               else "VERIFIE" if (l.confirmee or r.statut in ("CONFIRME", "RESOLU")) else "EN_ATTENTE")
        if statut_validation and val != statut_validation:
            continue
        av = float(l.metter_avant_corrige if l.metter_avant_corrige is not None else l.metter_avant)
        ap = float(l.metter_apres_corrige if l.metter_apres_corrige is not None else l.metter_apres)
        lignes.append({
            "rapport_id": r.id, "date": str(r.date), "periode": r.periode, "periode_lib": _per(r.periode),
            "pompiste_id": r.utilisateur_id, "pompiste": _nom(r.utilisateur), "statut_rapport": r.statut,
            "pompe_id": l.pompe_id, "meeter": libelle(l.pompe),
            "meeter_saisi": libelle(l.pompe_saisie) if l.pompe_saisie else None,
            "meeter_propose": libelle(l.pompe_proposee) if l.pompe_proposee else None,
            "metter_avant_saisi": float(l.metter_avant), "metter_apres_saisi": float(l.metter_apres),
            "metter_avant": av, "metter_apres": ap, "gallons": round(ap - av, 3),
            "index_reference": _f(dec(l.index_reference)), "source_reference": l.source_reference,
            "ecart_reference": _f(dec(l.ecart_reference)),
            "statut_rapprochement": l.statut_rapprochement or "NON_ANALYSE", "confiance": l.confiance,
            "decision_pompiste": l.decision_pompiste, "anomalies": anos,
            "statut_validation": val,
            "verifie_par": _nom(l.verifie_par) if l.verifie_par else None,
            "verifie_at": l.verifie_at.isoformat() if l.verifie_at else None,
            "note_verification": l.note_verification,
        })
    lignes.sort(key=lambda x: (x["date"], x["periode"] != "Matin", x["meeter"]))
    compte = lambda k, v: sum(1 for x in lignes if x[k] == v)
    return {"lignes": lignes,
            "resume": {"total": len(lignes),
                       **{s: compte("statut_rapprochement", s) for s in STATUTS_RAPPROCHEMENT},
                       "en_attente": compte("statut_validation", "EN_ATTENTE"),
                       "verifie": compte("statut_validation", "VERIFIE"),
                       "rejete": compte("statut_validation", "REJETE"),
                       **{a: sum(1 for x in lignes if a in x["anomalies"]) for a in ANOMALIES}},
            "anomalies": ANOMALIES, "statuts": STATUTS_RAPPROCHEMENT}


class VerifierIn(BaseModel):
    note: str


@router.post("/api/smart-meter/rapports/{rid}/lignes/{pompe_id}/verifier")
def verifier_attribution(rid: int, pompe_id: int, data: VerifierIn, request: Request, db: Session = Depends(get_db)):
    """La direction confirme que ces index appartiennent bien à ce meeter
    (note obligatoire). Le statut de rapprochement saisi reste visible : la
    vérification est notée à part (qui, quand, note)."""
    import main
    s = _superviseur(request, db)
    note = " ".join((data.note or "").split())
    if len(note) < 3:
        raise HTTPException(400, "Ajoutez une note de vérification.")
    l = (db.query(RapportPompisteLigne).filter_by(rapport_id=rid, pompe_id=pompe_id).first())
    if not l:
        raise HTTPException(404, "Ligne introuvable.")
    l.verifie_par_id, l.verifie_at, l.note_verification = s.id, now_haiti(), note[:500]
    main._tracer(db, request, "rapprochement_verifie", {"rapport_id": rid, "pompe_id": pompe_id,
                                                        "statut_rapprochement": l.statut_rapprochement,
                                                        "note": note[:300]})
    db.commit()
    return {"ok": True}


@router.get("/api/smart-meter/rapports/{rid}/historique")
def historique(rid: int, request: Request, db: Session = Depends(get_db)):
    """Toutes les interventions tracées sur ce rapport (journal d'audit)."""
    import json as _json

    from models import AuditLog
    _superviseur(request, db)
    out = []
    for a in (db.query(AuditLog).filter(AuditLog.details.like(f'%"rapport_id": {int(rid)},%')
                                        | AuditLog.details.like(f'%"rapport_id": {int(rid)}}}%'))
              .order_by(AuditLog.id).all()):
        try:
            det = _json.loads(a.details or "{}")
        except ValueError:
            det = {"texte": a.details}
        u = db.get(Utilisateur, a.user_id) if a.user_id else None
        out.append({"id": a.id, "action": a.action, "par": (u.nom_complet or u.username) if u else None,
                    "le": a.created_at.isoformat() if getattr(a, "created_at", None) else None, "details": det})
    return out
