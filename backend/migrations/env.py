"""Ambiente Alembic.

La URL arriva da app.config, che legge la password dal Docker secret: le migrazioni
girano con la stessa configurazione dell'API, senza duplicare la credenziale in
alembic.ini.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.config import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Nessun modello dichiarativo nello scheletro, quindi nessun autogenerate:
# le tabelle del piano (§2) arriveranno con le migrazioni dei commit successivi.
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().sqlalchemy_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_settings().sqlalchemy_url(), pool_pre_ping=True)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
