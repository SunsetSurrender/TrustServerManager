"""0.5.0: avvisi di sistema, storico dello stato, verifica dei dump, presenza e blocchi

1. `system_alert_config` (riga unica): avvisi di sistema via email. Destinatari:
   gli amministratori attivi con un'email (se `include_admins`) più gli
   indirizzi in `extra_recipients`. `min_level` decide se scrivere solo per i
   problemi critici o anche per quelli da controllare. `repeat_hours` è ogni
   quanto si ricorda un problema ancora aperto.

2. `system_alerts`: un problema (o un promemoria) per riga. Un problema è
   aperto finché `resolved_at` è vuoto; la chiave logica è `key` («Backup/Ultimo
   dump», «cert:30:2029-01-31»…). Le righe non si cancellano: sono lo storico
   mostrato nella scheda Diagnostica.

3. `status_snapshots`: una fotografia al giorno delle grandezze che crescono
   (database, disco, dump, dispositivi), per l'andamento a 90 giorni.

4. `db_dumps`: voci del dump (`toc_entries`) e prova di ripristino settimanale
   in un database temporaneo (`restore_*`).

5. `sessions.presence_at / presence_rack`: chi ha l'applicazione aperta adesso, e
   su quale rack. Colonne SEPARATE da `last_seen_at`: il segnale di presenza non
   deve tenere viva una sessione inattiva.

6. `edit_locks`: chi sta modificando quale rack. Un blocco SCADE da solo
   (`expires_at`) se il browser smette di rinnovarlo: niente rack bloccati per
   sempre da una scheda chiusa. Il runtime non ha DELETE (§8.19): rilasciare un
   blocco vuol dire farlo scadere adesso.

Tutto è letto e scritto dall'API (ruolo `tsm_api`), che ospita anche la
sentinella degli avvisi.

Revision ID: 0018_ops_presence
Revises: 0017_idle_and_dumps
Create Date: 2026-10-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0018_ops_presence"
down_revision: Union[str, None] = "0017_idle_and_dumps"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNTIME_ROLE = "tsm_api"
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    # ---- 1. configurazione degli avvisi ----
    op.create_table(
        "system_alert_config",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("include_admins", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("extra_recipients", pg.ARRAY(sa.Text), nullable=False,
                  server_default=sa.text("'{}'::text[]")),
        sa.Column("min_level", sa.Text, nullable=False, server_default="crit"),
        sa.Column("repeat_hours", sa.Integer, nullable=False, server_default=sa.text("24")),
        sa.Column("last_send_attempt_at", TS),
        sa.Column("last_send_ok_at", TS),
        sa.Column("last_send_error", sa.Text),
        sa.Column("last_test_at", TS),
        sa.Column("last_run_at", TS),
        sa.Column("updated_at", TS),
        sa.Column("updated_by", sa.Text),
        sa.CheckConstraint("id = 1", name="ck_alert_config_single"),
        sa.CheckConstraint("min_level IN ('crit', 'warn')", name="ck_alert_config_level"),
        sa.CheckConstraint("repeat_hours BETWEEN 1 AND 168", name="ck_alert_config_repeat"),
        sa.CheckConstraint("cardinality(extra_recipients) <= 20", name="ck_alert_config_extra"),
    )
    op.execute("INSERT INTO system_alert_config (id) VALUES (1)")
    op.execute(f"GRANT SELECT, UPDATE ON system_alert_config TO {RUNTIME_ROLE}")

    # ---- 2. problemi e promemoria ----
    op.create_table(
        "system_alerts",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False, server_default="problem"),
        sa.Column("level", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("detail", sa.Text),
        sa.Column("first_seen_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("last_seen_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("notified_at", TS),
        sa.Column("resolved_at", TS),
        sa.Column("resolved_notified_at", TS),
        sa.CheckConstraint("kind IN ('problem', 'reminder')", name="ck_alert_kind"),
        sa.CheckConstraint("level IN ('crit', 'warn', 'info')", name="ck_alert_level"),
    )
    # Un solo problema APERTO per chiave: il database lo garantisce.
    op.create_index("ux_system_alerts_open", "system_alerts", ["key"], unique=True,
                    postgresql_where=sa.text("resolved_at IS NULL"))
    op.create_index("ix_system_alerts_seen", "system_alerts", [sa.text("last_seen_at DESC")])
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON system_alerts TO {RUNTIME_ROLE}")

    # ---- 3. storico giornaliero ----
    op.create_table(
        "status_snapshots",
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("taken_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("db_bytes", sa.BigInteger),
        sa.Column("disk_used_pct", sa.Integer),
        sa.Column("disk_free_bytes", sa.BigInteger),
        sa.Column("dump_bytes", sa.BigInteger),
        sa.Column("devices", sa.Integer),
        sa.Column("racks", sa.Integer),
        sa.Column("active_users", sa.Integer),
        sa.Column("failed_logins", sa.Integer),
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON status_snapshots TO {RUNTIME_ROLE}")

    # ---- 4. dump: voci e prova di ripristino ----
    op.add_column("db_dumps", sa.Column("toc_entries", sa.Integer))
    op.add_column("db_dumps", sa.Column("restore_checked_at", TS))
    op.add_column("db_dumps", sa.Column("restore_ok", sa.Boolean))
    op.add_column("db_dumps", sa.Column("restore_rows", sa.BigInteger))
    op.add_column("db_dumps", sa.Column("restore_message", sa.Text))

    # ---- 5. presenza ----
    op.add_column("sessions", sa.Column("presence_at", TS))
    op.add_column("sessions", sa.Column("presence_rack", pg.UUID(as_uuid=False)))
    op.create_index("ix_sessions_presence", "sessions", [sa.text("presence_at DESC")],
                    postgresql_where=sa.text("revoked_at IS NULL"))

    # ---- 6. blocchi di modifica ----
    op.create_table(
        "edit_locks",
        sa.Column("rack_uid", pg.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", pg.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", pg.UUID(as_uuid=False), sa.ForeignKey("sessions.id", ondelete="CASCADE")),
        sa.Column("acquired_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", TS, nullable=False),
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON edit_locks TO {RUNTIME_ROLE}")
    op.execute(f"REVOKE DELETE, TRUNCATE ON edit_locks FROM {RUNTIME_ROLE}")

    # ---- 7. letture per lo Stato del sistema e la Diagnostica ----
    # L'esito dei giri degli avvisi di scadenza (lo scrive il worker).
    op.execute(f"GRANT SELECT ON scheduler_runs TO {RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT ON scheduler_runs FROM {RUNTIME_ROLE}")
    op.drop_table("edit_locks")
    op.drop_index("ix_sessions_presence", table_name="sessions")
    op.drop_column("sessions", "presence_rack")
    op.drop_column("sessions", "presence_at")
    for c in ("restore_message", "restore_rows", "restore_ok", "restore_checked_at",
              "toc_entries"):
        op.drop_column("db_dumps", c)
    op.drop_table("status_snapshots")
    op.drop_index("ix_system_alerts_seen", table_name="system_alerts")
    op.drop_index("ux_system_alerts_open", table_name="system_alerts")
    op.drop_table("system_alerts")
    op.drop_table("system_alert_config")
