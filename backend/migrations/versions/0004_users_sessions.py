"""utenze e sessioni

Vedi BACKEND-PLAN.md §8.6 (disattivazione, non cancellazione) e §8.1 (sequenza
di avvio autenticata).

Scelte materializzate qui:

- `users.disabled_at` invece di un `DELETE`: `audit.actor_user_id` punta agli
  utenti, e cancellarli romperebbe la tracciabilità, che è il motivo per cui
  l'audit è stato spostato sul server. Lo `username` resta unico anche fra i
  disabilitati: riusare il nome di un utente disattivato è una riattivazione
  esplicita, non un inserimento.

- `sessions.token_hash` e non il token: il valore nel cookie non viene mai
  scritto nel database. Se il database venisse letto da chi non deve, le
  sessioni attive non sarebbero dirottabili.

- La foreign key da `audit.actor_user_id` si aggiunge ORA, che è il commit in cui
  `users` esiste: era annotata come debito in 0002.

Revision ID: 0004_users_sessions
Revises: 0003_runtime_role
Create Date: 2026-08-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0004_users_sessions"
down_revision: Union[str, None] = "0003_runtime_role"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNTIME_ROLE = "tsm_api"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "users",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("username", pg.CITEXT, nullable=False, unique=True),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("must_change_pw", sa.Boolean, nullable=False,
                  server_default=sa.false()),
        sa.Column("nome", sa.Text), sa.Column("cognome", sa.Text),
        sa.Column("telefono", sa.Text), sa.Column("team", sa.Text),
        sa.Column("disabled_at", sa.DateTime(timezone=True)),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("role IN ('view','edit','admin')", name="ck_users_role"),
    )
    op.create_index("ix_users_active", "users", ["username"],
                    postgresql_where=sa.text("disabled_at IS NULL"))

    op.create_table(
        "sessions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        # Nel database c'è l'hash, non il token del cookie.
        sa.Column("token_hash", sa.Text, nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("ip", pg.INET),
        sa.Column("user_agent", sa.Text),
    )
    op.create_index("ix_sessions_live", "sessions", ["user_id"],
                    postgresql_where=sa.text("revoked_at IS NULL"))

    # Debito annotato in 0002, saldato qui.
    op.create_foreign_key("fk_audit_actor_user", "audit", "users",
                          ["actor_user_id"], ["id"], ondelete="RESTRICT")

    # --- privilegi del ruolo di runtime (§8.19) ---
    # Le utenze si leggono e si aggiornano (last_login_at, cambio password,
    # disattivazione), non si eliminano: §8.6.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON users TO {RUNTIME_ROLE}")
    # Le sessioni si creano e si revocano; la pulizia delle scadute è un job
    # amministrativo, quindi il DELETE non serve al runtime.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON sessions TO {RUNTIME_ROLE}")


def downgrade() -> None:
    op.drop_constraint("fk_audit_actor_user", "audit", type_="foreignkey")
    op.drop_table("sessions")
    op.drop_table("users")
