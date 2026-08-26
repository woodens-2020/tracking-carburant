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
    BarProduit, BarPrixHistorique, BarAchat, BarMouvementStock,
    BarVente, BarLigneVente, BarCredit, BarRemboursement,
    BarCommande, BarLigneCommande, BarPaiementEmploye,
    HotelChambre, HotelEmploye, HotelReservation,
    LoginSecurityEvent,  # noqa: F401 — nécessaire pour create_all
    BarSessionEvaluation,  # noqa: F401 — nécessaire pour create_all
    BarSessionComptage,  # noqa: F401 — nécessaire pour create_all
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
        ]
    else:
        return

    with engine.connect() as conn:
        for table, col, ddl_col, ddl_idx in new_cols:
            existing = [c["name"] for c in insp.get_columns(table)]
            if col not in existing:
                conn.execute(sql_text(ddl_col))
                if ddl_idx:
                    conn.execute(sql_text(ddl_idx))

        # v4 — rôle PDG : mise à jour de la contrainte CHECK sur utilisateurs
        if _is_postgres:
            try:
                conn.execute(sql_text(
                    "ALTER TABLE utilisateurs DROP CONSTRAINT IF EXISTS chk_utilisateur_role"
                ))
                conn.execute(sql_text(
                    "ALTER TABLE utilisateurs ADD CONSTRAINT chk_utilisateur_role "
                    "CHECK (role IN ('admin', 'operateur', 'pdg'))"
                ))
            except Exception:
                pass  # contrainte déjà à jour

            # v16 — précision des relevés de compteur élargie de 3 à 4 décimales
            # (élargissement sans perte : les valeurs existantes restent valides)
            try:
                conn.execute(sql_text(
                    "ALTER TABLE releves ALTER COLUMN metter_avant TYPE NUMERIC(14,4)"
                ))
                conn.execute(sql_text(
                    "ALTER TABLE releves ALTER COLUMN metter_apres TYPE NUMERIC(14,4)"
                ))
            except Exception:
                pass  # déjà à la bonne précision

            # v19 — jusqu'à 2 sessions de caisse par jour par caissier : la
            # contrainte d'unicité passe de (caissier_id, date_session) à
            # (caissier_id, date_session, numero_session).
            try:
                conn.execute(sql_text(
                    "ALTER TABLE bar_sessions_caisse DROP CONSTRAINT IF EXISTS uq_session_caissier_date"
                ))
                conn.execute(sql_text(
                    "ALTER TABLE bar_sessions_caisse ADD CONSTRAINT uq_session_caissier_date_num "
                    "UNIQUE (caissier_id, date_session, numero_session)"
                ))
            except Exception:
                pass  # contrainte déjà à jour

            # v20 — lieu (Bar Devant / Bar Piscine) : contrainte de domaine
            try:
                conn.execute(sql_text(
                    "ALTER TABLE bar_sessions_caisse ADD CONSTRAINT chk_session_lieu "
                    "CHECK (lieu IS NULL OR lieu IN ('DEVANT','PISCINE'))"
                ))
            except Exception:
                pass  # déjà présente

        conn.commit()


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
    finally:
        db.close()
