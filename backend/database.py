"""
Connexion à la base de données.

Variables d'environnement (fichier .env ou système) :
  DATABASE_URL=postgresql+psycopg2://user:password@host:5432/dbname
  (Fallback SQLite pour développement local sans PostgreSQL)
"""
import os
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
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
    IdempotencyKey,      # noqa: F401 — nécessaire pour create_all
    BarSessionEvaluation,  # noqa: F401 — nécessaire pour create_all
    PatisserieCategorie, PatisserieProduit, PatisserieAchat,        # noqa: F401
    PatisserieMouvementStock, PatisserieSessionCaisse,              # noqa: F401
    PatisserieSessionEvaluation,                                    # noqa: F401 — nécessaire pour create_all
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

    # Active les clés étrangères sur SQLite (désactivées par défaut)
    @event.listens_for(engine, "connect")
    def _sqlite_fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

else:
    raise ValueError(f"DATABASE_URL non supportée : {DATABASE_URL}")

# ── Session factory ───────────────────────────────────────────────
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """Dépendance FastAPI : fournit une session et la ferme automatiquement."""
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
            # v26 — évaluation de rapport (parité avec Bar) sur les sessions
            # de caisse pâtisserie : mêmes colonnes que bar_sessions_caisse.
            ("patisserie_sessions_caisse", "evaluation_statut",
             "ALTER TABLE patisserie_sessions_caisse ADD COLUMN evaluation_statut VARCHAR(20) NOT NULL DEFAULT 'NON_EVALUE'", None),
            ("patisserie_sessions_caisse", "score",
             "ALTER TABLE patisserie_sessions_caisse ADD COLUMN score NUMERIC(5,2)", None),
            ("patisserie_sessions_caisse", "evalue_par_id",
             "ALTER TABLE patisserie_sessions_caisse ADD COLUMN evalue_par_id INTEGER REFERENCES utilisateurs(id)", None),
            ("patisserie_sessions_caisse", "evalue_le",
             "ALTER TABLE patisserie_sessions_caisse ADD COLUMN evalue_le TIMESTAMP", None),
            # v27 — Piscine, département séparé du Bar : département
            # (DEVANT / PISCINE) sur ventes, achats et paie bar.
            ("bar_ventes", "lieu",
             "ALTER TABLE bar_ventes ADD COLUMN lieu VARCHAR(20)",
             "CREATE INDEX IF NOT EXISTS idx_bar_ventes_lieu ON bar_ventes(lieu)"),
            ("bar_achats", "lieu",
             "ALTER TABLE bar_achats ADD COLUMN lieu VARCHAR(20)", None),
            ("bar_paiements_employes", "lieu",
             "ALTER TABLE bar_paiements_employes ADD COLUMN lieu VARCHAR(20)", None),
        ]
    elif _is_postgres:
        new_cols = [
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
            # v26 — évaluation de rapport (parité avec Bar) sur les sessions
            # de caisse pâtisserie : mêmes colonnes que bar_sessions_caisse.
            ("patisserie_sessions_caisse", "evaluation_statut",
             "ALTER TABLE patisserie_sessions_caisse ADD COLUMN evaluation_statut VARCHAR(20) NOT NULL DEFAULT 'NON_EVALUE'", None),
            ("patisserie_sessions_caisse", "score",
             "ALTER TABLE patisserie_sessions_caisse ADD COLUMN score NUMERIC(5,2)", None),
            ("patisserie_sessions_caisse", "evalue_par_id",
             "ALTER TABLE patisserie_sessions_caisse ADD COLUMN evalue_par_id INTEGER REFERENCES utilisateurs(id) ON DELETE SET NULL", None),
            ("patisserie_sessions_caisse", "evalue_le",
             "ALTER TABLE patisserie_sessions_caisse ADD COLUMN evalue_le TIMESTAMP WITH TIME ZONE", None),
            # v27 — Piscine, département séparé du Bar : département
            # (DEVANT / PISCINE) sur ventes, achats et paie bar.
            ("bar_ventes", "lieu",
             "ALTER TABLE bar_ventes ADD COLUMN lieu VARCHAR(20)",
             "CREATE INDEX IF NOT EXISTS idx_bar_ventes_lieu ON bar_ventes(lieu)"),
            ("bar_achats", "lieu",
             "ALTER TABLE bar_achats ADD COLUMN lieu VARCHAR(20)", None),
            ("bar_paiements_employes", "lieu",
             "ALTER TABLE bar_paiements_employes ADD COLUMN lieu VARCHAR(20)", None),
        ]
    else:
        return

    # v28 — devise réellement reçue (HTG / USD) sur toutes les ventes
    # encaissées (voir devise.py), quel que soit le moteur.
    _num14 = "NUMERIC" if _is_sqlite else "NUMERIC(14,2)"
    _num10 = "NUMERIC" if _is_sqlite else "NUMERIC(10,4)"
    # v29 — article géré (ou non) au Dépôt central
    new_cols.append(("bar_produits", "depot_actif",
                     f"ALTER TABLE bar_produits ADD COLUMN depot_actif {'INTEGER' if _is_sqlite else 'BOOLEAN'}", None))
    # v30 — prix des chambres en dollars et tarif du séjour en USD
    new_cols.append(("hotel_chambres", "prix_nuit_usd", "ALTER TABLE hotel_chambres ADD COLUMN prix_nuit_usd NUMERIC(12,2)", None))
    new_cols.append(("hotel_chambres", "prix_moment_usd", "ALTER TABLE hotel_chambres ADD COLUMN prix_moment_usd NUMERIC(12,2)", None))
    new_cols.append(("hotel_reservations", "devise_tarif", "ALTER TABLE hotel_reservations ADD COLUMN devise_tarif VARCHAR(3)", None))
    new_cols.append(("hotel_reservations", "prix_unitaire_usd", "ALTER TABLE hotel_reservations ADD COLUMN prix_unitaire_usd NUMERIC(12,2)", None))
    new_cols.append(("hotel_reservations", "montant_total_usd", "ALTER TABLE hotel_reservations ADD COLUMN montant_total_usd NUMERIC(12,2)", None))
    new_cols.append(("hotel_reservations", "taux_tarif", "ALTER TABLE hotel_reservations ADD COLUMN taux_tarif NUMERIC(10,4)", None))
    # v31 — dépôt : « Démarrer » par article, « reçu par » sur les transferts
    new_cols.append(("bar_produits", "depot_demarre_le",
                     f"ALTER TABLE bar_produits ADD COLUMN depot_demarre_le {'DATETIME' if _is_sqlite else 'TIMESTAMP WITH TIME ZONE'}", None))
    new_cols.append(("depot_transactions", "recu_par", "ALTER TABLE depot_transactions ADD COLUMN recu_par VARCHAR(100)", None))
    # v32 — Piscine : cartes Adulte / Jeune / Enfant, correction par l'admin
    new_cols.append(("piscine_config", "prix_adulte", "ALTER TABLE piscine_config ADD COLUMN prix_adulte NUMERIC(12,2)", None))
    new_cols.append(("piscine_config", "prix_jeune", "ALTER TABLE piscine_config ADD COLUMN prix_jeune NUMERIC(12,2)", None))
    new_cols.append(("piscine_config", "prix_enfant", "ALTER TABLE piscine_config ADD COLUMN prix_enfant NUMERIC(12,2)", None))
    new_cols.append(("piscine_ventes", "qte_adulte", "ALTER TABLE piscine_ventes ADD COLUMN qte_adulte INTEGER", None))
    new_cols.append(("piscine_ventes", "qte_jeune", "ALTER TABLE piscine_ventes ADD COLUMN qte_jeune INTEGER", None))
    new_cols.append(("piscine_ventes", "qte_enfant", "ALTER TABLE piscine_ventes ADD COLUMN qte_enfant INTEGER", None))
    new_cols.append(("piscine_ventes", "prix_adulte", "ALTER TABLE piscine_ventes ADD COLUMN prix_adulte NUMERIC(12,2)", None))
    new_cols.append(("piscine_ventes", "prix_jeune", "ALTER TABLE piscine_ventes ADD COLUMN prix_jeune NUMERIC(12,2)", None))
    new_cols.append(("piscine_ventes", "prix_enfant", "ALTER TABLE piscine_ventes ADD COLUMN prix_enfant NUMERIC(12,2)", None))
    new_cols.append(("piscine_ventes", "corrige_par", "ALTER TABLE piscine_ventes ADD COLUMN corrige_par VARCHAR(150)", None))
    new_cols.append(("piscine_ventes", "motif_correction", "ALTER TABLE piscine_ventes ADD COLUMN motif_correction VARCHAR(300)", None))
    new_cols.append(("piscine_ventes", "corrige_le",
                     f"ALTER TABLE piscine_ventes ADD COLUMN corrige_le {'DATETIME' if _is_sqlite else 'TIMESTAMP WITH TIME ZONE'}", None))
    for _t in ("bar_ventes", "bar_remboursements", "cuisine_ventes", "patisserie_ventes", "piscine_ventes"):
        new_cols += [
            (_t, "devise",         f"ALTER TABLE {_t} ADD COLUMN devise VARCHAR(3) NOT NULL DEFAULT 'HTG'", None),
            (_t, "montant_devise", f"ALTER TABLE {_t} ADD COLUMN montant_devise {_num14}", None),
            (_t, "taux_change",    f"ALTER TABLE {_t} ADD COLUMN taux_change {_num10}", None),
        ]

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
        tables_existantes = set(insp.get_table_names())
        for table, col, ddl_col, ddl_idx in new_cols:
            if table not in tables_existantes:
                continue
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

        # v26 — évaluation de rapport sur les sessions de caisse pâtisserie :
        # même contrainte de domaine que chk_session_eval_statut (Bar).
        with engine.connect() as conn:
            try:
                conn.execute(sql_text(
                    "ALTER TABLE patisserie_sessions_caisse ADD CONSTRAINT chk_patisserie_session_eval_statut "
                    "CHECK (evaluation_statut IN ('NON_EVALUE','TERMINEE'))"
                ))
                conn.commit()
            except Exception:
                conn.rollback()  # déjà présente

        # v23 — module Pâtisserie ajouté à la liste des départements pouvant
        # recevoir un renflouement manuel de caisse ; v27 — Piscine aussi.
        with engine.connect() as conn:
            try:
                conn.execute(sql_text(
                    "ALTER TABLE renflouements_departement DROP CONSTRAINT IF EXISTS chk_renfl_dept_departement"
                ))
                conn.execute(sql_text(
                    "ALTER TABLE renflouements_departement ADD CONSTRAINT chk_renfl_dept_departement "
                    "CHECK (departement IN ('HOTEL','CUISINE','BAR','PISCINE','PATISSERIE'))"
                ))
                conn.commit()
            except Exception:
                conn.rollback()  # contrainte déjà à jour

        # v28 — prix d'achat dépôt à 4 décimales (prix caisse ÷ unités, sans
        # perte d'arrondi) — élargissement sans perte.
        with engine.connect() as conn:
            try:
                conn.execute(sql_text("ALTER TABLE depot_achat_lignes ALTER COLUMN prix_achat TYPE NUMERIC(12,4)"))
                conn.commit()
            except Exception:
                conn.rollback()

        # v28 — Dépôt central : lieu DEPOT et mouvement TRANSFERT sur le
        # stock bar.
        for _nom, _check in (
            ("chk_bar_mouv_lieu", "lieu IS NULL OR lieu IN ('DEVANT','PISCINE','DEPOT')"),
            ("chk_bar_mouv_type", "type_mouvement IN ('ENTREE','SORTIE_VENTE','AJUSTEMENT','PERTE','CASSE','VOLONTAIRE','TRANSFERT')"),
        ):
            with engine.connect() as conn:
                try:
                    conn.execute(sql_text(f"ALTER TABLE bar_mouvements_stock DROP CONSTRAINT IF EXISTS {_nom}"))
                    conn.execute(sql_text(f"ALTER TABLE bar_mouvements_stock ADD CONSTRAINT {_nom} CHECK ({_check})"))
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    print(f"[migration v28] {_nom} : {e}")

        # v27 — domaine du département (DEVANT / PISCINE) sur ventes,
        # achats et paie bar : même contrainte que chk_session_lieu.
        for _table, _nom in (("bar_ventes", "chk_bar_vente_lieu"),
                             ("bar_achats", "chk_bar_achat_lieu"),
                             ("bar_paiements_employes", "chk_bar_paie_lieu")):
            with engine.connect() as conn:
                try:
                    conn.execute(sql_text(
                        f"ALTER TABLE {_table} ADD CONSTRAINT {_nom} "
                        "CHECK (lieu IS NULL OR lieu IN ('DEVANT','PISCINE'))"
                    ))
                    conn.commit()
                except Exception:
                    conn.rollback()  # déjà présente


