"""
Assistant IA d'analyse (Rapports IA) — function calling (tool use).

Fournisseurs (priorité) :
  1. Google Gemini   (GEMINI_API_KEY)
  2. Anthropic Claude (ANTHROPIC_API_KEY) — secours

Fiabilité : les chiffres viennent d'outils qui réutilisent les calculs
vérifiés du système (journal de la caisse principale, Intelligence Center,
stock) — l'IA ne recalcule pas l'argent elle-même. Le SQL libre ne reste
qu'en dernier recours, en lecture seule, borné et sans accès aux tables
sensibles.
"""
import os
import re
import json
import time
from datetime import date, timedelta

from sqlalchemy.orm import Session
from sqlalchemy import text as sql_text
from tz_utils import today_haiti
from stats import compute_stats, liste_produits_pompes
from stock_service import (
    stock_restant,
    rentabilite_globale,
    cout_moyen_pondere,
    SEUIL_ALERTE_JOURS_PAR_DEFAUT,
)
from models import Base

MAX_TOOL_ROUNDS = 8
GEMINI_MODELS   = ["gemini-flash-lite-latest", "gemini-flash-latest"]   # rapide et économique d'abord, puis secours
CLAUDE_MODEL    = "claude-sonnet-5-5"
DELAI_IA_S      = 40          # réponse du fournisseur (par tentative ; puis modèle de secours)
DELAI_SQL_S     = 5           # requête SQL de dernier recours
HISTORIQUE_MAX  = 12          # messages gardés dans le contexte
TAILLE_MESSAGE  = 4000        # caractères par message d'historique

# ── SQL de dernier recours : lecture seule, tables sensibles exclues ──
_TABLES_EXCLUES = {
    "roles", "utilisateurs", "sessions", "login_security_events",
    "otp_codes", "admin_codes", "audit_logs", "password_reset_tokens", "oauth_states",
    "chat_conversations", "ia_journal",   # conversations et questions des autres utilisateurs
    "taches", "tache_participants",       # assignations interpersonnelles
    "pieces_jointes",                     # contenu base64 volumineux
    "espaces",                            # registre technique des stations
}
_TABLES_AUTORISEES = sorted(nom for nom in Base.metadata.tables if nom not in _TABLES_EXCLUES)
_MOTS_SQL_INTERDITS = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "grant", "revoke", "attach", "detach", "pragma", "vacuum", "copy",
    "exec", "execute", "call", "merge", "replace", "into", "lock", "listen", "notify",
    "do", "set", "reset", "begin", "commit", "rollback", "savepoint", "prepare", "deallocate",
)
# Fonctions système (lecture de fichiers, attente, arrêt de connexions, réglages…)
_FONCTIONS_INTERDITES = (
    r"\bpg_\w+", r"\bcurrent_setting\b", r"\bset_config\b", r"\bdblink\w*", r"\blo_\w+",
    r"\b(?:nextval|setval|currval|lastval)\b", r"\binformation_schema\b", r"\bquery_to_\w+",
    r"\b\w*_to_xml\w*\b", r"\bxpath\w*\b", r"\binet_\w+", r"\bversion\s*\(", r"\btxid_\w*",
    r"\bsleep\b", r"\bload_extension\b", r"\breadfile\b", r"\bsqlite_\w+", r"\bgenerate_series\b",
)
_LIMITE_LIGNES_REQUETE = 200


def _schema_texte() -> str:
    return "\n".join(f"  - {nom}({', '.join(c.name for c in Base.metadata.tables[nom].columns)})"
                     for nom in _TABLES_AUTORISEES)


