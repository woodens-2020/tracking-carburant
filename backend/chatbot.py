"""
Chatbot d'analyse des ventes avec function calling (tool use).

Priorité :
  1. Google Gemini  (GEMINI_API_KEY)  — gratuit, généreux
  2. Anthropic Claude (ANTHROPIC_API_KEY) — fallback payant
"""
import os
import re
import json
import time
from datetime import date

from sqlalchemy.orm import Session
from sqlalchemy import text as sql_text
from stats import compute_stats, liste_produits_pompes
from stock_service import (
    stock_restant,
    rentabilite_globale,
    gallons_livres,
    cout_moyen_pondere,
    SEUIL_ALERTE_JOURS_PAR_DEFAUT,
)
from models import Base

MAX_TOOL_ROUNDS = 8
GEMINI_MODELS   = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-001"]
CLAUDE_MODEL    = "claude-sonnet-4-6"


# ── Accès libre en lecture seule à toutes les données métier ──────
# Tables d'authentification / sécurité : jamais exposées au chatbot,
# quelle que soit la question posée (mots de passe, tokens, OTP...).
_TABLES_EXCLUES = {
    "roles", "utilisateurs", "sessions", "login_security_events",
    "otp_codes", "admin_codes", "audit_logs", "password_reset_tokens",
}

_TABLES_AUTORISEES = sorted(
    nom for nom in Base.metadata.tables if nom not in _TABLES_EXCLUES
)

_MOTS_SQL_INTERDITS = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "grant", "revoke", "attach", "detach", "pragma", "vacuum", "copy",
    "exec", "execute", "call", "merge", "replace", "into",
)

_LIMITE_LIGNES_REQUETE = 200


def _schema_texte() -> str:
    lignes = []
    for nom in _TABLES_AUTORISEES:
        colonnes = ", ".join(c.name for c in Base.metadata.tables[nom].columns)
        lignes.append(f"  - {nom}({colonnes})")
    return "\n".join(lignes)


