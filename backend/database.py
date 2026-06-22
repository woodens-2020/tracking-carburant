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
from sqlalchemy.pool import NullPool

# Chargement du fichier .env si python-dotenv est disponible
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from models import Base, Produit, Pompe, Utilisateur

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
    """Ajoute les nouvelles colonnes aux tables existantes (idempotent, SQLite + PostgreSQL)."""
    from sqlalchemy import inspect, text as sql_text

    insp = inspect(engine)

    if _is_sqlite:
        new_cols = [
            ("produits",      "actif",           "ALTER TABLE produits      ADD COLUMN actif INTEGER NOT NULL DEFAULT 1"),
            ("produits",      "created_at",      "ALTER TABLE produits      ADD COLUMN created_at DATETIME DEFAULT NULL"),
            ("pompes",        "actif",           "ALTER TABLE pompes        ADD COLUMN actif INTEGER NOT NULL DEFAULT 1"),
            ("pompes",        "created_at",      "ALTER TABLE pompes        ADD COLUMN created_at DATETIME DEFAULT NULL"),
            ("releves",       "created_at",      "ALTER TABLE releves       ADD COLUMN created_at DATETIME DEFAULT NULL"),
            ("releves",       "updated_at",      "ALTER TABLE releves       ADD COLUMN updated_at DATETIME DEFAULT NULL"),
            ("releves",       "nb_modifications","ALTER TABLE releves       ADD COLUMN nb_modifications INTEGER NOT NULL DEFAULT 0"),
            ("utilisateurs",  "api_key_hash",    "ALTER TABLE utilisateurs  ADD COLUMN api_key_hash VARCHAR(64) UNIQUE"),
        ]
    elif _is_postgres:
        new_cols = [
            ("releves",      "nb_modifications",
             "ALTER TABLE releves      ADD COLUMN nb_modifications INTEGER NOT NULL DEFAULT 0"),
            ("utilisateurs", "api_key_hash",
             "ALTER TABLE utilisateurs ADD COLUMN api_key_hash VARCHAR(64) UNIQUE"),
        ]
    else:
        return

    with engine.connect() as conn:
        for table, col, ddl in new_cols:
            existing = [c["name"] for c in insp.get_columns(table)]
            if col not in existing:
                conn.execute(sql_text(ddl))
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
            from auth import hash_password
            admin = Utilisateur(
                username="admin",
                password_hash=hash_password("admin123"),
                nom_complet="Administrateur",
                role="admin",
            )
            db.add(admin)
            db.commit()
    finally:
        db.close()