def _valider_sql_lecture_seule(sql: str) -> str:
    propre = (sql or "").strip().rstrip(";").strip()
    if not propre:
        raise ValueError("Requête vide.")
    if ";" in propre:
        raise ValueError("Une seule instruction SELECT autorisée (pas de point-virgule).")
    if any(c in propre for c in ('"', "`", "\\", "[")):
        raise ValueError("Guillemets doubles, accents graves, crochets et barres obliques inverses interdits.")
    minuscule = propre.lower()
    if re.search(r"--|/\*", minuscule):
        raise ValueError("Commentaires SQL interdits.")
    if not re.match(r"^\s*(select|with)\b", minuscule):
        raise ValueError("Seules les requêtes SELECT (ou WITH ... SELECT) sont autorisées.")
    for mot in _MOTS_SQL_INTERDITS:
        if re.search(rf"\b{re.escape(mot)}\b", minuscule):
            raise ValueError(f"Mot-clé interdit dans la requête : {mot}")
    for motif in _FONCTIONS_INTERDITES:
        if re.search(motif, minuscule):
            raise ValueError("Fonction système interdite dans la requête.")
    # Table sensible citée n'importe où (jointure par virgule, sous-requête…)
    for t in _TABLES_EXCLUES:
        if re.search(rf"\b{t}\b", minuscule):
            raise ValueError(f"Table non accessible à l'assistant : {t}")
    tables_referencees = set(re.findall(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", minuscule))
    ctes = set(re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s+as\s*\(", minuscule))
    non_autorisees = tables_referencees - set(_TABLES_AUTORISEES) - ctes
    if non_autorisees:
        raise ValueError(
            f"Table(s) non autorisée(s) ou inconnue(s) : {', '.join(sorted(non_autorisees))}. "
            f"Appelle lister_tables pour voir les tables disponibles.")
    return propre


def _executer_requete(db: Session, sql: str) -> dict:
    """SQL de dernier recours, sur une connexion à part : transaction en
    lecture seule, délai maximum, schéma de la station courante."""
    try:
        propre = _valider_sql_lecture_seule(sql)
    except ValueError as e:
        return {"erreur": str(e)}
    from database import engine, espace_courant, _is_postgres
    sch = espace_courant()
    if sch and not _is_postgres:
        return {"erreur": "Requêtes libres indisponibles dans une station séparée sur cette installation."}
    try:
        with engine.connect() as conn:
            if _is_postgres:
                with conn.begin():                       # transaction en lecture seule, délai borné
                    conn.execute(sql_text("SET TRANSACTION READ ONLY"))
                    conn.execute(sql_text(f"SET LOCAL statement_timeout = '{DELAI_SQL_S}s'"))
                    if sch:
                        conn.execute(sql_text(f'SET LOCAL search_path TO "{sch}"'))
                    resultat = conn.execute(sql_text(propre))
                    colonnes = list(resultat.keys())
                    lignes = resultat.fetchmany(_LIMITE_LIGNES_REQUETE + 1)
            else:                                        # SQLite : connexion en lecture seule
                conn.exec_driver_sql("PRAGMA query_only = ON")
                try:
                    resultat = conn.execute(sql_text(propre))
                    colonnes = list(resultat.keys())
                    lignes = resultat.fetchmany(_LIMITE_LIGNES_REQUETE + 1)
                finally:
                    conn.rollback()
                    conn.exec_driver_sql("PRAGMA query_only = OFF")
                    conn.commit()
    except Exception as e:
        message = str(getattr(e, "orig", e)).strip().split("\n")[0]
        if "statement timeout" in message or "canceling statement" in message:
            message = f"requête trop longue (plus de {DELAI_SQL_S} s) — ajoute des filtres ou des agrégats"
        return {"erreur": f"Erreur SQL : {message}"}
    tronque = len(lignes) > _LIMITE_LIGNES_REQUETE
    lignes = lignes[:_LIMITE_LIGNES_REQUETE]
    return {
        "source": "requête SQL libre (lecture seule) — chiffres bruts, hors règles de la caisse",
        "colonnes": colonnes, "lignes": [dict(zip(colonnes, row)) for row in lignes],
        "nb_lignes": len(lignes), "tronque": tronque,
        "avertissement_troncature": (f"Résultat tronqué à {_LIMITE_LIGNES_REQUETE} lignes — affine la requête."
                                     if tronque else None),
    }


# ── Outils (une seule définition pour tous les fournisseurs) ──────────
_P_DATES = {
    "date_debut": {"type": "string", "description": "Date de début AAAA-MM-JJ (incluse)."},
    "date_fin":   {"type": "string", "description": "Date de fin AAAA-MM-JJ (incluse)."},
}
OUTILS = [
    {"name": "analyse_financiere",
     "description": ("SOURCE PRINCIPALE pour l'argent. Synthèse vérifiée d'une période (mêmes calculs que la "
                     "Caisse principale et l'Intelligence Center) : revenu, charges, résultat, marge, encaissements, "
                     "décaissements, flux net, trésorerie (solde d'ouverture et de clôture de la caisse), performance "
                     "par département, charges par catégorie, ventes et gallons PAR PRODUIT, pompistes, stock valorisé, "
                     "évolution, comparaison avec une autre période et constats calculés. À appeler pour toute question "
                     "de chiffre d'affaires, dépenses, bénéfice, trésorerie, comparaison ou rapport."),
     "parameters": {"type": "object", "properties": dict(_P_DATES, **{
         "comparer": {"type": "string", "enum": ["precedent", "annee", "aucune"],
                      "description": "precedent = période de même durée juste avant (défaut) ; annee = même période l'an dernier."},
         "departements": {"type": "string", "description": "Optionnel — départements séparés par | (ex. Station|Bar)."},
         "categories": {"type": "string", "description": "Optionnel — catégories de dépenses séparées par |."},
         "produit_id": {"type": "integer", "description": "Optionnel — un produit carburant."}}),
         "required": ["date_debut", "date_fin"]}},
    {"name": "operations_caisse",
     "description": ("Lignes du journal de la caisse principale (les opérations derrière un chiffre) : ventes, "
                     "dépenses, achats, salaires, transferts, entrées. Pour détailler, vérifier ou lister les plus grosses opérations."),
     "parameters": {"type": "object", "properties": dict(_P_DATES, **{
         "types": {"type": "string", "description": "Optionnel — types séparés par | (vente_carburant, depense, achat, salaire, transfert, entree_source, vente_bar…)."},
         "categories": {"type": "string", "description": "Optionnel — catégories séparées par |."},
         "departements": {"type": "string", "description": "Optionnel — départements séparés par |."},
         "recherche": {"type": "string", "description": "Optionnel — mot cherché dans la description, le tiers ou la catégorie."},
         "tri": {"type": "string", "enum": ["montant", "date"], "description": "Tri (défaut : plus gros montants)."},
         "limite": {"type": "integer", "description": "Nombre de lignes (max 100, défaut 30)."}}),
         "required": ["date_debut", "date_fin"]}},
    {"name": "ventes_carburant",
     "description": ("Ventes de carburant validées (relevés de compteurs) : montant total en gourdes, et gallons + montant "
                     "PAR PRODUIT et PAR POMPE (meter). Les gallons ne sont jamais additionnés entre produits."),
     "parameters": {"type": "object", "properties": dict(_P_DATES, **{
         "produit_id": {"type": "integer", "description": "Optionnel — id produit."}}), "required": ["date_debut", "date_fin"]}},
    {"name": "get_stock",
     "description": "Stock carburant restant en gallons par produit (livraisons − ventes), alerte stock bas, coût moyen pondéré.",
     "parameters": {"type": "object", "properties": {
         "produit_id": {"type": "integer", "description": "Optionnel — un produit."},
         "seuil_jours": {"type": "integer", "description": "Optionnel — alerte si stock < N jours de vente (défaut 7)."}},
         "required": []}},
    {"name": "get_rentabilite",
     "description": "Rentabilité carburant : revenu, coût des ventes (coût moyen pondéré × gallons), bénéfice, marge, par produit.",
     "parameters": {"type": "object", "properties": dict(_P_DATES, **{
         "produit_id": {"type": "integer", "description": "Optionnel — un produit."}}), "required": ["date_debut", "date_fin"]}},
    {"name": "get_livraisons",
     "description": "Dernières livraisons de carburant (max 20), filtrables par produit et dates.",
     "parameters": {"type": "object", "properties": {
         "produit_id": {"type": "integer", "description": "Optionnel."},
         "date_debut": {"type": "string", "description": "Optionnel — AAAA-MM-JJ."},
         "date_fin": {"type": "string", "description": "Optionnel — AAAA-MM-JJ."}}, "required": []}},
    {"name": "lister_tables",
     "description": "Tables et colonnes accessibles en lecture (pour executer_requete).",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "executer_requete",
     "description": ("DERNIER RECOURS : une requête SQL SELECT en lecture seule (5 s max, 200 lignes max) pour une donnée "
                     "qu'aucun autre outil ne fournit (ex. occupation de l'hôtel, articles du bar, liste d'employés). "
                     "Ne l'utilise JAMAIS pour des totaux d'argent : analyse_financiere est la source officielle."),
     "parameters": {"type": "object", "properties": {"sql": {"type": "string", "description": "Requête SQL SELECT."}},
                    "required": ["sql"]}},
]


def _d(v):
    return date.fromisoformat(str(v)[:10]) if v else None


def _run_tool(db: Session, name: str, args: dict) -> dict:
    """Exécute un outil ; toute erreur est rendue à l'IA (jamais d'exception)."""
    args = dict(args or {})
    try:
        if name == "analyse_financiere":
            from intelligence import synthese_ia
            return synthese_ia(db, _d(args.get("date_debut")), _d(args.get("date_fin")), args.get("comparer") or "precedent",
                               modules=args.get("departements"), comptes=args.get("categories"),
                               produits=str(args["produit_id"]) if args.get("produit_id") else None)
        if name == "operations_caisse":
            from intelligence import operations_ia
            return operations_ia(db, _d(args.get("date_debut")), _d(args.get("date_fin")), types=args.get("types"),
                                 comptes=args.get("categories"), modules=args.get("departements"),
                                 recherche=args.get("recherche"), tri=args.get("tri") or "montant", limite=args.get("limite") or 30)
        if name in ("ventes_carburant", "get_stats"):
            s = compute_stats(db, _d(args["date_debut"]), _d(args["date_fin"]), args.get("produit_id"), None)
            return {"source": "relevés de compteurs validés", "date_debut": s["date_debut"], "date_fin": s["date_fin"],
                    "ventes_total_gourdes": s["total_montant"], "nb_releves": s["nb_releves"],
                    "nb_jours_couverts": s["nb_jours_couverts"],
                    "par_produit": s["par_produit"], "par_pompe": s["par_pompe"],
                    "note": "Gallons donnés par produit : ne pas les additionner entre produits."}
        if name == "get_stock":
            from models import Produit
            q = db.query(Produit).filter(Produit.actif.is_(True))
            if args.get("produit_id"):
                q = q.filter(Produit.id == args["produit_id"])
            res = []
            for p in q.all():
                s = stock_restant(db, p.id, seuil_jours=int(args.get("seuil_jours") or SEUIL_ALERTE_JOURS_PAR_DEFAUT))
                s["produit_nom"] = p.nom
                s["cout_moyen_pondere"] = cout_moyen_pondere(db, p.id, uniquement_ouvertes=True)
                res.append(s)
            return {"stocks": res, "nb_alertes": sum(1 for r in res if r["alerte_bas"])}
        if name == "get_rentabilite":
            return rentabilite_globale(db, _d(args["date_debut"]), _d(args["date_fin"]), args.get("produit_id"))
        if name == "get_livraisons":
            from models import Livraison
            q = db.query(Livraison)
            if args.get("produit_id"):
                q = q.filter(Livraison.produit_id == args["produit_id"])
            if args.get("date_debut"):
                q = q.filter(Livraison.date_livraison >= _d(args["date_debut"]))
            if args.get("date_fin"):
                q = q.filter(Livraison.date_livraison <= _d(args["date_fin"]))
            ls = q.order_by(Livraison.date_livraison.desc()).limit(20).all()
            return {"nb": len(ls), "livraisons": [
                {"id": l.id, "produit_nom": l.produit.nom, "date_livraison": str(l.date_livraison),
                 "gallons_recus": round(float(l.gallons_recus), 3), "prix_achat_gallon": round(float(l.prix_achat_gallon), 2),
                 "fournisseur": l.fournisseur} for l in ls]}
        if name == "lister_tables":
            return {"tables": _TABLES_AUTORISEES, "schema": _schema_texte()}
        if name == "executer_requete":
            return _executer_requete(db, args.get("sql", ""))
        return {"erreur": f"Outil inconnu : {name}"}
    except (KeyError, ValueError) as e:
        db.rollback()
        return {"erreur": f"Paramètres invalides ({e}). Dates au format AAAA-MM-JJ."}
    except Exception as e:
        db.rollback()
        detail = getattr(e, "detail", None)
        return {"erreur": str(detail if detail else e)[:300]}


# ── Prompt système : contexte, dictionnaire des indicateurs, règles ───
def _system_prompt(db: Session) -> str:
    from database import espace_courant
    produits = liste_produits_pompes(db)
    ctx_produits = "\n".join(
        f"  - {nom} (id={info['id']}) : {', '.join(info['pompes']) or 'aucune pompe'}" for nom, info in produits.items()
    ) or "  (aucun produit configuré)"
    try:
        from main import _MODULES
        modules = ", ".join(k for k, v in _MODULES.items() if v) or "carburant"
    except Exception:
        modules = "carburant"
    sch = espace_courant()
    from espaces_routes import nom_principale
    station = nom_principale()
    if sch:
        from espaces_routes import schemas_actifs
        station = schemas_actifs().get(sch, sch)
    t = today_haiti()
    lundi = t - timedelta(days=t.weekday())
    mois_prec_fin = t.replace(day=1) - timedelta(days=1)
    return f"""Tu es l'assistant d'analyse de gestion de KONEKTA (Haïti), pour la direction.

CONTEXTE
- Aujourd'hui : {t.isoformat()} · station : {station} · modules actifs : {modules}
- Monnaie : gourde (G). Quantités carburant : gallons (gal).
- Produits et pompes (meters) :
{ctx_produits}

DATES RELATIVES (convertis en AAAA-MM-JJ avant d'appeler un outil)
- aujourd'hui / jodi a / today : {t.isoformat()}
- hier / yè / yesterday : {(t - timedelta(days=1)).isoformat()}
- cette semaine / semèn sa a : du {lundi.isoformat()} au {t.isoformat()}
- ce mois / mwa sa a : du {t.replace(day=1).isoformat()} au {t.isoformat()}
- mois dernier / mwa pase : du {mois_prec_fin.replace(day=1).isoformat()} au {mois_prec_fin.isoformat()}
- 30 derniers jours : du {(t - timedelta(days=29)).isoformat()} au {t.isoformat()}
Sans période précisée : prends le mois en cours ET dis-le dans la réponse.

DICTIONNAIRE DES INDICATEURS (définitions officielles — ne jamais les changer ni les confondre)
- Revenu (chiffre d'affaires) = ventes : carburant (relevés validés), Bar, Cuisine, Hôtel. Ce n'est PAS l'argent en caisse.
- Encaissements = tout l'argent entré dans la caisse principale (ventes + entrées des sources + renflouements + remboursements de crédit).
- Charges = dépenses + achats + salaires (+ dépenses payées par les pompistes sur leurs ventes). Un dépôt en banque / transfert n'est PAS une charge.
- Résultat = revenu − charges (base caisse : un achat de stock compte quand il est payé). Marge = résultat / revenu.
- Décaissements = tout l'argent sorti de la caisse (charges payées + transferts).
- Trésorerie = solde de la caisse principale à une date (solde d'ouverture + encaissements − décaissements).
- Stock carburant = livraisons − gallons vendus, par produit ; valeur = stock × coût moyen pondéré d'achat.
- Les rapports pompistes EN ATTENTE de validation ne comptent dans aucun chiffre. Les opérations annulées non plus.
- Gazoline, Diesel, Propane… : leurs GALLONS ne s'additionnent JAMAIS ; seuls les montants en gourdes s'additionnent.

OUTILS — ORDRE DE PRÉFÉRENCE
1. analyse_financiere : toute question d'argent, de département, de période, de comparaison ou de rapport.
2. operations_caisse : le détail des opérations derrière un chiffre.
3. ventes_carburant, get_stock, get_rentabilite, get_livraisons : questions carburant.
4. executer_requete (avec lister_tables) : SEULEMENT pour une donnée qu'aucun outil ne donne ; jamais pour des totaux d'argent.
Un seul appel d'analyse_financiere suffit pour un rapport complet : n'en fais pas un par indicateur.

RÈGLES ABSOLUES
1. Zéro chiffre inventé : chaque montant, quantité ou solde vient d'un résultat d'outil de CETTE conversation.
2. Si un outil ne renvoie rien ou une erreur, dis-le clairement ; n'extrapole jamais.
3. Lecture seule : tu ne peux rien créer, modifier, supprimer, valider ni payer. Si on te le demande, explique-le.
4. Une variation inhabituelle est un « point à vérifier », jamais une fraude ou une faute.
5. Ne divulgue pas de données personnelles inutiles (téléphones, adresses) ; limite-toi à ce que la question demande.

FORMAT
- Réponds dans la langue de la question : kreyòl ayisyen, français ou anglais.
- Montants : 2 décimales, espace pour les milliers (ex. 12 345,67 G). Gallons : 3 décimales (ex. 98,500 gal).
- Commence par la réponse directe en une ou deux phrases, puis un tableau Markdown des chiffres.
- Termine par une ligne « Source : <outil> · période <du … au …> » et, si utile, « Limites : … ».
- Sépare clairement les chiffres vérifiés de ton interprétation (section « Analyse »).
- Sois concis et factuel.
"""


def _historique_contexte(historique: list) -> list:
    """Derniers échanges seulement, messages tronqués (coût et longueur maîtrisés)."""
    out = []
    for h in (historique or [])[-HISTORIQUE_MAX:]:
        if isinstance(h, dict) and h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str):
            out.append({"role": h["role"], "content": h["content"][:TAILLE_MESSAGE]})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _tracer_outil(trace: list, nom: str, args: dict, debut: float, res: dict) -> None:
    trace.append({"nom": nom, "arguments": args, "duree_ms": int((time.time() - debut) * 1000),
                  "erreur": res.get("erreur") if isinstance(res, dict) else None})


# ── Gemini ────────────────────────────────────────────────────────────
def _chat_gemini(db: Session, message: str, historique: list, api_key: str, trace: dict) -> dict:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=DELAI_IA_S * 1000))
    last_err = None
    for i, model_name in enumerate(GEMINI_MODELS):
        try:
            return _chat_gemini_model(db, message, historique, client, model_name, types, trace)
        except Exception as e:
            err_str = str(e)
            if any(x in err_str for x in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "504", "DEADLINE_EXCEEDED",
                                           "500", "INTERNAL", "timed out", "Timeout")):
                trace["outils"] = []                     # le modèle suivant repart de zéro
                last_err = e
                if i < len(GEMINI_MODELS) - 1:
                    time.sleep(1)
                continue
            raise
    raise Exception(f"Tous les modèles Gemini indisponibles. Dernière erreur : {last_err}")