def _valider_sql_lecture_seule(sql: str) -> str:
    propre = (sql or "").strip().rstrip(";").strip()
    if not propre:
        raise ValueError("Requête vide.")
    if ";" in propre:
        raise ValueError("Une seule instruction SELECT autorisée (pas de point-virgule).")
    minuscule = propre.lower()
    if not re.match(r"^\s*(select|with)\b", minuscule):
        raise ValueError("Seules les requêtes SELECT (ou WITH ... SELECT) sont autorisées.")
    for mot in _MOTS_SQL_INTERDITS:
        if re.search(rf"\b{re.escape(mot)}\b", minuscule):
            raise ValueError(f"Mot-clé interdit dans la requête : {mot}")
    tables_referencees = set(re.findall(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", minuscule))
    non_autorisees = tables_referencees - set(_TABLES_AUTORISEES)
    if non_autorisees:
        raise ValueError(
            f"Table(s) non autorisée(s) ou inconnue(s) : {', '.join(sorted(non_autorisees))}. "
            f"Appelle lister_tables pour voir les tables disponibles."
        )
    return propre


def _executer_requete(db: Session, sql: str) -> dict:
    try:
        propre = _valider_sql_lecture_seule(sql)
    except ValueError as e:
        return {"erreur": str(e)}
    try:
        resultat = db.execute(sql_text(propre))
    except Exception as e:
        return {"erreur": f"Erreur SQL : {e}"}
    colonnes = list(resultat.keys())
    lignes = resultat.fetchmany(_LIMITE_LIGNES_REQUETE + 1)
    tronque = len(lignes) > _LIMITE_LIGNES_REQUETE
    lignes = lignes[:_LIMITE_LIGNES_REQUETE]
    return {
        "colonnes": colonnes,
        "lignes": [dict(zip(colonnes, row)) for row in lignes],
        "nb_lignes": len(lignes),
        "tronque": tronque,
        "avertissement_troncature": (
            f"Résultat tronqué à {_LIMITE_LIGNES_REQUETE} lignes — affine ta requête "
            "(filtre, agrégat, LIMIT) si besoin de plus de précision."
        ) if tronque else None,
    }


# ── Prompt système ────────────────────────────────────────────────
def _system_prompt(db: Session) -> str:
    produits = liste_produits_pompes(db)
    lignes = []
    for nom, info in produits.items():
        pompes_str = ", ".join(info["pompes"]) if info["pompes"] else "aucune pompe"
        lignes.append(f"  - {nom} (id={info['id']}) : {pompes_str}")
    contexte_produits = "\n".join(lignes) if lignes else "  (aucun produit configuré)"

    from datetime import timedelta
    today      = date.today()
    debut_mois = today.strftime("%Y-%m-01")
    # Bug 3 fix : utiliser timedelta au lieu de replace(day=...) qui casse en début de mois
    sept_jours_avant = (today - timedelta(days=6)).isoformat()
    hier             = (today - timedelta(days=1)).isoformat()

    dialecte = db.bind.dialect.name if db.bind is not None else "postgresql"

    return f"""Tu es l'assistant d'analyse de **Konekta** — une institution en Haïti qui gère
plusieurs départements : Station de carburant, Bar/POS, Hôtel, Cuisine, Zelle
(transferts USD/HTG), employés et paie, dépenses et achats.
Tu aides l'opérateur/le gérant à consulter, analyser et croiser les données de TOUS ces départements.

━━━ CONTEXTE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DATE DU JOUR   : {today.isoformat()}
MONNAIE        : gourde haïtienne — symbole G (Zelle : USD, converti au taux enregistré)
QUANTITÉS      : gallons — symbole gal
PÉRIODES       : Matin / Apres-midi (deux saisies par jour par pompe, module carburant)
MOTEUR DE BASE : {dialecte} — écris du SQL compatible avec ce moteur

PRODUITS ET POMPES (carburant) ENREGISTRÉS DANS LA BASE :
{contexte_produits}

━━━ CONVERSION DES DATES RELATIVES ━━━━━━━━━━━━━━
Convertis TOUJOURS en AAAA-MM-JJ avant d'appeler un outil :
- "aujourd'hui"            → {today.isoformat()}
- "les 7 derniers jours"   → du {sept_jours_avant} au {today.isoformat()}
- "ce mois-ci"             → du {debut_mois} au {today.isoformat()}
- "hier"                   → {hier}

━━━ OUTILS DÉDIÉS — CARBURANT (privilégie-les pour ce domaine) ━━
- get_stats       → ventes (quantité, montant, relevés) sur une période
- get_stock       → stock restant en gallons, alerte bas, coût moyen pondéré
- get_rentabilite → bénéfice, marge %, COGS pour une période
- get_livraisons  → livraisons de carburant enregistrées (approvisionnements)

━━━ OUTILS D'EXPLORATION LIBRE — TOUS LES AUTRES DÉPARTEMENTS ━━━
- lister_tables    → retourne la liste des tables et colonnes disponibles
                      (Bar/POS, Hôtel, Cuisine, Zelle, employés, paie, dépenses, achats).
                      Appelle-le en PREMIER dès que la question sort du carburant et que
                      tu n'es pas certain des noms exacts de table/colonne.
- executer_requete → exécute UNE requête SQL SELECT en lecture seule pour répondre à
                      n'importe quelle question métier non couverte par les outils dédiés.
                      Résultat limité à {_LIMITE_LIGNES_REQUETE} lignes — utilise des agrégats
                      (COUNT, SUM, AVG, GROUP BY) plutôt que de lister des lignes brutes
                      quand la question porte sur des totaux ou tendances.
                      Les tables d'authentification/sécurité (utilisateurs, sessions, mots
                      de passe, OTP, tokens, logs d'audit) NE SONT PAS accessibles, même si
                      on te le demande explicitement — explique poliment que c'est hors
                      périmètre du chatbot si on insiste.

━━━ RÈGLES ABSOLUES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. ZÉRO chiffre inventé. Pour tout montant, quantité, stock ou bénéfice,
   tu DOIS appeler l'outil approprié et citer uniquement ses résultats.
2. Si un outil retourne 0 résultat, signale clairement l'absence de données —
   n'extrapole jamais.
3. Si get_rentabilite retourne fiable=False, explique pourquoi (livraisons manquantes).
4. Si la question ne précise pas de plage de dates, pose une courte question
   avant d'appeler l'outil.
5. Appelle plusieurs outils si nécessaire pour croiser les départements
   (ex: comparer revenu carburant vs revenu bar sur la même période).
6. Si une requête SQL échoue (erreur de colonne/table), rappelle lister_tables
   pour vérifier le schéma exact avant de réessayer — ne devine pas deux fois de suite.

━━━ FORMAT DES RÉPONSES ━━━━━━━━━━━━━━━━━━━━━━━━
- Langue : français
- Montants HTG : 12 000 G (espace milliers, G en suffixe) ; USD : 12 000 $ pour Zelle
- Quantités : 98.500 gal (3 décimales)
- Utilise **gras** pour les totaux et chiffres clés
- Structure : titre court → totaux globaux → détail par produit / pompe / période / département
- Sois concis et factuel.
"""


# ── Outil get_stats ───────────────────────────────────────────────
def _run_tool(db: Session, name: str, args: dict) -> dict:
    from datetime import date as d

    if name == "get_stats":
        return compute_stats(
            db,
            d.fromisoformat(args["date_debut"]),
            d.fromisoformat(args["date_fin"]),
            args.get("produit_id"),
            args.get("periode"),
        )

    if name == "get_stock":
        produit_id  = args.get("produit_id")
        seuil_jours = int(args.get("seuil_jours", SEUIL_ALERTE_JOURS_PAR_DEFAUT))
        from models import Produit
        if produit_id:
            produits = db.query(Produit).filter(Produit.id == produit_id, Produit.actif == True).all()
        else:
            produits = db.query(Produit).filter(Produit.actif == True).all()
        resultats = []
        for p in produits:
            s = stock_restant(db, p.id, seuil_jours=seuil_jours)
            s["produit_nom"] = p.nom
            s["cout_moyen_pondere"] = cout_moyen_pondere(db, p.id, uniquement_ouvertes=True)
            resultats.append(s)
        return {"stocks": resultats, "nb_alertes": sum(1 for r in resultats if r["alerte_bas"])}

    if name == "get_rentabilite":
        d_debut = d.fromisoformat(args["date_debut"])
        d_fin   = d.fromisoformat(args["date_fin"])
        return rentabilite_globale(db, d_debut, d_fin, args.get("produit_id"))

    if name == "get_livraisons":
        from models import Livraison, Produit
        q = db.query(Livraison)
        if args.get("produit_id"):
            q = q.filter(Livraison.produit_id == args["produit_id"])
        if args.get("date_debut"):
            q = q.filter(Livraison.date_livraison >= d.fromisoformat(args["date_debut"]))
        if args.get("date_fin"):
            q = q.filter(Livraison.date_livraison <= d.fromisoformat(args["date_fin"]))
        livraisons = q.order_by(Livraison.date_livraison.desc()).limit(20).all()
        return {
            "nb": len(livraisons),
            "livraisons": [
                {
                    "id":                l.id,
                    "produit_nom":       l.produit.nom,
                    "date_livraison":    str(l.date_livraison),
                    "gallons_recus":     float(l.gallons_recus),
                    "prix_achat_gallon": float(l.prix_achat_gallon),
                    "fournisseur":       l.fournisseur,
                }
                for l in livraisons
            ],
        }

    if name == "lister_tables":
        return {"tables": _TABLES_AUTORISEES, "schema": _schema_texte()}

    if name == "executer_requete":
        return _executer_requete(db, args.get("sql", ""))

    return {"erreur": f"Outil inconnu : {name}"}


# ── Implémentation Gemini ─────────────────────────────────────────
def _chat_gemini(db: Session, message: str, historique: list, api_key: str) -> dict:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    last_err = None

    for i, model_name in enumerate(GEMINI_MODELS):
        try:
            return _chat_gemini_model(db, message, historique, client, model_name, types)
        except Exception as e:
            err_str = str(e)
            if "503" in err_str or "UNAVAILABLE" in err_str or "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                last_err = e
                if i < len(GEMINI_MODELS) - 1:
                    time.sleep(1)  # pause avant le prochain modèle
                continue
            raise

    raise Exception(f"Tous les modèles Gemini indisponibles. Dernière erreur : {last_err}")


def _chat_gemini_model(db: Session, message: str, historique: list, client, model_name: str, types) -> dict:
    get_stats_decl = types.FunctionDeclaration(
        name="get_stats",
        description=(
            "Récupère les statistiques de ventes réelles depuis la base de données "
            "pour un intervalle de dates donné. Retourne : total_quantite (gal), "
            "total_montant (G), nb_releves, nb_jours_couverts, et le détail par "
            "produit, pompe et période. C'est la SEULE source fiable — appelle-la "
            "TOUJOURS avant de citer un chiffre."
        ),
        parameters={
            "type": "object",
            "properties": {
                "date_debut": {"type": "string", "description": "Date début AAAA-MM-JJ (incluse)."},
                "date_fin":   {"type": "string", "description": "Date fin AAAA-MM-JJ (incluse)."},
                "produit_id": {"type": "integer", "description": "Optionnel — id produit."},
                "periode":    {"type": "string",  "description": "Optionnel — 'Matin' ou 'Apres-midi'."},
            },
            "required": ["date_debut", "date_fin"],
        },
    )
    get_stock_decl = types.FunctionDeclaration(
        name="get_stock",
        description=(
            "Retourne le stock restant en gallons pour chaque produit actif, "
            "calculé à partir des livraisons moins les ventes dérivées des relevés. "
            "Inclut l'alerte stock bas, le coût moyen pondéré et la valeur du stock. "
            "Appelle cet outil pour toute question sur le stock, les livraisons restantes "
            "ou la disponibilité de carburant."
        ),
        parameters={
            "type": "object",
            "properties": {
                "produit_id":  {"type": "integer", "description": "Optionnel — filtrer sur un produit."},
                "seuil_jours": {"type": "integer", "description": "Optionnel — alerte si stock < N jours de vente (défaut 7)."},
            },
            "required": [],
        },
    )
    get_rentabilite_decl = types.FunctionDeclaration(
        name="get_rentabilite",
        description=(
            "Calcule le bénéfice, la marge et le COGS sur une période. "
            "Bénéfice = Revenu (relevés) − COGS (WAC × gallons vendus). "
            "Retourne None si aucune livraison n'est enregistrée (COGS incalculable). "
            "Appelle cet outil pour les questions de rentabilité, marge ou bénéfice."
        ),
        parameters={
            "type": "object",
            "properties": {
                "date_debut": {"type": "string",  "description": "Date début AAAA-MM-JJ."},
                "date_fin":   {"type": "string",  "description": "Date fin AAAA-MM-JJ."},
                "produit_id": {"type": "integer", "description": "Optionnel — filtrer sur un produit."},
            },
            "required": ["date_debut", "date_fin"],
        },
    )
    get_livraisons_decl = types.FunctionDeclaration(
        name="get_livraisons",
        description=(
            "Retourne les dernières livraisons de carburant enregistrées (max 20). "
            "Filtre optionnel par produit et/ou plage de dates. "
            "Appelle cet outil pour les questions sur les approvisionnements reçus."
        ),
        parameters={
            "type": "object",
            "properties": {
                "produit_id": {"type": "integer", "description": "Optionnel — filtrer par produit."},
                "date_debut": {"type": "string",  "description": "Optionnel — date début AAAA-MM-JJ."},
                "date_fin":   {"type": "string",  "description": "Optionnel — date fin AAAA-MM-JJ."},
            },
            "required": [],
        },
    )
    lister_tables_decl = types.FunctionDeclaration(
        name="lister_tables",
        description=(
            "Liste toutes les tables et colonnes disponibles en base (Bar/POS, Hôtel, "
            "Cuisine, Zelle, employés, paie, dépenses, achats...). Appelle-la en premier "
            "avant executer_requete si tu n'es pas certain du schéma exact."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
    )
    executer_requete_decl = types.FunctionDeclaration(
        name="executer_requete",
        description=(
            "Exécute UNE requête SQL SELECT en lecture seule pour répondre à toute "
            "question métier non couverte par get_stats/get_stock/get_rentabilite/"
            "get_livraisons — Bar/POS, Hôtel, Cuisine, Zelle, employés, paie, dépenses, "
            "achats. Max 200 lignes retournées. Les tables d'authentification/sécurité "
            "sont bloquées."
        ),
        parameters={
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "Requête SQL SELECT (lecture seule)."},
            },
            "required": ["sql"],
        },
    )
    tools = [types.Tool(function_declarations=[
        get_stats_decl, get_stock_decl, get_rentabilite_decl, get_livraisons_decl,
        lister_tables_decl, executer_requete_decl,
    ])]
    config = types.GenerateContentConfig(
        system_instruction=_system_prompt(db),
        tools=tools,
        temperature=0.1,
    )

    # Convertir l'historique au format Gemini (role "model" au lieu de "assistant")
    contents = []
    for h in historique:
        role = "user" if h["role"] == "user" else "model"
        contents.append(types.Content(
            role=role,
            parts=[types.Part(text=h["content"])],
        ))
    contents.append(types.Content(
        role="user",
        parts=[types.Part(text=message)],
    ))

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=config,
        )

        candidate = response.candidates[0]

        # Extraire les function calls
        fc_parts = [
            p for p in candidate.content.parts
            if p.function_call and p.function_call.name
        ]

        if not fc_parts:
            # Réponse texte finale
            text = "".join(
                p.text for p in candidate.content.parts if p.text
            )
            new_hist = historique + [
                {"role": "user",      "content": message},
                {"role": "assistant", "content": text},
            ]
            return {"reponse": text, "historique": new_hist}

        # Ajouter la réponse du modèle à la conversation
        contents.append(candidate.content)

        # Exécuter les outils et renvoyer les résultats
        result_parts = []
        for p in fc_parts:
            fc     = p.function_call
            result = _run_tool(db, fc.name, dict(fc.args))
            result_parts.append(types.Part(
                function_response=types.FunctionResponse(
                    name=fc.name,
                    response={"result": json.dumps(result, ensure_ascii=False, default=str)},
                )
            ))
        contents.append(types.Content(role="user", parts=result_parts))

    return {"reponse": "Trop d'appels d'outils — requête interrompue.",
            "historique": historique}


