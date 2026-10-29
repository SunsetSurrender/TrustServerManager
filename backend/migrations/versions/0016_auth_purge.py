"""pulizia periodica dei dati di accesso

Tre tabelle crescono a ogni accesso e non si svuotavano mai:

  login_attempts   una riga per ogni tentativo, riuscito o no
  sessions         una riga per ogni accesso, anche scaduta o revocata
  recovery_codes   una riga per ogni richiesta di recupero password

Dalla 0016 il worker le pota una volta al giorno, oltre la soglia di
conservazione (`TSM_AUTH_RETENTION_DAYS`, 90 giorni). Il registro di audit NON si
tocca: è la storia, e ha le sue regole (§8.19).

Il privilegio di cancellare lo riceve SOLO il worker (`tsm_worker`), come per la
GC delle foto: l'API resta senza DELETE su queste tabelle. Il lavoro ha la sua
riga in `maintenance_runs` (job `auth_purge`), quindi il vincolo sui nomi dei
lavori si allarga.

Revision ID: 0016_auth_purge
Revises: 0015_account_recovery
Create Date: 2026-10-28
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0016_auth_purge"
down_revision: Union[str, None] = "0015_account_recovery"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

WORKER_ROLE = "tsm_worker"
TABLES = ("login_attempts", "sessions", "recovery_codes")


def upgrade() -> None:
    op.drop_constraint("ck_maintenance_job", "maintenance_runs", type_="check")
    op.create_check_constraint("ck_maintenance_job", "maintenance_runs",
                               "job IN ('photo_gc', 'auth_purge')")
    for t in TABLES:
        op.execute(f"GRANT SELECT, DELETE ON {t} TO {WORKER_ROLE}")


def downgrade() -> None:
    for t in TABLES:
        op.execute(f"REVOKE SELECT, DELETE ON {t} FROM {WORKER_ROLE}")
    op.execute("DELETE FROM maintenance_runs WHERE job = 'auth_purge'")
    op.drop_constraint("ck_maintenance_job", "maintenance_runs", type_="check")
    op.create_check_constraint("ck_maintenance_job", "maintenance_runs",
                               "job IN ('photo_gc')")
