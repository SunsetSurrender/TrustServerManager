"""blocco delle utenze dopo troppi accessi falliti

Fino alla 0013 la protezione contro i tentativi ripetuti era una finestra
temporale: 5 errori sulla stessa utenza o 20 dallo stesso indirizzo nell'arco di
15 minuti, poi si riprovava. Su `tsm-prd` tutti gli operatori arrivano dal web
gateway, quindi con lo stesso indirizzo IP: la finestra per IP era condivisa da
tutto il gruppo, e un collega che sbagliava la password poteva chiudere fuori gli
altri.

Da qui il comportamento è un altro, ed è una richiesta esplicita dell'esercizio:

1. `users.failed_logins` conta gli accessi falliti CONSECUTIVI di un'utenza; un
   accesso riuscito lo azzera.
2. Raggiunta la soglia, `users.locked_at` si valorizza e l'utenza resta bloccata
   finché un AMMINISTRATORE non la sblocca. Non scade da sola.
3. La soglia sta in `security_policy`, una riga sola, e si cambia
   dall'interfaccia (Impostazioni). Vincolo 3–20: sotto il 3 un errore di
   battitura blocca l'utenza, sopra il 20 il blocco non protegge più.

Una tabella propria e non un campo del documento delle impostazioni: quel
documento è uno schema chiuso con la sua concorrenza ottimistica e i suoi
destinatari, e la politica di accesso è un'altra cosa — con un altro registro di
chi l'ha cambiata.

Revision ID: 0014_account_lockout
Revises: 0013_domain
Create Date: 2026-10-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_account_lockout"
down_revision: Union[str, None] = "0013_domain"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNTIME_ROLE = "tsm_api"

#: Soglia predefinita: cinque errori consecutivi.
DEFAULT_MAX_FAILED_LOGINS = 5


def upgrade() -> None:
    op.add_column("users", sa.Column(
        "failed_logins", sa.Integer, nullable=False, server_default=sa.text("0")))
    op.add_column("users", sa.Column(
        "locked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_users_failed_logins", "users", "failed_logins >= 0")

    op.create_table(
        "security_policy",
        # Una riga sola, come `settings`: la chiave è il valore 1 e un vincolo lo
        # impone, così un INSERT di troppo fallisce invece di creare due politiche.
        sa.Column("id", sa.SmallInteger, primary_key=True),
        sa.Column("max_failed_logins", sa.Integer, nullable=False,
                  server_default=sa.text(str(DEFAULT_MAX_FAILED_LOGINS))),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by", sa.Text),
        sa.CheckConstraint("id = 1", name="ck_security_policy_singleton"),
        sa.CheckConstraint("max_failed_logins BETWEEN 3 AND 20",
                           name="ck_security_policy_max_failed_logins"),
    )
    op.execute("INSERT INTO security_policy (id) VALUES (1)")

    # L'API legge e aggiorna la riga; non può crearne altre né cancellarla.
    op.execute(f"GRANT SELECT, UPDATE ON security_policy TO {RUNTIME_ROLE}")
    op.execute(f"REVOKE INSERT, DELETE, TRUNCATE ON security_policy FROM {RUNTIME_ROLE}")


def downgrade() -> None:
    op.drop_table("security_policy")
    op.drop_constraint("ck_users_failed_logins", "users", type_="check")
    op.drop_column("users", "locked_at")
    op.drop_column("users", "failed_logins")