# ── Implémentation Anthropic (Claude) ─────────────────────────────
_ANTHROPIC_TOOLS = [
    {
        "name": "get_stats",
        "description": (
            "Récupère les statistiques de ventes réelles depuis la base de données. "
            "Retourne : total_quantite, total_montant, nb_releves, nb_jours_couverts, "
            "et le détail par produit, pompe et période. "
            "Appelle-la TOUJOURS avant de citer un chiffre."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_debut": {"type": "string",  "description": "Date début AAAA-MM-JJ."},
                "date_fin":   {"type": "string",  "description": "Date fin AAAA-MM-JJ."},
                "produit_id": {"type": "integer", "description": "Optionnel — id produit."},
                "periode":    {"type": "string",  "enum": ["Matin", "Apres-midi"],
                               "description": "Optionnel — période."},
            },
            "required": ["date_debut", "date_fin"],
        },
    },
    {
        "name": "get_stock",
        "description": (
            "Stock restant en gallons par produit = livraisons − ventes relevés. "
            "Inclut alerte stock bas, coût moyen pondéré et valeur en gourdes. "
            "Appelle pour toute question sur le stock ou la disponibilité carburant."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "produit_id":  {"type": "integer", "description": "Optionnel — filtrer par produit."},
                "seuil_jours": {"type": "integer", "description": "Optionnel — alerte si < N jours (défaut 7)."},
            },
            "required": [],
        },
    },
    {
        "name": "get_rentabilite",
        "description": (
            "Calcule bénéfice, marge et COGS sur une période. "
            "Bénéfice = Revenu (relevés) − COGS (WAC × gallons vendus). "
            "Retourne null si aucune livraison (COGS incalculable). "
            "Appelle pour questions de rentabilité, marge ou bénéfice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_debut": {"type": "string",  "description": "Date début AAAA-MM-JJ."},
                "date_fin":   {"type": "string",  "description": "Date fin AAAA-MM-JJ."},
                "produit_id": {"type": "integer", "description": "Optionnel — filtrer par produit."},
            },
            "required": ["date_debut", "date_fin"],
        },
    },
    {
        "name": "get_livraisons",
        "description": (
            "Retourne les dernières livraisons de carburant enregistrées (max 20). "
            "Appelle pour questions sur les approvisionnements ou les livraisons reçues."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "produit_id": {"type": "integer", "description": "Optionnel — filtrer par produit."},
                "date_debut": {"type": "string",  "description": "Optionnel — date début AAAA-MM-JJ."},
                "date_fin":   {"type": "string",  "description": "Optionnel — date fin AAAA-MM-JJ."},
            },
            "required": [],
        },
    },
    {
        "name": "lister_tables",
        "description": (
            "Liste toutes les tables et colonnes disponibles en base (Bar/POS, Hôtel, "
            "Cuisine, Zelle, employés, paie, dépenses, achats...). Appelle-la en premier "
            "avant executer_requete si tu n'es pas certain du schéma exact."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "executer_requete",
        "description": (
            "Exécute UNE requête SQL SELECT en lecture seule pour répondre à toute "
            "question métier non couverte par get_stats/get_stock/get_rentabilite/"
            "get_livraisons — Bar/POS, Hôtel, Cuisine, Zelle, employés, paie, dépenses, "
            "achats. Max 200 lignes retournées. Les tables d'authentification/sécurité "
            "sont bloquées."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "Requête SQL SELECT (lecture seule)."},
            },
            "required": ["sql"],
        },
    },
]


