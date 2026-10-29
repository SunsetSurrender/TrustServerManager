"""uscita per inattività e registro dei dump notturni

1. `security_policy.idle_timeout_minutes`: una sessione senza richieste per più
   di N minuti si chiude. 0 = disattivato; altrimenti 15–720. Predefinito 60: il
   web gateway è condiviso, e una sessione lasciata aperta lì resta usabile da
   chiunque si sieda dopo.

2. `db_dumps`: una riga per ogni dump notturno del database (servizio `backup`
   di compose.yaml), con esito, dimensione e spazio libero del disco dati. La
   scrive il servizio di backup come proprietario dello schema; l'API la legge
   per la pagina «Stato del sistema». Il lavoro ha anche la sua riga in
   `maintenance_runs` (job `db_dump`), così il controllo Nagios della
   manutenzione lo copre come gli altri due.

Revision ID: 0017_idle_and_dumps
Revises: 0016_auth_purge
Create Date: 2026-10-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_idle_and_dumps"
down_revision: Union[str, None] = "0016_auth_purge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNTIME_ROLE = "tsm_api"


def upgrade() -> None:
    op.add_column("security_policy", sa.Column(
        "idle_timeout_minutes", sa.Integer, nullable=False, server_default=sa.text("60")))
    op.create_check_constraint(
        "ck_security_policy_idle", "security_policy",
        "idle_timeout_minutes = 0 OR idle_timeout_minutes BETWEEN 15 AND 720")

    op.create_table(
        "db_dumps",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("ok", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("file", sa.Text),
        sa.Column("bytes", sa.BigInteger),
        sa.Column("disk_total_bytes", sa.BigInteger),
        sa.Column("disk_free_bytes", sa.BigInteger),
        sa.Column("kept", sa.Integer),
        sa.Column("message", sa.Text),
    )
    op.create_index("ix_db_dumps_started", "db_dumps", ["started_at"])
    op.execute(f"GRANT SELECT ON db_dumps TO {RUNTIME_ROLE}")
    # La pagina «Stato del sistema» legge anche gli ultimi giri di manutenzione.
    op.execute(f"GRANT SELECT ON maintenance_runs TO {RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT ON worker_heartbeat TO {RUNTIME_ROLE}")

    op.drop_constraint("ck_maintenance_job", "maintenance_runs", type_="check")
    op.create_check_constraint("ck_maintenance_job", "maintenance_runs",
                               "job IN ('photo_gc', 'auth_purge', 'db_dump')")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT ON maintenance_runs FROM {RUNTIME_ROLE}")
    op.execute(f"REVOKE SELECT ON worker_heartbeat FROM {RUNTIME_ROLE}")
    op.execute("DELETE FROM maintenance_runs WHERE job = 'db_dump'")
    op.drop_constraint("ck_maintenance_job", "maintenance_runs", type_="check")
    op.create_check_constraint("ck_maintenance_job", "maintenance_runs",
                               "job IN ('photo_gc', 'auth_purge')")
    op.drop_index("ix_db_dumps_started", table_name="db_dumps")
    op.drop_table("db_dumps")
    op.drop_constraint("ck_security_policy_idle", "security_policy", type_="check")
    op.drop_column("security_policy", "idle_timeout_minutes")
