"""baseline: nessuna tabella applicativa

Esiste per dimostrare che la catena delle migrazioni è cablata e funziona
(`alembic upgrade head` crea `alembic_version` e ci scrive questa revisione),
senza creare nulla dello schema applicativo.

Le tabelle del piano (§2) — users, sessions, inventory, inventory_head, photos,
audit, settings, notifications_sent, job_runs — arrivano nei commit successivi,
insieme al codice che le usa. Crearle adesso vorrebbe dire avere per un po' uno
schema che nessun test esercita.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-04
"""
from typing import Sequence, Union

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