def _depot_remise_a_zero_initiale():
    """Une seule fois (demande de la direction, 17/11/2026) : le Dépôt
    central repart de zéro — achats, factures, paiements, stock du dépôt et
    entrées « Reçu du dépôt » des bars effacés, tous les articles « à
    démarrer ». Marqueur : l'événement d'audit depot_demarrage_v1."""
    from models import AuditLog
    db = SessionLocal()
    try:
        if db.query(AuditLog).filter(AuditLog.action == "depot_demarrage_v1").first():
            return
        import json
        from depot_routes import remise_a_zero
        nb = remise_a_zero(db)
        # Marqueur écrit dans la MÊME transaction que l'effacement : jamais
        # rejoué au démarrage suivant.
        db.add(AuditLog(action="depot_demarrage_v1", details=json.dumps({"auto": True, **nb})))
        db.commit()
        print(f"[depot] remise à zéro initiale : {nb}")
    except Exception as e:
        db.rollback()
        print(f"[depot] remise à zéro initiale non faite : {e}")
    finally:
        db.close()


def _reprise_piscine_et_paiements_hotel():
    """Reprises de données idempotentes, relancées à chaque démarrage (ne
    touchent que ce qui reste à faire) :

    1. bar_ventes.lieu : département d'une vente historique, déduit de sa
       session de caisse, sinon du lieu de son mouvement de stock. Une vente
       sans aucun indice reste NULL = comptée avec le Bar (Devant).
    2. hotel_paiements : tout séjour payé qui n'a encore aucune ligne de
       paiement reçoit une ligne HISTORIQUE en gourdes (son montant payé
       d'avant le registre), datée de son arrivée — les rapports par devise
       retombent ainsi exactement sur les montants déjà affichés."""
    from sqlalchemy import text as sql_text
    etapes = [
        "UPDATE bar_ventes SET lieu = (SELECT s.lieu FROM bar_sessions_caisse s "
        "WHERE s.id = bar_ventes.session_id) "
        "WHERE lieu IS NULL AND session_id IS NOT NULL",
        "UPDATE bar_ventes SET lieu = (SELECT m.lieu FROM bar_mouvements_stock m "
        "WHERE m.reference_vente_id = bar_ventes.id AND m.type_mouvement = 'SORTIE_VENTE' "
        "AND m.lieu IS NOT NULL ORDER BY m.id LIMIT 1) "
        "WHERE lieu IS NULL AND EXISTS (SELECT 1 FROM bar_mouvements_stock m2 "
        "WHERE m2.reference_vente_id = bar_ventes.id AND m2.type_mouvement = 'SORTIE_VENTE' "
        "AND m2.lieu IS NOT NULL)",
        "INSERT INTO hotel_paiements (reservation_id, date_paiement, devise, montant, "
        "taux_change, montant_htg, mode_paiement, origine) "
        "SELECT r.id, r.date_arrivee, 'HTG', r.montant_paye, NULL, r.montant_paye, "
        "r.mode_paiement, 'HISTORIQUE' FROM hotel_reservations r "
        "WHERE r.montant_paye > 0 AND NOT EXISTS "
        "(SELECT 1 FROM hotel_paiements p WHERE p.reservation_id = r.id)",
    ]
    for sql in etapes:
        with engine.connect() as conn:
            try:
                conn.execute(sql_text(sql))
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"[reprise v27] étape ignorée : {e}")
    _reprise_lieux_bars()