def _chat_anthropic(db: Session, message: str, historique: list, api_key: str) -> dict:
    from anthropic import Anthropic

    client  = Anthropic(api_key=api_key)
    system  = _system_prompt(db)
    messages = list(historique) + [{"role": "user", "content": message}]

    for _ in range(MAX_TOOL_ROUNDS):
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=2048, system=system,
            tools=_ANTHROPIC_TOOLS, messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "tool_use":
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result = _run_tool(db, block.name, block.input)
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     json.dumps(result, ensure_ascii=False, default=str),
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        text = "".join(b.text for b in resp.content if b.type == "text")
        new_hist = historique + [
            {"role": "user",      "content": message},
            {"role": "assistant", "content": text},
        ]
        return {"reponse": text, "historique": new_hist}

    return {"reponse": "Trop d'appels d'outils — requête interrompue.",
            "historique": historique}


# ── Point d'entrée public ─────────────────────────────────────────
def chat(db: Session, message: str, historique: list) -> dict:
    """
    Choisit automatiquement l'API disponible :
      GEMINI_API_KEY   → Google Gemini 2.0 Flash (gratuit)
      ANTHROPIC_API_KEY → Anthropic Claude (payant)
    """
    gemini_key    = os.environ.get("GEMINI_API_KEY", "").strip()
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

    if gemini_key:
        try:
            return _chat_gemini(db, message, historique, gemini_key)
        except Exception as e:
            err_str = str(e)
            # Si Gemini échoue, on essaie Anthropic en fallback
            if anthropic_key:
                return _chat_anthropic(db, message, historique, anthropic_key)
            # Message d'erreur lisible selon le type
            if "503" in err_str or "UNAVAILABLE" in err_str:
                msg = ("Le service Gemini est temporairement surchargé. "
                       "Veuillez réessayer dans quelques secondes.")
            elif "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                msg = ("Quota Gemini dépassé. "
                       "Attendez une minute puis réessayez, ou générez une nouvelle clé API.")
            else:
                msg = f"Erreur du service IA : {e}"
            return {"reponse": msg, "historique": historique}

    if anthropic_key:
        return _chat_anthropic(db, message, historique, anthropic_key)

    return {
        "reponse": (
            "Aucune clé API configurée. Ajoutez GEMINI_API_KEY (gratuit) "
            "ou ANTHROPIC_API_KEY dans le fichier .env."
        ),
        "historique": historique,
    }
