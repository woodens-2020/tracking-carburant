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
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=True)
except ImportError:
    pass

from models import (
    Base, Produit, Pompe, Utilisateur, Employe, FichePaie, Depense, Achat, ParametreDepense,
    BarProduit, BarPrixHistorique, BarAchat, BarMouvementStock,
    BarVente, BarLigneVente, BarCredit, BarRemboursement,
    BarCommande, BarLigneCommande, BarPaiementEmploye,
    HotelChambre, HotelEmploye, HotelReservation,
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