def _reprise_lieux_bars():
    """Bar Devant / Bar Piscine : rattache à son bar tout l'historique dont
    le bar est CERTAIN (idempotent, ne touche que les lignes sans lieu) :

    1. vente sans lieu dont tous les articles sont réservés au même bar ;
    2. achat sans lieu d'un article réservé à un bar → ce bar.

    Les MOUVEMENTS DE STOCK ne sont jamais réattribués : le stock de chaque
    bar est celui que les caisses utilisent aujourd'hui (inventaires et
    ajustements déjà faits) — le modifier après coup le fausserait.
    Ce qui reste sans lieu (bar impossible à déterminer) est compté avec le
    Bar Devant. Les comptes restants sont écrits dans le journal du serveur."""
    from sqlalchemy import text as sql_text
    etapes = [
        # 1. articles tous réservés au même bar
        "UPDATE bar_ventes SET lieu = (SELECT MIN(p.lieu) FROM bar_lignes_vente l "
        "JOIN bar_produits p ON p.id = l.produit_id WHERE l.vente_id = bar_ventes.id) "
        "WHERE lieu IS NULL "
        "AND EXISTS (SELECT 1 FROM bar_lignes_vente l JOIN bar_produits p ON p.id = l.produit_id "
        "            WHERE l.vente_id = bar_ventes.id AND p.lieu IS NOT NULL) "
        "AND NOT EXISTS (SELECT 1 FROM bar_lignes_vente l LEFT JOIN bar_produits p ON p.id = l.produit_id "
        "                WHERE l.vente_id = bar_ventes.id AND (p.lieu IS NULL OR l.produit_id IS NULL)) "
        "AND (SELECT COUNT(DISTINCT p.lieu) FROM bar_lignes_vente l JOIN bar_produits p ON p.id = l.produit_id "
        "     WHERE l.vente_id = bar_ventes.id) = 1",
        # 2. achats d'un article réservé à un bar
        "UPDATE bar_achats SET lieu = (SELECT p.lieu FROM bar_produits p WHERE p.id = bar_achats.produit_id) "
        "WHERE lieu IS NULL AND produit_id IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM bar_produits p2 WHERE p2.id = bar_achats.produit_id AND p2.lieu IS NOT NULL)",
    ]
    for sql in etapes:
        with engine.connect() as conn:
            try:
                r = conn.execute(sql_text(sql))
                conn.commit()
                if r.rowcount:
                    print(f"[lieux bars] {r.rowcount} ligne(s) rattachée(s) : {sql[7:40]}…")
            except Exception as e:
                conn.rollback()
                print(f"[lieux bars] étape ignorée : {e}")
    # Diagnostic : ce qui reste sans bar (lu dans les journaux du serveur).
    diag = {}
    with engine.connect() as conn:
        for nom, sql in [
            ("ventes", "SELECT lieu, COUNT(*) FROM bar_ventes GROUP BY lieu"),
            ("mouvements", "SELECT lieu, COUNT(*) FROM bar_mouvements_stock GROUP BY lieu"),
            ("achats", "SELECT lieu, COUNT(*) FROM bar_achats GROUP BY lieu"),
            ("paie", "SELECT lieu, COUNT(*) FROM bar_paiements_employes GROUP BY lieu"),
            ("depenses", "SELECT lieu, COUNT(*) FROM bar_depenses GROUP BY lieu"),
            ("sessions", "SELECT lieu, COUNT(*) FROM bar_sessions_caisse GROUP BY lieu"),
        ]:
            try:
                diag[nom] = {str(l or "SANS_LIEU"): int(n) for l, n in conn.execute(sql_text(sql)).all()}
            except Exception as e:
                diag[nom] = f"erreur : {e}"
    print(f"[lieux bars] répartition : {diag}")


# ── Initialisation du schéma + données de démarrage ──────────────
def init_db():
    """
    Crée les tables si elles n'existent pas et insère les données initiales.
    Idempotent : peut être appelé plusieurs fois sans effet de bord.
    """
    Base.metadata.create_all(bind=engine)
    _migrate_columns()          # ajoute les colonnes manquantes sur SQLite
    _reprise_piscine_et_paiements_hotel()
    _depot_remise_a_zero_initiale()

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
