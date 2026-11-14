"""
Connexion à la base de données.

Variables d'environnement (fichier .env ou système) :
  DATABASE_URL=postgresql+psycopg2://user:password@host:5432/dbname
  (Fallback SQLite pour développement local sans PostgreSQL)
"""
import contextvars
import os
import re
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session as _Session
# Chargement du fichier .env si python-dotenv est disponible
# ENV_FILE permet de pointer vers un autre fichier (ex: .env.test) sans toucher au .env de prod
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / os.environ.get("ENV_FILE", ".env"), override=True)
except ImportError:
    pass

from models import (
    Base, Produit, Pompe, Utilisateur, Employe, FichePaie, Depense, Achat, ParametreDepense,
    CategorieDepense, CategorieAchat, Poste, Beneficiaire,  # noqa: F401 — listes de référence partagées (create_all)
    BarProduit, BarPrixHistorique, BarAchat, BarMouvementStock,
    BarVente, BarLigneVente, BarCredit, BarRemboursement,
    BarCommande, BarLigneCommande, BarPaiementEmploye,
    HotelChambre, HotelEmploye, HotelReservation, HotelRapportNote,  # noqa: F401
    LoginSecurityEvent,  # noqa: F401 — nécessaire pour create_all
    BarSessionEvaluation,  # noqa: F401 — nécessaire pour create_all
    PatisserieCategorie, PatisserieProduit, PatisserieAchat,        # noqa: F401
    PatisserieMouvementStock, PatisserieSessionCaisse,              # noqa: F401
    PatisserieVente, PatisserieLigneVente, PatisserieDepense,       # noqa: F401
    PatisserieEtapeSuivi, PatisserieCommande, PatisserieLigneCommande,  # noqa: F401
    PatisserieCommandeSuivi,                                        # noqa: F401
)

# ── URL de connexion ──────────────────────────────────────────────
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "sqlite:///station.db",   # fallback développement
)

_is_postgres = DATABASE_URL.startswith("postgresql")
_is_sqlite   = DATABASE_URL.startswith("sqlite")

# ── Moteur SQLAlchemy ─────────────────────────────────────────────
if _is_postgres:
    engine = create_engine(
        DATABASE_URL,
        pool_size=5,          # connexions maintenues ouvertes
        max_overflow=10,      # connexions supplémentaires autorisées
        pool_pre_ping=True,   # vérifie la connexion avant usage
        pool_recycle=1800,    # recycle les connexions toutes les 30 min
        echo=False,
    )
elif _is_sqlite:
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        echo=False,
    )

    # Active les clés étrangères sur SQLite (désactivées par défaut) et
    # rattache les bases des stations séparées (un fichier par espace, vu
    # comme un schéma — même mécanisme que les schémas PostgreSQL).
    @event.listens_for(engine, "connect")
    def _sqlite_fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        try:
            for (sch,) in dbapi_conn.execute("SELECT schema FROM espaces").fetchall():
                if _SCHEMA_RE.match(sch or ""):
                    dbapi_conn.execute(f"ATTACH DATABASE ? AS {sch}", (_fichier_espace(sch),))
        except Exception:
            pass                               # table pas encore créée

else:
    raise ValueError(f"DATABASE_URL non supportée : {DATABASE_URL}")

# ── Espaces (stations séparées) ───────────────────────────────────
# L'espace courant (None = espace principal) est porté par la requête
# (cookie, voir main.EspaceMiddleware) ; toute session ouverte pendant la
# requête lit et écrit dans le schéma de cet espace.
_SCHEMA_RE = re.compile(r"^esp_[0-9]{1,6}$")
_espace_courant: contextvars.ContextVar = contextvars.ContextVar("konekta_espace", default=None)
_moteurs: dict = {}


def espace_courant():
    return _espace_courant.get()


def definir_espace(schema):
    """Retourne le jeton pour revenir à l'espace précédent (reset)."""
    if schema is not None and not _SCHEMA_RE.match(schema):
        raise ValueError("Schéma d'espace invalide")
    return _espace_courant.set(schema)


def retablir_espace(jeton):
    _espace_courant.reset(jeton)


def moteur_espace(schema):
    """Moteur qui traduit les tables vers le schéma de l'espace."""
    if not schema:
        return engine
    if schema not in _moteurs:
        _moteurs[schema] = engine.execution_options(schema_translate_map={None: schema})
    return _moteurs[schema]


def _fichier_espace(schema: str) -> str:
    base = DATABASE_URL.replace("sqlite:///", "", 1)
    p = Path(base)
    return str(p.with_name(f"{p.stem}__{schema}{p.suffix or '.db'}"))


class _SessionEspace:
    """Remplace sessionmaker : chaque session est liée à l'espace courant."""
    def __init__(self):
        self._fabrique = sessionmaker(autocommit=False, autoflush=False)

    def __call__(self, **kw):
        kw.setdefault("bind", moteur_espace(_espace_courant.get()))
        return self._fabrique(**kw)


SessionLocal = _SessionEspace()


def session_principale() -> _Session:
    """Session sur l'espace principal, quel que soit l'espace courant."""
    return _Session(bind=engine, autoflush=False)


def session_espace(schema) -> _Session:
    return _Session(bind=moteur_espace(schema), autoflush=False)


def creer_schema_espace(schema: str) -> None:
    """Crée le schéma (PostgreSQL) ou le fichier rattaché (SQLite) et toutes
    les tables de Konekta dedans."""
    if not _SCHEMA_RE.match(schema):
        raise ValueError("Schéma d'espace invalide")
    if _is_postgres:
        with engine.begin() as c:
            c.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    else:
        engine.dispose()                       # les connexions rouvertes rattachent le nouveau fichier
        with engine.begin() as c:
            noms = [r[1] for r in c.exec_driver_sql("PRAGMA database_list").fetchall()]
            if schema not in noms:
                c.exec_driver_sql(f"ATTACH DATABASE ? AS {schema}", (_fichier_espace(schema),))
    with moteur_espace(schema).begin() as c:
        Base.metadata.create_all(bind=c)


def migrer_espaces() -> int:
    """Démarrage : chaque espace séparé reçoit les nouvelles tables et les
    colonnes ajoutées depuis sa création (ajout générique, colonnes
    facultatives). Retourne le nombre d'espaces mis à jour."""
    from sqlalchemy import inspect as _inspect
    try:
        with session_principale() as db:
            schemas = [r[0] for r in db.execute(text("SELECT schema FROM espaces")).fetchall()]
    except Exception:
        return 0
    n = 0
    for sch in schemas:
        if not _SCHEMA_RE.match(sch or ""):
            continue
        try:
            with moteur_espace(sch).begin() as c:
                Base.metadata.create_all(bind=c)
            insp = _inspect(engine)
            with engine.begin() as c:
                for table in Base.metadata.sorted_tables:
                    try:
                        existantes = {col["name"] for col in insp.get_columns(table.name, schema=sch)}
                    except Exception:
                        continue
                    for col in table.columns:
                        if col.name in existantes:
                            continue
                        type_sql = col.type.compile(dialect=engine.dialect)
                        c.execute(text(f'ALTER TABLE "{sch}"."{table.name}" ADD COLUMN "{col.name}" {type_sql}'
                                       if _is_postgres else
                                       f'ALTER TABLE {sch}."{table.name}" ADD COLUMN "{col.name}" {type_sql}'))
            n += 1
        except Exception as exc:
            print(f"Espace {sch} : migration impossible : {exc}")
    return n