def _chat_gemini_model(db: Session, message: str, historique: list, client, model_name: str, types, trace: dict) -> dict:
    trace.update(fournisseur="gemini", modele=model_name)
    decl = [types.FunctionDeclaration(name=o["name"], description=o["description"], parameters=o["parameters"]) for o in OUTILS]
    config = types.GenerateContentConfig(system_instruction=_system_prompt(db), tools=[types.Tool(function_declarations=decl)],
                                         temperature=0.1)
    contents = [types.Content(role="user" if h["role"] == "user" else "model", parts=[types.Part(text=h["content"])])
                for h in historique]
    contents.append(types.Content(role="user", parts=[types.Part(text=message)]))
    for _ in range(MAX_TOOL_ROUNDS):
        response = client.models.generate_content(model=model_name, contents=contents, config=config)
        u = getattr(response, "usage_metadata", None)
        if u:
            trace["jetons_entree"] = trace.get("jetons_entree", 0) + (u.prompt_token_count or 0)
            trace["jetons_sortie"] = trace.get("jetons_sortie", 0) + (u.candidates_token_count or 0)
        candidate = response.candidates[0]
        parts = candidate.content.parts if candidate.content and candidate.content.parts else []
        fc_parts = [p for p in parts if p.function_call and p.function_call.name]
        if not fc_parts:
            return {"reponse": "".join(p.text for p in parts if p.text) or "Je n'ai pas pu formuler de réponse."}
        contents.append(candidate.content)
        result_parts = []
        for p in fc_parts:
            fc, t0 = p.function_call, time.time()
            args = dict(fc.args or {})
            result = _run_tool(db, fc.name, args)
            _tracer_outil(trace["outils"], fc.name, args, t0, result)
            result_parts.append(types.Part(function_response=types.FunctionResponse(
                name=fc.name, response={"result": json.dumps(result, ensure_ascii=False, default=str)})))
        contents.append(types.Content(role="user", parts=result_parts))
    return {"reponse": "Trop d'appels d'outils pour cette question — reformulez-la plus précisément."}


