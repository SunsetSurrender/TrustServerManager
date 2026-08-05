"""audit degli eventi di autenticazione e limitazione dei tentativi di accesso

Due modifiche, entrambe richieste dall'audit dell'autenticazione (§8.25):

1. `audit.actor_role` diventa nullable. Un tentativo di accesso FALLITO non ha un
   ruolo: l'utenza potrebbe non esistere. Con la colonna NOT NULL si sarebbe
   costretti a inventare un valore, e un ruolo inventato in un registro di audit
   è peggio di un campo vuoto.

2. `login_attempts`: contatore durevole dei tentativi, per la limitazione. In
   memoria non andrebbe bene — si azzera a ogni riavvio, che è esattamente il
   momento in cui un attaccante insiste — e non sopravviverebbe a più repliche.

Revision ID: 0005_auth_hardening
Revises: 0004_users_sessions
Create Date: 2026-08-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0005_auth_hardening"
down_revision: Union[str, None] = "0004_users_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNTIME_ROLE = "tsm_api"


def upgrade() -> None:
    # --- 1. ruolo nullable per gli eventi anonimi ---
    op.alter_column("audit", "actor_role", nullable=True)
    op.drop_constraint("ck_audit_role", "audit", type_="check")
    op.create_check_constraint(
        "ck_audit_role", "audit",
        "actor_role IS NULL OR actor_role IN ('view','edit','admin')")
    # Lo stesso vale per `actor_username`: resta NOT NULL perché per un accesso
    # fallito l'utenza TENTATA la conosciamo, ed è l'informazione che serve a chi
    # legge il registro. Non è una credenziale.

    # --- 2. tentativi di accesso ---
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("username", pg.CITEXT, nullable=False),
        sa.Column("ip", pg.INET),
        sa.Column("success", sa.Boolean, nullable=False),
    )
    # Indici sulle due finestre che interroga il limitatore: per utenza e per IP.
    op.create_index("ix_login_attempts_username_ts", "login_attempts",
                    ["username", sa.text("ts DESC")])
    op.create_index("ix_login_attempts_ip_ts", "login_attempts",
                    ["ip", sa.text("ts DESC")])

    # Il runtime accoda e conta. La potatura delle righe vecchie è manutenzione
    # (job amministrativo), quindi nessun DELETE: coerente con §8.19.
    op.execute(f"GRANT SELECT, INSERT ON login_attempts TO {RUNTIME_ROLE}")


def downgrade() -> None:
    op.drop_table("login_attempts")
    op.drop_constraint("ck_audit_role", "audit", type_="check")
    op.execute("UPDATE audit SET actor_role = 'admin' WHERE actor_role IS NULL")
    op.create_check_constraint("ck_audit_role", "audit",
                              "actor_role IN ('view','edit','admin')")
    op.alter_column("audit", "actor_role", nullable=False)
