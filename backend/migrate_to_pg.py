"""
Migration des données SQLite → PostgreSQL.

Prérequis :
  1. PostgreSQL installé et la base créée (voir schema.sql)
  2. Fichier .env configuré avec DATABASE_URL
  3. pip install psycopg2-binary python-dotenv

Usage :
  python migrate_to_pg.py

Le script est idempotent : il peut être relancé sans dupliquer les données
(grâce à INSERT ... ON CONFLICT DO NOTHING).
"""
import os, sys
from pathlib import Path
from datetime import date as date_type

# Charger .env
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    print("[WARN] python-dotenv non installé — variables d'env système utilisées")

PG_URL = os.environ.get("DATABASE_URL", "")
SQLITE_URL = "sqlite:///station.db"

if not PG_URL or not PG_URL.startswith("postgresql"):
    print("[ERREUR] DATABASE_URL doit pointer vers PostgreSQL.")
    print("         Exemple : postgresql+psycopg2://user:pass@localhost:5432/station_db")
    sys.exit(1)

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# ── Moteurs ────────────────────────────────────────────────────────
print("Connexion à SQLite…")
sqlite_eng = create_engine(SQLITE_URL, connect_args={"check_same_thread": False})
SQLiteDB   = sessionmaker(bind=sqlite_eng)

print("Connexion à PostgreSQL…")
pg_eng = create_engine(PG_URL, pool_pre_ping=True)
PGDB   = sessionmaker(bind=pg_eng)

from models import Base, Produit, Pompe, Releve

# ── Création du schéma PostgreSQL ─────────────────────────────────
print("Création des tables PostgreSQL…")
Base.metadata.create_all(pg_eng)

# ── Migration ─────────────────────────────────────────────────────
src = SQLiteDB()
dst = PGDB()

try:
    # ── Produits ──
    produits = src.query(Produit).all()
    print(f"  Migration de {len(produits)} produit(s)…")
    for p in produits:
        dst.merge(Produit(
            id=p.id, nom=p.nom,
            prix_gallon=float(p.prix_gallon),
            actif=True,
        ))
    dst.flush()

    # ── Pompes ──
    pompes = src.query(Pompe).all()
    print(f"  Migration de {len(pompes)} pompe(s)…")
    for po in pompes:
        dst.merge(Pompe(
            id=po.id, produit_id=po.produit_id,
            nom=po.nom, actif=True,
        ))
    dst.flush()

    # ── Relevés ──
    releves = src.query(Releve).all()
    print(f"  Migration de {len(releves)} relevé(s)…")
    for r in releves:
        dst.merge(Releve(
            id=r.id,
            date=r.date,
            periode=r.periode,
            pompe_id=r.pompe_id,
            prix_gallon=float(r.prix_gallon),
            metter_avant=float(r.metter_avant),
            metter_apres=float(r.metter_apres),
        ))
    dst.flush()

    dst.commit()
    print("  Commit OK.")

    # ── Réinitialiser les séquences PostgreSQL ──
    print("  Réinitialisation des séquences…")
    with pg_eng.connect() as conn:
        for table, col in [("produits", "id"), ("pompes", "id"), ("releves", "id")]:
            conn.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table}', '{col}'), "
                f"COALESCE((SELECT MAX({col}) FROM {table}), 0) + 1, false)"
            ))
        conn.commit()

    print()
    print("Migration terminée avec succès !")
    print(f"  {len(produits)} produit(s)   migré(s)")
    print(f"  {len(pompes)}   pompe(s)     migré(s)")
    print(f"  {len(releves)}  relevé(s)    migré(s)")

except Exception as e:
    dst.rollback()
    print(f"[ERREUR] Migration échouée : {e}")
    raise
finally:
    src.close()
    dst.close()