# ── Anthropic (Claude) ────────────────────────────────────────────────
def _chat_anthropic(db: Session, message: str, historique: list, api_key: str, trace: dict) -> dict:
    from anthropic import Anthropic
    trace.update(fournisseur="anthropic", modele=CLAUDE_MODEL)
    client = Anthropic(api_key=api_key, timeout=DELAI_IA_S, max_retries=2)
    tools = [{"name": o["name"], "description": o["description"], "input_schema": o["parameters"]} for o in OUTILS]
    system = _system_prompt(db)
    messages = list(historique) + [{"role": "user", "content": message}]
    for _ in range(MAX_TOOL_ROUNDS):
        resp = client.messages.create(model=CLAUDE_MODEL, max_tokens=4096, system=system, tools=tools, messages=messages)
        if getattr(resp, "usage", None):
            trace["jetons_entree"] = trace.get("jetons_entree", 0) + (resp.usage.input_tokens or 0)
            trace["jetons_sortie"] = trace.get("jetons_sortie", 0) + (resp.usage.output_tokens or 0)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "tool_use":
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    t0 = time.time()
                    result = _run_tool(db, block.name, block.input)
                    _tracer_outil(trace["outils"], block.name, dict(block.input or {}), t0, result)
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                         "content": json.dumps(result, ensure_ascii=False, default=str)})
            messages.append({"role": "user", "content": tool_results})
            continue
        return {"reponse": "".join(b.text for b in resp.content if b.type == "text") or "Je n'ai pas pu formuler de réponse."}
    return {"reponse": "Trop d'appels d'outils pour cette question — reformulez-la plus précisément."}


