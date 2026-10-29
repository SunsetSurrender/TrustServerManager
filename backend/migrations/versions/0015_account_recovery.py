"""recupero della password via email

Tre cose:

1. `users.email`: l'indirizzo a cui mandare il codice. Lo imposta un
   amministratore, come gli altri dati di profilo. Facoltativo: senza indirizzo
   il recupero per quell'utenza semplicemente non parte.

2. `recovery_codes`: un codice monouso per richiesta. Si conserva solo
   l'HASH del codice, mai il valore: chi legge il database non può usarlo. Il
   codice scade (30 minuti), si usa una volta, e dopo pochi tentativi sbagliati
   si invalida — è corto apposta, perché sulla VM si arriva dal web gateway e
   il link della mail spesso non si può aprire dove la mail si legge: il codice
   si ricopia a mano.

3. `security_policy.recovery_enabled`: l'amministratore può spegnere il
   recupero dall'interfaccia. Acceso per impostazione predefinita, ma senza SMTP
   configurato non fa comunque niente.

Revision ID: 0015_account_recovery
Revises: 0014_account_lockout
Create Date: 2026-10-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0015_account_recovery"
down_revision: Union[str, None] = "0014_account_lockout"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNTIME_ROLE = "tsm_api"


def upgrade() -> None:
    op.add_column("users", sa.Column("email", sa.Text, nullable=True))
    op.create_check_constraint(
        "ck_users_email", "users",
        "email IS NULL OR (char_length(email) BETWEEN 3 AND 254 AND email LIKE '%_@_%')")

    op.create_table(
        "recovery_codes",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code_hash", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("invalidated_at", sa.DateTime(timezone=True)),
        sa.Column("failed_attempts", sa.Integer, nullable=False,
                  server_default=sa.text("0")),
        sa.Column("requested_ip", sa.Text),
        sa.CheckConstraint("failed_attempts >= 0",
                           name="ck_recovery_codes_failed"),
    )
    op.create_index("ix_recovery_codes_user", "recovery_codes",
                    ["user_id", "created_at"])

    op.add_column("security_policy", sa.Column(
        "recovery_enabled", sa.Boolean, nullable=False,
        server_default=sa.text("true")))

    # Niente DELETE: i codici usati o scaduti restano come traccia, e il ruolo
    # di runtime non deve poter cancellare la prova di una richiesta.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON recovery_codes TO {RUNTIME_ROLE}")
    op.execute(f"REVOKE DELETE, TRUNCATE ON recovery_codes FROM {RUNTIME_ROLE}")


def downgrade() -> None:
    op.drop_column("security_policy", "recovery_enabled")
    op.drop_index("ix_recovery_codes_user", table_name="recovery_codes")
    op.drop_table("recovery_codes")
    op.drop_constraint("ck_users_email", "users", type_="check")
    op.drop_column("users", "email")
