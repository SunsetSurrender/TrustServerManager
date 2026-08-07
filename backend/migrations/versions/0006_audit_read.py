"""lettura dell'audit: esito, indice per (ts, id) e privilegi di sola lettura

Tre cose, tutte al servizio di `GET /api/audit` (§8.36):

1. Colonna `result`. La risposta deve dire se l'evento è andato a buon fine, e
   dedurlo dal nome dell'azione lato lettura sarebbe una regola sparsa in due
   punti che prima o poi divergono. Meglio scriverlo quando l'evento accade.

2. Indice su `(ts DESC, id DESC)`. È esattamente l'ordinamento e il predicato
   della paginazione a cursore: senza, ogni pagina è una scansione della tabella
   che cresce per sempre.

3. Verifica dei privilegi. Il ruolo di runtime deve poter SOLO leggere e
   inserire: la storia dell'audit non si corregge (§8.19). La GRANT di lettura
   c'è già dalla 0003; qui si REVOCA esplicitamente ciò che non deve avere, nel
   caso qualcuno l'abbia concesso altrove.

Revision ID: 0006_audit_read
Revises: 0005_auth_hardening
Create Date: 2026-08-06
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_audit_read"
down_revision: Union[str, None] = "0005_auth_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNTIME_ROLE = "tsm_api"


def upgrade() -> None:
    # --- 1. esito dell'evento ---
    op.add_column("audit", sa.Column("result", sa.Text, nullable=False,
                                     server_default="success"))
    op.create_check_constraint(
        "ck_audit_result", "audit",
        "result IN ('success','failure','denied')")

    # Le righe già presenti: i fallimenti di accesso si riconoscono dall'azione.
    op.execute("UPDATE audit SET result = 'failure' "
               "WHERE action = 'auth.login.failure'")
    op.execute("UPDATE audit SET result = 'denied' "
               "WHERE action = 'auth.login.blocked'")

    # --- 2. indice dell'accesso reale ---
    # Un solo indice composito: la query ordina e pagina esattamente così.
    # Indici sui filtri si aggiungono quando i numeri lo giustificano, non prima.
    op.execute("DROP INDEX IF EXISTS ix_audit_ts")     # sostituito dal composito
    op.create_index("ix_audit_ts_id_desc", "audit",
                    [sa.text("ts DESC"), sa.text("id DESC")])

    # --- 3. privilegi: lettura e scrittura in coda, niente altro ---
    op.execute(f"GRANT SELECT, INSERT ON audit TO {RUNTIME_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON audit FROM {RUNTIME_ROLE}")


def downgrade() -> None:
    op.drop_index("ix_audit_ts_id_desc", table_name="audit")
    op.create_index("ix_audit_ts", "audit", [sa.text("ts DESC")])
    op.drop_constraint("ck_audit_result", "audit", type_="check")
    op.drop_column("audit", "result")