# ── Point d'entrée ────────────────────────────────────────────────────
def chat(db: Session, message: str, historique: list) -> dict:
    """Répond à une question. Retourne {reponse, historique, trace} ; trace =
    fournisseur, modèle, outils appelés, jetons, erreur (journal IA)."""
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    contexte = _historique_contexte(historique)
    trace: dict = {"outils": [], "fournisseur": None, "modele": None, "erreur": None}
    debut = time.time()

    def fin(reponse: str, ok: bool = True) -> dict:
        trace["duree_ms"] = int((time.time() - debut) * 1000)
        nouv = (historique or []) + ([{"role": "user", "content": message}, {"role": "assistant", "content": reponse}] if ok else [])
        return {"reponse": reponse, "historique": nouv, "trace": trace}

    if gemini_key:
        try:
            return fin(_chat_gemini(db, message, contexte, gemini_key, trace)["reponse"])
        except Exception as e:
            trace["erreur"] = f"gemini : {str(e)[:300]}"
            if anthropic_key:
                try:
                    trace["outils"] = []
                    return fin(_chat_anthropic(db, message, contexte, anthropic_key, trace)["reponse"])
                except Exception as e2:
                    trace["erreur"] += f" | anthropic : {str(e2)[:300]}"
                    return fin("Le service d'IA est momentanément indisponible. Réessayez dans un instant.", ok=False)
            err = str(e)
            if "503" in err or "UNAVAILABLE" in err:
                msg = "Le service Gemini est temporairement surchargé. Réessayez dans quelques secondes."
            elif "429" in err or "RESOURCE_EXHAUSTED" in err:
                msg = "Quota Gemini dépassé. Attendez une minute puis réessayez."
            else:
                msg = "Le service d'IA n'a pas pu répondre. Réessayez dans un instant."
            return fin(msg, ok=False)
    if anthropic_key:
        try:
            return fin(_chat_anthropic(db, message, contexte, anthropic_key, trace)["reponse"])
        except Exception as e:
            trace["erreur"] = f"anthropic : {str(e)[:300]}"
            return fin("Le service d'IA n'a pas pu répondre. Réessayez dans un instant.", ok=False)
    trace["erreur"] = "aucune clé API"
    return fin("Aucune clé API configurée (GEMINI_API_KEY ou ANTHROPIC_API_KEY).", ok=False)
