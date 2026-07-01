import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool
from alembic import context

# Ajouter le dossier backend au path pour importer models
sys.path.insert(0, str(Path(__file__).parent.parent))

# Charger les variables d'environnement
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

from models import Base           # importe Base avec tous les modèles
import models                     # force l'enregistrement de tous les modèles

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Métadonnées pour l'autogenerate
target_metadata = Base.metadata

# URL depuis l'environnement (priorité sur alembic.ini)
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///station.db")
# Échapper % → %% pour éviter l'erreur configparser d'interpolation
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