def get_db():
    """Dépendance FastAPI : fournit une session (sur l'espace de la requête) et la ferme automatiquement."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Migration légère (colonnes ajoutées en v1.1) ─────────────────
def _migrate_columns():
    """Ajoute les nouvelles colonnes aux tables existantes (idempotent, SQLite + PostgreSQL).

    SQLite (< 3.37) ne supporte pas ADD COLUMN ... UNIQUE. On ajoute la
    colonne sans contrainte puis on crée l'index UNIQUE séparément.
    """
    from sqlalchemy import inspect, text as sql_text

    insp = inspect(engine)

    if _is_sqlite:
        # (table, col, ddl_colonne, ddl_index_optionnel)
        new_cols = [
            ("produits",     "actif",            "ALTER TABLE produits      ADD COLUMN actif INTEGER NOT NULL DEFAULT 1",  None),
            ("produits",     "created_at",       "ALTER TABLE produits      ADD COLUMN created_at DATETIME DEFAULT NULL",  None),
            ("pompes",       "actif",            "ALTER TABLE pompes        ADD COLUMN actif INTEGER NOT NULL DEFAULT 1",   None),
            ("pompes",       "created_at",       "ALTER TABLE pompes        ADD COLUMN created_at DATETIME DEFAULT NULL",   None),
            ("releves",      "created_at",       "ALTER TABLE releves       ADD COLUMN created_at DATETIME DEFAULT NULL",   None),
            ("releves",      "updated_at",       "ALTER TABLE releves       ADD COLUMN updated_at DATETIME DEFAULT NULL",   None),
            ("releves",      "nb_modifications", "ALTER TABLE releves       ADD COLUMN nb_modifications INTEGER NOT NULL DEFAULT 0", None),
            # UNIQUE ajouté via index séparé (SQLite interdit ADD COLUMN ... UNIQUE)
            ("utilisateurs", "api_key_hash",
             "ALTER TABLE utilisateurs  ADD COLUMN api_key_hash VARCHAR(64)",
             "CREATE UNIQUE INDEX IF NOT EXISTS uq_utilisateurs_api_key_hash ON utilisateurs(api_key_hash)"),
            # v2 — OAuth + gestion des employés
            ("utilisateurs", "email",
             "ALTER TABLE utilisateurs  ADD COLUMN email VARCHAR(254)",
             "CREATE UNIQUE INDEX IF NOT EXISTS uq_utilisateurs_email ON utilisateurs(email)"),
            ("utilisateurs", "oauth_provider",
             "ALTER TABLE utilisateurs  ADD COLUMN oauth_provider VARCHAR(32)", None),
            ("utilisateurs", "oauth_sub",
             "ALTER TABLE utilisateurs  ADD COLUMN oauth_sub VARCHAR(255)",
             "CREATE UNIQUE INDEX IF NOT EXISTS uq_utilisateurs_oauth_sub ON utilisateurs(oauth_sub)"),
            # v3 — code d'accès 9 chiffres
            ("utilisateurs", "code_acces_hash",
             "ALTER TABLE utilisateurs  ADD COLUMN code_acces_hash VARCHAR(255)", None),
            # v7 — suivi activité sessions
            ("sessions", "last_activity_at",
             "ALTER TABLE sessions ADD COLUMN last_activity_at DATETIME DEFAULT NULL", None),
            # v8 — clôture de cargaison (suivi FIFO par livraison)
            ("livraisons", "terminee",
             "ALTER TABLE livraisons ADD COLUMN terminee INTEGER NOT NULL DEFAULT 0", None),
            ("livraisons", "gallons_report_recu",
             "ALTER TABLE livraisons ADD COLUMN gallons_report_recu NUMERIC DEFAULT 0", None),
            ("livraisons", "gallons_restants_cloture",
             "ALTER TABLE livraisons ADD COLUMN gallons_restants_cloture NUMERIC DEFAULT NULL", None),
            ("livraisons", "date_cloture",
             "ALTER TABLE livraisons ADD COLUMN date_cloture DATETIME DEFAULT NULL", None),
            ("livraisons", "utilisateur_cloture_id",
             "ALTER TABLE livraisons ADD COLUMN utilisateur_cloture_id INTEGER DEFAULT NULL", None),
            ("livraisons", "report_vers_livraison_id",
             "ALTER TABLE livraisons ADD COLUMN report_vers_livraison_id INTEGER DEFAULT NULL", None),
            # v9 — rapport de vente figé par cargaison (historique de vente)
            ("livraisons", "rapport_gallons_vendus",
             "ALTER TABLE livraisons ADD COLUMN rapport_gallons_vendus NUMERIC DEFAULT NULL", None),
            ("livraisons", "rapport_revenu",
             "ALTER TABLE livraisons ADD COLUMN rapport_revenu NUMERIC DEFAULT NULL", None),
            # v10 — reste avant saisi manuellement (compté dans le stock agrégé)
            ("livraisons", "gallons_reste_manuel",
             "ALTER TABLE livraisons ADD COLUMN gallons_reste_manuel NUMERIC DEFAULT 0", None),
            # v11 — téléphone utilisateur (second canal OTP par SMS)
            ("utilisateurs", "telephone",
             "ALTER TABLE utilisateurs ADD COLUMN telephone VARCHAR(20)", None),
            # v12 — caisse d'origine (Gazoline/Diesel) d'une dépense
            ("depenses", "produit_id",
             "ALTER TABLE depenses ADD COLUMN produit_id INTEGER REFERENCES produits(id)",
             "CREATE INDEX IF NOT EXISTS idx_depenses_produit ON depenses(produit_id)"),
            # v13 — taux HTG/USD fige a la saisie d'une depense Zelle
            ("zelle_depenses", "taux_applique",
             "ALTER TABLE zelle_depenses ADD COLUMN taux_applique NUMERIC NOT NULL DEFAULT 130", None),
            # v14 — statut resolu (global) d'une notification
            ("notifications", "resolu",
             "ALTER TABLE notifications ADD COLUMN resolu INTEGER NOT NULL DEFAULT 0",
             "CREATE INDEX IF NOT EXISTS idx_notif_resolu ON notifications(resolu)"),
            ("notifications", "resolu_par_id",
             "ALTER TABLE notifications ADD COLUMN resolu_par_id INTEGER REFERENCES utilisateurs(id)", None),
            ("notifications", "resolu_at",
             "ALTER TABLE notifications ADD COLUMN resolu_at DATETIME DEFAULT NULL", None),
            # v15 — éligibilité crédit client + lien client_id sur ventes/crédits bar
            ("clients", "statut_credit",
             "ALTER TABLE clients ADD COLUMN statut_credit VARCHAR(20) NOT NULL DEFAULT 'ELIGIBLE'", None),
            ("bar_ventes", "client_id",
             "ALTER TABLE bar_ventes ADD COLUMN client_id INTEGER REFERENCES clients(id)", None),
            ("bar_credits", "client_id",
             "ALTER TABLE bar_credits ADD COLUMN client_id INTEGER REFERENCES clients(id)", None),
            # v16 — photo illustrative d'un produit bar (base64, comme PieceJointe)
            ("bar_produits", "photo_base64",
             "ALTER TABLE bar_produits ADD COLUMN photo_base64 TEXT", None),
            ("bar_produits", "photo_mime",
             "ALTER TABLE bar_produits ADD COLUMN photo_mime VARCHAR(50)", None),
            # v17 — réconciliation cash à la soumission d'une session de caisse
            ("bar_sessions_caisse", "cash_attendu_soumission",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN cash_attendu_soumission NUMERIC", None),
            ("bar_sessions_caisse", "montant_compte",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN montant_compte NUMERIC", None),
            ("bar_sessions_caisse", "ecart",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN ecart NUMERIC", None),
            # v18 — évaluation produit par produit du rapport par un responsable
            ("bar_sessions_caisse", "evaluation_statut",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN evaluation_statut VARCHAR(20) DEFAULT 'NON_EVALUE'", None),
            ("bar_sessions_caisse", "score",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN score NUMERIC", None),
            ("bar_sessions_caisse", "evalue_par_id",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN evalue_par_id INTEGER REFERENCES utilisateurs(id)", None),
            ("bar_sessions_caisse", "evalue_le",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN evalue_le TIMESTAMP", None),
            # v19 — jusqu'à 2 sessions de caisse par jour par caissier (mesure de sécurité)
            ("bar_sessions_caisse", "numero_session",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN numero_session INTEGER NOT NULL DEFAULT 1", None),
            # v20 — lieu (Bar Devant / Bar Piscine) choisi au démarrage de session
            ("bar_sessions_caisse", "lieu",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN lieu VARCHAR(20)", None),
            # v21 — rattachement direct d'une vente à sa session de caisse
            # (corrige les rapports dupliqués entre sessions successives du
            # même jour — voir _ventes_session dans caisse_routes.py)
            ("bar_ventes", "session_id",
             "ALTER TABLE bar_ventes ADD COLUMN session_id INTEGER REFERENCES bar_sessions_caisse(id)",
             "CREATE INDEX IF NOT EXISTS idx_bar_ventes_session ON bar_ventes(session_id)"),
            # v22 — traçabilité : qui a saisi / modifié un relevé de compteur
            ("releves", "cree_par_id",
             "ALTER TABLE releves ADD COLUMN cree_par_id INTEGER REFERENCES utilisateurs(id)", None),
            ("releves", "modifie_par_id",
             "ALTER TABLE releves ADD COLUMN modifie_par_id INTEGER REFERENCES utilisateurs(id)", None),
            # v23 — pompiste (employé) attribué à un relevé, indépendamment du
            # compte de connexion utilisé pour la saisie
            ("releves", "pompiste_id",
             "ALTER TABLE releves ADD COLUMN pompiste_id INTEGER REFERENCES employes(id)", None),
            # v24 — lieu (Bar Devant / Bar Piscine) sur les mouvements de stock —
            # uniquement pour les ajustements manuels (AJUSTEMENT/PERTE/CASSE) ;
            # les achats/ventes non rattachées à une session restent NULL.
            ("bar_mouvements_stock", "lieu",
             "ALTER TABLE bar_mouvements_stock ADD COLUMN lieu VARCHAR(20)", None),
            # v25 — catalogue d'articles bar séparé par lieu (Bar Devant /
            # Bar Piscine) : NULL = article créé avant la séparation
            # (visible aux deux, jusqu'à réaffectation manuelle par
            # l'admin) ; exigé désormais à la création d'un nouvel article.
            ("bar_produits", "lieu",
             "ALTER TABLE bar_produits ADD COLUMN lieu VARCHAR(20)", None),
            # v26 — fondations module Commerce & Distribution (généralisation
            # du module Bar) : entrepôt structuré, fournisseur structuré,
            # limite de crédit client, prix gros/détail. Toutes nullable /
            # avec défaut inoffensif — aucun impact sur les tenants Bar
            # existants tant que ces colonnes ne sont pas renseignées.
            ("bar_achats", "fournisseur_id",
             "ALTER TABLE bar_achats ADD COLUMN fournisseur_id INTEGER REFERENCES fournisseurs(id)", None),
            ("bar_mouvements_stock", "entrepot_id",
             "ALTER TABLE bar_mouvements_stock ADD COLUMN entrepot_id INTEGER REFERENCES entrepots(id)", None),
            ("bar_sessions_caisse", "entrepot_id",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN entrepot_id INTEGER REFERENCES entrepots(id)", None),
            ("clients", "limite_credit",
             "ALTER TABLE clients ADD COLUMN limite_credit NUMERIC", None),
            ("bar_prix_historique", "type_prix",
             "ALTER TABLE bar_prix_historique ADD COLUMN type_prix VARCHAR(10) NOT NULL DEFAULT 'DETAIL'", None),
            # v27 — déclaration d'achat multi-articles (BarDeclarationAchat/
            # BarLigneDeclarationAchat, nouvelles tables via create_all) +
            # confirmation de réception par scan (rappel non bloquant).
            ("bar_achats", "declaration_id",
             "ALTER TABLE bar_achats ADD COLUMN declaration_id INTEGER REFERENCES bar_declarations_achat(id) ON DELETE SET NULL", None),
            ("bar_caisses", "confirmee",
             "ALTER TABLE bar_caisses ADD COLUMN confirmee INTEGER NOT NULL DEFAULT 0", None),
            ("bar_caisses", "confirmee_le",
             "ALTER TABLE bar_caisses ADD COLUMN confirmee_le DATETIME", None),
            ("bar_caisses", "confirmee_par_id",
             "ALTER TABLE bar_caisses ADD COLUMN confirmee_par_id INTEGER REFERENCES utilisateurs(id) ON DELETE SET NULL", None),
            ("bar_caisses", "declaration_id",
             "ALTER TABLE bar_caisses ADD COLUMN declaration_id INTEGER REFERENCES bar_declarations_achat(id) ON DELETE SET NULL", None),
            # v26 — logique métier carburant : continuité des compteurs
            ("releves", "ecart_compteur",
             "ALTER TABLE releves ADD COLUMN ecart_compteur NUMERIC DEFAULT NULL", None),
            ("releves", "motif_ecart_compteur",
             "ALTER TABLE releves ADD COLUMN motif_ecart_compteur VARCHAR(255) DEFAULT NULL", None),
            ("livraisons", "motif_reste_manuel",
             "ALTER TABLE livraisons ADD COLUMN motif_reste_manuel VARCHAR(255) DEFAULT NULL", None),
            ("livraisons", "motif_cloture",
             "ALTER TABLE livraisons ADD COLUMN motif_cloture VARCHAR(255) DEFAULT NULL", None),
            # Amorçage unique (exécuté seulement à l'ajout de la colonne) des
            # catégories qui sont des transferts d'argent, pas des charges.
            ("categories_depense", "est_transfert",
             "ALTER TABLE categories_depense ADD COLUMN est_transfert INTEGER NOT NULL DEFAULT 0",
             "UPDATE categories_depense SET est_transfert = 1 WHERE nom_norm LIKE '%banque%' OR nom_norm LIKE '%zelle%' OR nom_norm LIKE '%transfert%' OR nom_norm LIKE '%virement%'"),
            ("releves", "rapport_pompiste_id",
             "ALTER TABLE releves ADD COLUMN rapport_pompiste_id INTEGER REFERENCES rapports_pompiste(id) ON DELETE SET NULL", None),
            ("rapports_pompiste", "depenses",
             "ALTER TABLE rapports_pompiste ADD COLUMN depenses JSON NOT NULL DEFAULT '[]'", None),
            ("rapports_pompiste", "total_depenses",
             "ALTER TABLE rapports_pompiste ADD COLUMN total_depenses NUMERIC(14,2) NOT NULL DEFAULT 0", None),
            ("rapports_pompiste", "devises",
             "ALTER TABLE rapports_pompiste ADD COLUMN devises JSON NOT NULL DEFAULT '[]'", None),
            ("rapports_pompiste", "feuille",
             "ALTER TABLE rapports_pompiste ADD COLUMN feuille JSON DEFAULT NULL", None),
            ("rapports_pompiste_lignes", "pompe_saisie_id",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN pompe_saisie_id INTEGER DEFAULT NULL", None),
            ("rapports_pompiste_lignes", "statut_rapprochement",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN statut_rapprochement VARCHAR(20)", None),
            ("rapports_pompiste_lignes", "confiance",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN confiance VARCHAR(10)", None),
            ("rapports_pompiste_lignes", "score_rapprochement",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN score_rapprochement INTEGER", None),
            ("rapports_pompiste_lignes", "pompe_proposee_id",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN pompe_proposee_id INTEGER", None),
            ("rapports_pompiste_lignes", "index_reference",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN index_reference NUMERIC(14,4)", None),
            ("rapports_pompiste_lignes", "source_reference",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN source_reference VARCHAR(200)", None),
            ("rapports_pompiste_lignes", "ecart_reference",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN ecart_reference NUMERIC(14,4)", None),
            ("rapports_pompiste_lignes", "decision_pompiste",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN decision_pompiste VARCHAR(10)", None),
            ("rapports_pompiste_lignes", "anomalies",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN anomalies JSON", None),
            ("rapports_pompiste_lignes", "verifie_par_id",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN verifie_par_id INTEGER", None),
            ("rapports_pompiste_lignes", "verifie_at",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN verifie_at TIMESTAMP", None),
            ("rapports_pompiste_lignes", "note_verification",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN note_verification VARCHAR(500)", None),
            ("releves", "pompe_saisie_id",
             "ALTER TABLE releves ADD COLUMN pompe_saisie_id INTEGER DEFAULT NULL", None),
            ("releves", "metter_avant_saisi",
             "ALTER TABLE releves ADD COLUMN metter_avant_saisi NUMERIC DEFAULT NULL", None),
            ("releves", "metter_apres_saisi",
             "ALTER TABLE releves ADD COLUMN metter_apres_saisi NUMERIC DEFAULT NULL", None),
            ("rapports_pompiste", "supprime_at",
             "ALTER TABLE rapports_pompiste ADD COLUMN supprime_at DATETIME DEFAULT NULL", None),
            ("rapports_pompiste", "suppression",
             "ALTER TABLE rapports_pompiste ADD COLUMN suppression JSON DEFAULT NULL", None),
            ("produits", "adresse",
             "ALTER TABLE produits ADD COLUMN adresse VARCHAR(255) DEFAULT NULL", None),
            ("utilisateurs", "station_adresse",
             "ALTER TABLE utilisateurs ADD COLUMN station_adresse VARCHAR(255) DEFAULT NULL", None),
            ("utilisateurs", "compte_origine",
             "ALTER TABLE utilisateurs ADD COLUMN compte_origine VARCHAR(40) DEFAULT NULL", None),
            # Fiche station : contacts et gestion
            ("stations_geo", "responsable",
             "ALTER TABLE stations_geo ADD COLUMN responsable VARCHAR(150)", None),
            ("stations_geo", "telephone",
             "ALTER TABLE stations_geo ADD COLUMN telephone VARCHAR(30)", None),
            ("stations_geo", "email",
             "ALTER TABLE stations_geo ADD COLUMN email VARCHAR(254)", None),
            ("stations_geo", "urgence_nom",
             "ALTER TABLE stations_geo ADD COLUMN urgence_nom VARCHAR(150)", None),
            ("stations_geo", "urgence_telephone",
             "ALTER TABLE stations_geo ADD COLUMN urgence_telephone VARCHAR(30)", None),
            ("stations_geo", "horaires",
             "ALTER TABLE stations_geo ADD COLUMN horaires VARCHAR(120)", None),
            ("stations_geo", "note",
             "ALTER TABLE stations_geo ADD COLUMN note VARCHAR(500)", None),
            ("stations_geo", "maj_par",
             "ALTER TABLE stations_geo ADD COLUMN maj_par VARCHAR(150)", None),
            ("stations_geo", "maj_le",
             "ALTER TABLE stations_geo ADD COLUMN maj_le DATETIME", None),
            ("stations_geo", "nom",
             "ALTER TABLE stations_geo ADD COLUMN nom VARCHAR(150)", None),
            ("pompes", "face_id",
             "ALTER TABLE pompes ADD COLUMN face_id INTEGER REFERENCES faces(id) ON DELETE SET NULL", None),
            ("pompes", "metre_depart",
             "ALTER TABLE pompes ADD COLUMN metre_depart NUMERIC(14,4)", None),
            ("pompes", "date_depart",
             "ALTER TABLE pompes ADD COLUMN date_depart DATE", None),
            ("distributeurs", "adresse",
             "ALTER TABLE distributeurs ADD COLUMN adresse VARCHAR(255)", None),
            ("depenses", "rapport_pompiste_id",
             "ALTER TABLE depenses ADD COLUMN rapport_pompiste_id INTEGER REFERENCES rapports_pompiste(id) ON DELETE SET NULL", None),
            ("depenses", "saisi_par_id",
             "ALTER TABLE depenses ADD COLUMN saisi_par_id INTEGER REFERENCES utilisateurs(id) ON DELETE SET NULL", None),
            ("depenses", "pompiste_id",
             "ALTER TABLE depenses ADD COLUMN pompiste_id INTEGER REFERENCES employes(id) ON DELETE SET NULL", None),
            ("depenses", "dans_rapport_journalier",
             "ALTER TABLE depenses ADD COLUMN dans_rapport_journalier BOOLEAN", None),
            ("achats", "saisi_par_id",
             "ALTER TABLE achats ADD COLUMN saisi_par_id INTEGER REFERENCES utilisateurs(id) ON DELETE SET NULL", None),
        ]
    elif _is_postgres:
        new_cols = [
            # Lue dès init_db (amorçage des produits) : doit exister avant
            # les migrations de main.py
            ("produits", "adresse",
             "ALTER TABLE produits ADD COLUMN adresse VARCHAR(255)", None),
            ("utilisateurs", "station_adresse",
             "ALTER TABLE utilisateurs ADD COLUMN station_adresse VARCHAR(255)", None),
            ("utilisateurs", "compte_origine",
             "ALTER TABLE utilisateurs ADD COLUMN compte_origine VARCHAR(40)", None),
            # Fiche station : contacts et gestion
            ("stations_geo", "responsable",
             "ALTER TABLE stations_geo ADD COLUMN responsable VARCHAR(150)", None),
            ("stations_geo", "telephone",
             "ALTER TABLE stations_geo ADD COLUMN telephone VARCHAR(30)", None),
            ("stations_geo", "email",
             "ALTER TABLE stations_geo ADD COLUMN email VARCHAR(254)", None),
            ("stations_geo", "urgence_nom",
             "ALTER TABLE stations_geo ADD COLUMN urgence_nom VARCHAR(150)", None),
            ("stations_geo", "urgence_telephone",
             "ALTER TABLE stations_geo ADD COLUMN urgence_telephone VARCHAR(30)", None),
            ("stations_geo", "horaires",
             "ALTER TABLE stations_geo ADD COLUMN horaires VARCHAR(120)", None),
            ("stations_geo", "note",
             "ALTER TABLE stations_geo ADD COLUMN note VARCHAR(500)", None),
            ("stations_geo", "maj_par",
             "ALTER TABLE stations_geo ADD COLUMN maj_par VARCHAR(150)", None),
            ("stations_geo", "maj_le",
             "ALTER TABLE stations_geo ADD COLUMN maj_le TIMESTAMP WITH TIME ZONE", None),
            ("stations_geo", "nom",
             "ALTER TABLE stations_geo ADD COLUMN nom VARCHAR(150)", None),
            ("pompes", "face_id",
             "ALTER TABLE pompes ADD COLUMN face_id INTEGER REFERENCES faces(id) ON DELETE SET NULL", None),
            ("pompes", "metre_depart",
             "ALTER TABLE pompes ADD COLUMN metre_depart NUMERIC(14,4)", None),
            ("pompes", "date_depart",
             "ALTER TABLE pompes ADD COLUMN date_depart DATE", None),
            ("distributeurs", "adresse",
             "ALTER TABLE distributeurs ADD COLUMN adresse VARCHAR(255)", None),
            ("releves",      "nb_modifications",
             "ALTER TABLE releves      ADD COLUMN nb_modifications INTEGER NOT NULL DEFAULT 0", None),
            ("utilisateurs", "api_key_hash",
             "ALTER TABLE utilisateurs ADD COLUMN api_key_hash VARCHAR(64) UNIQUE", None),
            ("utilisateurs", "email",
             "ALTER TABLE utilisateurs ADD COLUMN email VARCHAR(254) UNIQUE", None),
            ("utilisateurs", "oauth_provider",
             "ALTER TABLE utilisateurs ADD COLUMN oauth_provider VARCHAR(32)", None),
            ("utilisateurs", "oauth_sub",
             "ALTER TABLE utilisateurs ADD COLUMN oauth_sub VARCHAR(255) UNIQUE", None),
            # v3 — code d'accès 9 chiffres
            ("utilisateurs", "code_acces_hash",
             "ALTER TABLE utilisateurs ADD COLUMN code_acces_hash VARCHAR(255)", None),
            # v5 — poste de l'employé (contrôle d'accès)
            ("utilisateurs", "poste",
             "ALTER TABLE utilisateurs ADD COLUMN poste VARCHAR(100)", None),
            # v6 — NIF client crédit bar
            ("bar_credits", "client_nif",
             "ALTER TABLE bar_credits ADD COLUMN client_nif VARCHAR(50)", None),
            # v7 — suivi activité sessions
            ("sessions", "last_activity_at",
             "ALTER TABLE sessions ADD COLUMN last_activity_at TIMESTAMP WITH TIME ZONE", None),
            # v8 — clôture de cargaison (suivi FIFO par livraison)
            ("livraisons", "terminee",
             "ALTER TABLE livraisons ADD COLUMN terminee BOOLEAN NOT NULL DEFAULT FALSE", None),
            ("livraisons", "gallons_report_recu",
             "ALTER TABLE livraisons ADD COLUMN gallons_report_recu NUMERIC(14,3) NOT NULL DEFAULT 0", None),
            ("livraisons", "gallons_restants_cloture",
             "ALTER TABLE livraisons ADD COLUMN gallons_restants_cloture NUMERIC(14,3)", None),
            ("livraisons", "date_cloture",
             "ALTER TABLE livraisons ADD COLUMN date_cloture TIMESTAMP WITH TIME ZONE", None),
            ("livraisons", "utilisateur_cloture_id",
             "ALTER TABLE livraisons ADD COLUMN utilisateur_cloture_id INTEGER REFERENCES utilisateurs(id) ON DELETE SET NULL", None),
            ("livraisons", "report_vers_livraison_id",
             "ALTER TABLE livraisons ADD COLUMN report_vers_livraison_id INTEGER REFERENCES livraisons(id) ON DELETE SET NULL", None),
            # v9 — rapport de vente figé par cargaison (historique de vente)
            ("livraisons", "rapport_gallons_vendus",
             "ALTER TABLE livraisons ADD COLUMN rapport_gallons_vendus NUMERIC(14,3)", None),
            ("livraisons", "rapport_revenu",
             "ALTER TABLE livraisons ADD COLUMN rapport_revenu NUMERIC(14,2)", None),
            # v10 — reste avant saisi manuellement (compté dans le stock agrégé)
            ("livraisons", "gallons_reste_manuel",
             "ALTER TABLE livraisons ADD COLUMN gallons_reste_manuel NUMERIC(14,3) NOT NULL DEFAULT 0", None),
            # v11 — téléphone utilisateur (second canal OTP par SMS)
            ("utilisateurs", "telephone",
             "ALTER TABLE utilisateurs ADD COLUMN telephone VARCHAR(20)", None),
            # v12 — caisse d'origine (Gazoline/Diesel) d'une dépense
            ("depenses", "produit_id",
             "ALTER TABLE depenses ADD COLUMN produit_id INTEGER REFERENCES produits(id) ON DELETE SET NULL",
             "CREATE INDEX IF NOT EXISTS idx_depenses_produit ON depenses(produit_id)"),
            # v13 — taux HTG/USD fige a la saisie d'une depense Zelle
            ("zelle_depenses", "taux_applique",
             "ALTER TABLE zelle_depenses ADD COLUMN taux_applique NUMERIC(10,4) NOT NULL DEFAULT 130", None),
            # v14 — statut resolu (global) d'une notification
            ("notifications", "resolu",
             "ALTER TABLE notifications ADD COLUMN resolu BOOLEAN NOT NULL DEFAULT FALSE",
             "CREATE INDEX IF NOT EXISTS idx_notif_resolu ON notifications(resolu)"),
            ("notifications", "resolu_par_id",
             "ALTER TABLE notifications ADD COLUMN resolu_par_id INTEGER REFERENCES utilisateurs(id) ON DELETE SET NULL", None),
            ("notifications", "resolu_at",
             "ALTER TABLE notifications ADD COLUMN resolu_at TIMESTAMP WITH TIME ZONE", None),
            # v15 — éligibilité crédit client + lien client_id sur ventes/crédits bar
            ("clients", "statut_credit",
             "ALTER TABLE clients ADD COLUMN statut_credit VARCHAR(20) NOT NULL DEFAULT 'ELIGIBLE'", None),
            ("bar_ventes", "client_id",
             "ALTER TABLE bar_ventes ADD COLUMN client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL", None),
            ("bar_credits", "client_id",
             "ALTER TABLE bar_credits ADD COLUMN client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL", None),
            # v16 — photo illustrative d'un produit bar (base64, comme PieceJointe)
            ("bar_produits", "photo_base64",
             "ALTER TABLE bar_produits ADD COLUMN photo_base64 TEXT", None),
            ("bar_produits", "photo_mime",
             "ALTER TABLE bar_produits ADD COLUMN photo_mime VARCHAR(50)", None),
            # v17 — réconciliation cash à la soumission d'une session de caisse
            ("bar_sessions_caisse", "cash_attendu_soumission",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN cash_attendu_soumission NUMERIC(14,2)", None),
            ("bar_sessions_caisse", "montant_compte",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN montant_compte NUMERIC(14,2)", None),
            ("bar_sessions_caisse", "ecart",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN ecart NUMERIC(14,2)", None),
            # v18 — évaluation produit par produit du rapport par un responsable
            ("bar_sessions_caisse", "evaluation_statut",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN evaluation_statut VARCHAR(20) DEFAULT 'NON_EVALUE'", None),
            ("bar_sessions_caisse", "score",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN score NUMERIC(5,2)", None),
            ("bar_sessions_caisse", "evalue_par_id",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN evalue_par_id INTEGER REFERENCES utilisateurs(id)", None),
            ("bar_sessions_caisse", "evalue_le",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN evalue_le TIMESTAMP WITH TIME ZONE", None),
            # v19 — jusqu'à 2 sessions de caisse par jour par caissier (mesure de sécurité)
            ("bar_sessions_caisse", "numero_session",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN numero_session INTEGER NOT NULL DEFAULT 1", None),
            # v20 — lieu (Bar Devant / Bar Piscine) choisi au démarrage de session
            ("bar_sessions_caisse", "lieu",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN lieu VARCHAR(20)", None),
            # v21 — rattachement direct d'une vente à sa session de caisse
            # (corrige les rapports dupliqués entre sessions successives du
            # même jour — voir _ventes_session dans caisse_routes.py)
            ("bar_ventes", "session_id",
             "ALTER TABLE bar_ventes ADD COLUMN session_id INTEGER REFERENCES bar_sessions_caisse(id) ON DELETE SET NULL",
             "CREATE INDEX IF NOT EXISTS idx_bar_ventes_session ON bar_ventes(session_id)"),
            # v22 — traçabilité : qui a saisi / modifié un relevé de compteur
            ("releves", "cree_par_id",
             "ALTER TABLE releves ADD COLUMN cree_par_id INTEGER REFERENCES utilisateurs(id) ON DELETE SET NULL", None),
            ("releves", "modifie_par_id",
             "ALTER TABLE releves ADD COLUMN modifie_par_id INTEGER REFERENCES utilisateurs(id) ON DELETE SET NULL", None),
            # v23 — pompiste (employé) attribué à un relevé, indépendamment du
            # compte de connexion utilisé pour la saisie
            ("releves", "pompiste_id",
             "ALTER TABLE releves ADD COLUMN pompiste_id INTEGER REFERENCES employes(id) ON DELETE SET NULL", None),
            # v24 — lieu (Bar Devant / Bar Piscine) sur les mouvements de stock —
            # uniquement pour les ajustements manuels (AJUSTEMENT/PERTE/CASSE) ;
            # les achats/ventes non rattachées à une session restent NULL.
            ("bar_mouvements_stock", "lieu",
             "ALTER TABLE bar_mouvements_stock ADD COLUMN lieu VARCHAR(20)", None),
            # v25 — catalogue d'articles bar séparé par lieu (Bar Devant /
            # Bar Piscine) : NULL = article créé avant la séparation
            # (visible aux deux, jusqu'à réaffectation manuelle par
            # l'admin) ; exigé désormais à la création d'un nouvel article.
            ("bar_produits", "lieu",
             "ALTER TABLE bar_produits ADD COLUMN lieu VARCHAR(20)", None),
            # v26 — fondations module Commerce & Distribution (généralisation
            # du module Bar) : entrepôt structuré, fournisseur structuré,
            # limite de crédit client, prix gros/détail. Toutes nullable /
            # avec défaut inoffensif — aucun impact sur les tenants Bar
            # existants tant que ces colonnes ne sont pas renseignées.
            ("bar_achats", "fournisseur_id",
             "ALTER TABLE bar_achats ADD COLUMN fournisseur_id INTEGER REFERENCES fournisseurs(id) ON DELETE SET NULL", None),
            ("bar_mouvements_stock", "entrepot_id",
             "ALTER TABLE bar_mouvements_stock ADD COLUMN entrepot_id INTEGER REFERENCES entrepots(id) ON DELETE SET NULL", None),
            ("bar_sessions_caisse", "entrepot_id",
             "ALTER TABLE bar_sessions_caisse ADD COLUMN entrepot_id INTEGER REFERENCES entrepots(id) ON DELETE SET NULL", None),
            ("clients", "limite_credit",
             "ALTER TABLE clients ADD COLUMN limite_credit NUMERIC", None),
            ("bar_prix_historique", "type_prix",
             "ALTER TABLE bar_prix_historique ADD COLUMN type_prix VARCHAR(10) NOT NULL DEFAULT 'DETAIL'", None),
            # v27 — déclaration d'achat multi-articles (BarDeclarationAchat/
            # BarLigneDeclarationAchat, nouvelles tables via create_all) +
            # confirmation de réception par scan (rappel non bloquant).
            ("bar_achats", "declaration_id",
             "ALTER TABLE bar_achats ADD COLUMN declaration_id INTEGER REFERENCES bar_declarations_achat(id) ON DELETE SET NULL", None),
            ("bar_caisses", "confirmee",
             "ALTER TABLE bar_caisses ADD COLUMN confirmee BOOLEAN NOT NULL DEFAULT FALSE", None),
            ("bar_caisses", "confirmee_le",
             "ALTER TABLE bar_caisses ADD COLUMN confirmee_le TIMESTAMP WITH TIME ZONE", None),
            ("bar_caisses", "confirmee_par_id",
             "ALTER TABLE bar_caisses ADD COLUMN confirmee_par_id INTEGER REFERENCES utilisateurs(id) ON DELETE SET NULL", None),
            ("bar_caisses", "declaration_id",
             "ALTER TABLE bar_caisses ADD COLUMN declaration_id INTEGER REFERENCES bar_declarations_achat(id) ON DELETE SET NULL", None),
            # v26 — logique métier carburant : continuité des compteurs
            ("releves", "ecart_compteur",
             "ALTER TABLE releves ADD COLUMN ecart_compteur NUMERIC(14,4)", None),
            ("releves", "motif_ecart_compteur",
             "ALTER TABLE releves ADD COLUMN motif_ecart_compteur VARCHAR(255)", None),
            ("releves", "pompe_saisie_id",
             "ALTER TABLE releves ADD COLUMN pompe_saisie_id INTEGER REFERENCES pompes(id) ON DELETE SET NULL", None),
            ("releves", "metter_avant_saisi",
             "ALTER TABLE releves ADD COLUMN metter_avant_saisi NUMERIC(14,4)", None),
            ("releves", "metter_apres_saisi",
             "ALTER TABLE releves ADD COLUMN metter_apres_saisi NUMERIC(14,4)", None),
            ("depenses", "pompiste_id",
             "ALTER TABLE depenses ADD COLUMN pompiste_id INTEGER REFERENCES employes(id) ON DELETE SET NULL", None),
            ("depenses", "dans_rapport_journalier",
             "ALTER TABLE depenses ADD COLUMN dans_rapport_journalier BOOLEAN", None),
            ("rapports_pompiste_lignes", "statut_rapprochement",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN statut_rapprochement VARCHAR(20)", None),
            ("rapports_pompiste_lignes", "confiance",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN confiance VARCHAR(10)", None),
            ("rapports_pompiste_lignes", "score_rapprochement",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN score_rapprochement INTEGER", None),
            ("rapports_pompiste_lignes", "pompe_proposee_id",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN pompe_proposee_id INTEGER REFERENCES pompes(id) ON DELETE SET NULL", None),
            ("rapports_pompiste_lignes", "index_reference",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN index_reference NUMERIC(14,4)", None),
            ("rapports_pompiste_lignes", "source_reference",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN source_reference VARCHAR(200)", None),
            ("rapports_pompiste_lignes", "ecart_reference",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN ecart_reference NUMERIC(14,4)", None),
            ("rapports_pompiste_lignes", "decision_pompiste",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN decision_pompiste VARCHAR(10)", None),
            ("rapports_pompiste_lignes", "anomalies",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN anomalies JSON", None),
            ("rapports_pompiste_lignes", "verifie_par_id",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN verifie_par_id INTEGER REFERENCES utilisateurs(id) ON DELETE SET NULL", None),
            ("rapports_pompiste_lignes", "verifie_at",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN verifie_at TIMESTAMPTZ", None),
            ("rapports_pompiste_lignes", "note_verification",
             "ALTER TABLE rapports_pompiste_lignes ADD COLUMN note_verification VARCHAR(500)", None),
            ("livraisons", "motif_reste_manuel",
             "ALTER TABLE livraisons ADD COLUMN motif_reste_manuel VARCHAR(255)", None),
            ("livraisons", "motif_cloture",
             "ALTER TABLE livraisons ADD COLUMN motif_cloture VARCHAR(255)", None),
            # Amorçage unique (exécuté seulement à l'ajout de la colonne) des
            # catégories qui sont des transferts d'argent, pas des charges.
            ("categories_depense", "est_transfert",
             "ALTER TABLE categories_depense ADD COLUMN est_transfert BOOLEAN NOT NULL DEFAULT FALSE",
             "UPDATE categories_depense SET est_transfert = TRUE WHERE nom_norm LIKE '%banque%' OR nom_norm LIKE '%zelle%' OR nom_norm LIKE '%transfert%' OR nom_norm LIKE '%virement%'"),
        ]
    else:
        return

    # Ajout des colonnes manquantes — commité tout de suite, dans SA PROPRE
    # connexion/transaction, avant de toucher aux blocs de contraintes
    # ci-dessous. CRITIQUE : ne jamais partager une connexion/transaction
    # entre cet ajout de colonnes et les blocs try/except suivants — un
    # conn.rollback() annule TOUTE la transaction en cours sur cette
    # connexion, pas seulement l'instruction qui a échoué. Ça a réellement
    # empêché l'ajout de bar_ventes.session_id (v21) de survivre en
    # production : le rollback d'un bloc de contrainte plus bas (qui échoue
    # normalement à chaque redémarrage, la contrainte étant déjà en place)
    # a silencieusement effacé la colonne pourtant déjà ajoutée avec succès
    # dans la même connexion, juste avant le commit final.
    with engine.connect() as conn:
        for table, col, ddl_col, ddl_idx in new_cols:
            existing = [c["name"] for c in insp.get_columns(table)]
            if col not in existing:
                conn.execute(sql_text(ddl_col))
                if ddl_idx:
                    conn.execute(sql_text(ddl_idx))
        conn.commit()

    # Contraintes Postgres — chaque bloc est idempotent (relancé à chaque
    # démarrage) mais échoue normalement sur un redéploiement (contrainte
    # déjà en place) ; il obtient donc SA PROPRE connexion/transaction,
    # pour qu'un rollback ici ne puisse jamais annuler le commit des
    # colonnes ci-dessus, ni le travail réussi d'un autre bloc.
    if _is_postgres:
        with engine.connect() as conn:
            try:
                conn.execute(sql_text(
                    "ALTER TABLE utilisateurs DROP CONSTRAINT IF EXISTS chk_utilisateur_role"
                ))
                conn.execute(sql_text(
                    "ALTER TABLE utilisateurs ADD CONSTRAINT chk_utilisateur_role "
                    "CHECK (role IN ('admin', 'operateur', 'pdg'))"
                ))
                conn.commit()
            except Exception:
                conn.rollback()  # contrainte déjà à jour

        # v16 — précision des relevés de compteur élargie de 3 à 4 décimales
        # (élargissement sans perte : les valeurs existantes restent valides)
        with engine.connect() as conn:
            try:
                conn.execute(sql_text(
                    "ALTER TABLE releves ALTER COLUMN metter_avant TYPE NUMERIC(14,4)"
                ))
                conn.execute(sql_text(
                    "ALTER TABLE releves ALTER COLUMN metter_apres TYPE NUMERIC(14,4)"
                ))
                conn.commit()
            except Exception:
                conn.rollback()  # déjà à la bonne précision

        # v19 — jusqu'à 2 sessions de caisse par jour par caissier : la
        # contrainte d'unicité passe de (caissier_id, date_session) à
        # (caissier_id, date_session, numero_session).
        with engine.connect() as conn:
            try:
                conn.execute(sql_text(
                    "ALTER TABLE bar_sessions_caisse DROP CONSTRAINT IF EXISTS uq_session_caissier_date"
                ))
                conn.execute(sql_text(
                    "ALTER TABLE bar_sessions_caisse ADD CONSTRAINT uq_session_caissier_date_num "
                    "UNIQUE (caissier_id, date_session, numero_session)"
                ))
                conn.commit()
            except Exception:
                conn.rollback()  # contrainte déjà à jour

        # v20 — lieu (Bar Devant / Bar Piscine) : contrainte de domaine
        with engine.connect() as conn:
            try:
                conn.execute(sql_text(
                    "ALTER TABLE bar_sessions_caisse ADD CONSTRAINT chk_session_lieu "
                    "CHECK (lieu IS NULL OR lieu IN ('DEVANT','PISCINE'))"
                ))
                conn.commit()
            except Exception:
                conn.rollback()  # déjà présente

        # v24 — lieu (Bar Devant / Bar Piscine) sur les mouvements de stock :
        # même contrainte de domaine que chk_session_lieu.
        with engine.connect() as conn:
            try:
                conn.execute(sql_text(
                    "ALTER TABLE bar_mouvements_stock ADD CONSTRAINT chk_bar_mouv_lieu "
                    "CHECK (lieu IS NULL OR lieu IN ('DEVANT','PISCINE'))"
                ))
                conn.commit()
            except Exception:
                conn.rollback()  # déjà présente

        # v25 — lieu (Bar Devant / Bar Piscine) sur le catalogue d'articles bar :
        # même contrainte de domaine que chk_session_lieu / chk_bar_mouv_lieu.
        with engine.connect() as conn:
            try:
                conn.execute(sql_text(
                    "ALTER TABLE bar_produits ADD CONSTRAINT chk_bar_produit_lieu "
                    "CHECK (lieu IS NULL OR lieu IN ('DEVANT','PISCINE'))"
                ))
                conn.commit()
            except Exception:
                conn.rollback()  # déjà présente

        # v26 — module Commerce & Distribution : contraintes sur les colonnes
        # ajoutées à des tables existantes (entrepots/fournisseurs/
        # bottle_movements/crate_movements sont neuves — leurs contraintes
        # sont déjà posées par create_all(), pas besoin d'ALTER ici).
        with engine.connect() as conn:
            try:
                conn.execute(sql_text(
                    "ALTER TABLE bar_prix_historique ADD CONSTRAINT chk_bar_prix_type "
                    "CHECK (type_prix IN ('DETAIL','GROS'))"
                ))
                conn.commit()
            except Exception:
                conn.rollback()  # déjà présente

        with engine.connect() as conn:
            try:
                conn.execute(sql_text(
                    "ALTER TABLE clients ADD CONSTRAINT chk_client_limite_credit_pos "
                    "CHECK (limite_credit IS NULL OR limite_credit >= 0)"
                ))
                conn.commit()
            except Exception:
                conn.rollback()  # déjà présente

        # v23 — module Pâtisserie ajouté à la liste des départements pouvant
        # recevoir un renflouement manuel de caisse.
        with engine.connect() as conn:
            try:
                conn.execute(sql_text(
                    "ALTER TABLE renflouements_departement DROP CONSTRAINT IF EXISTS chk_renfl_dept_departement"
                ))
                conn.execute(sql_text(
                    "ALTER TABLE renflouements_departement ADD CONSTRAINT chk_renfl_dept_departement "
                    "CHECK (departement IN ('HOTEL','CUISINE','BAR','PATISSERIE'))"
                ))
                conn.commit()
            except Exception:
                conn.rollback()  # contrainte déjà à jour

        # v27 — CONFIRMATION ajoutée aux types de mouvement de caisse
        # autorisés (scan de confirmation de réception, déclaration d'achat
        # multi-articles) — élargissement sans perte, mêmes valeurs existantes
        # restent valides.
        with engine.connect() as conn:
            try:
                conn.execute(sql_text(
                    "ALTER TABLE bar_caisse_mouvements DROP CONSTRAINT IF EXISTS chk_bar_caisse_mouv_type"
                ))
                conn.execute(sql_text(
                    "ALTER TABLE bar_caisse_mouvements ADD CONSTRAINT chk_bar_caisse_mouv_type "
                    "CHECK (type_mouvement IN ('TRANSFERT','VENTE','CASSE','PERTE','CORRECTION','ANNULATION','CONFIRMATION'))"
                ))
                conn.commit()
            except Exception:
                conn.rollback()  # contrainte déjà à jour


# ── Initialisation du schéma + données de démarrage ──────────────
def init_db():
    """
    Crée les tables si elles n'existent pas et insère les données initiales.
    Idempotent : peut être appelé plusieurs fois sans effet de bord.
    """
    Base.metadata.create_all(bind=engine)
    _migrate_columns()          # ajoute les colonnes manquantes sur SQLite

    db = SessionLocal()
    try:
        if db.query(Produit).count() == 0:
            gaz = Produit(nom="Gazoline", prix_gallon=900)
            die = Produit(nom="Diesel",   prix_gallon=1000)
            db.add_all([gaz, die])
            db.flush()
            for i in (1, 2):
                db.add(Pompe(produit_id=gaz.id, nom=f"Gazoline {i}"))
                db.add(Pompe(produit_id=die.id, nom=f"Diesel {i}"))
            db.commit()

        if db.query(Utilisateur).count() == 0:
            from auth import hash_password, hash_code_acces
            admin = Utilisateur(
                username="admin",
                password_hash=hash_password("admin123"),
                code_acces_hash=hash_code_acces("123456789"),
                nom_complet="Administrateur",
                role="admin",
                email="admin@konekta.local",
            )
            db.add(admin)
            db.commit()

        if db.query(PatisserieEtapeSuivi).count() == 0:
            db.add_all([
                PatisserieEtapeSuivi(code="recue",        libelle="Reçue",           ordre=1, couleur="#64748b", est_initiale=True),
                PatisserieEtapeSuivi(code="preparation",  libelle="En préparation",  ordre=2, couleur="#f59e0b"),
                PatisserieEtapeSuivi(code="prete",        libelle="Prête",           ordre=3, couleur="#3b82f6"),
                PatisserieEtapeSuivi(code="livree",       libelle="Livrée",          ordre=4, couleur="#22c55e", est_finale=True),
            ])
            db.commit()
    finally:
        db.close()

    _ensure_bootstrap_admin()


def _ensure_bootstrap_admin():
    """
    Amorce un compte admin depuis des variables d'environnement, sans accès
    direct à la base. Actif seulement si BOOTSTRAP_ADMIN_EMAIL et
    BOOTSTRAP_ADMIN_PASSWORD sont définis.

    Idempotent : si un compte porte déjà cet email, on le met à jour (mot de
    passe + PIN fournis, rôle admin, actif) plutôt que d'en recréer un — sert
    aussi à réinitialiser le mot de passe d'un admin existant sans accès
    direct à la base. Ne logue jamais les secrets.

    Bloc temporaire : retirer les variables (et ce code) une fois l'amorçage
    terminé.
    """
    email    = (os.environ.get("BOOTSTRAP_ADMIN_EMAIL") or "").strip().lower()
    password =  os.environ.get("BOOTSTRAP_ADMIN_PASSWORD") or ""
    pin      = (os.environ.get("BOOTSTRAP_ADMIN_PIN") or "").strip()
    if not email or not password:
        return

    from auth import hash_password, hash_code_acces
    db = SessionLocal()
    try:
        existing = db.query(Utilisateur).filter(Utilisateur.email.ilike(email)).first()
        if existing:
            existing.password_hash = hash_password(password)   # réinitialisation demandée
            if existing.role != "admin":
                existing.role = "admin"
            if not existing.actif:
                existing.actif = True
            if pin:
                existing.code_acces_hash = hash_code_acces(pin)
            db.commit()
            print(f"[bootstrap-admin] compte existant mis a jour (mot de passe reinitialise) pour {email}", flush=True)
            return

        base = (os.environ.get("BOOTSTRAP_ADMIN_USERNAME") or email.split("@", 1)[0])[:74] or "admin"
        username, n = base, 1
        while db.query(Utilisateur).filter_by(username=username).first():
            n += 1
            username = f"{base}-{n}"

        db.add(Utilisateur(
            username=username,
            password_hash=hash_password(password),
            code_acces_hash=hash_code_acces(pin) if pin else None,
            nom_complet=os.environ.get("BOOTSTRAP_ADMIN_NAME", "Administrateur"),
            role="admin",
            email=email,
            actif=True,
        ))
        db.commit()
        pin_note = "" if pin else " (SANS code PIN — connexion impossible tant qu'un PIN n'est pas defini)"
        print(f"[bootstrap-admin] compte admin cree : username={username} email={email}{pin_note}", flush=True)
    except Exception as exc:
        db.rollback()
        print(f"[bootstrap-admin] echec : {type(exc).__name__}: {exc}", flush=True)
    finally:
        db.close()
