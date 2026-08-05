"""ruolo di runtime separato: append-only imposto dai privilegi

Finora l'API si collegava come proprietario dello schema, quindi «append-only»
era una promessa del codice applicativo. Un difetto, un `UPDATE` scritto per
sbaglio o un'iniezione lo avrebbero smentito senza che il database obiettasse.

Qui la garanzia si sposta di livello: due ruoli distinti.

  proprietario (POSTGRES_USER, es. `tsm`)  → migrazioni, DDL, bootstrap
  runtime      (`tsm_api`)                 → solo ciò che serve a servire richieste

Privilegi del ruolo di runtime:

  inventory_versions   SELECT, INSERT            (mai UPDATE, mai DELETE)
  audit                SELECT, INSERT            (mai UPDATE, mai DELETE)
  inventory_head       SELECT, UPDATE            (mai INSERT, mai DELETE)
  alembic_version      SELECT                    (serve alla readiness)

`INSERT` sulla testa è escluso di proposito: la riga di testa nasce una volta
sola, nel bootstrap, che gira come proprietario. Così «il bootstrap non passa da
HTTP» non è una convenzione ma un privilegio che l'API non ha.

La password del ruolo NON sta qui: una migrazione finisce nel repository e
nell'immagine. Il ruolo viene creato senza password e la imposta l'operations —
`backend/scripts/provision_runtime_role.py`, che il servizio `migrate` esegue
leggendola da un secret.

Revision ID: 0003_runtime_role
Revises: 0002_inventory
Create Date: 2026-08-04
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003_runtime_role"
down_revision: Union[str, None] = "0002_inventory"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNTIME_ROLE = "tsm_api"


def upgrade() -> None:
    # Ruolo idempotente: CREATE ROLE non ha IF NOT EXISTS.
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RUNTIME_ROLE}') THEN
                CREATE ROLE {RUNTIME_ROLE} LOGIN NOINHERIT;
            END IF;
        END
        $$;
    """)

    # Si parte da zero: nessun privilegio ereditato o residuo.
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {RUNTIME_ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {RUNTIME_ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}")
    # Nessun CREATE: il ruolo di runtime non fa DDL.
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {RUNTIME_ROLE}")

    # Storia: si legge e si accoda, non si riscrive.
    op.execute(f"GRANT SELECT, INSERT ON inventory_versions TO {RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON audit TO {RUNTIME_ROLE}")

    # Testa: si legge e si sposta. Non si crea (bootstrap) e non si elimina.
    op.execute(f"GRANT SELECT, UPDATE ON inventory_head TO {RUNTIME_ROLE}")

    # Readiness: deve poter verificare che le migrazioni siano al livello atteso.
    op.execute(f"GRANT SELECT ON alembic_version TO {RUNTIME_ROLE}")

    # Le colonne identity hanno sequenze proprie della colonna: l'INSERT sulla
    # tabella basta. Nessun GRANT su sequenze, che darebbe più del necessario.


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {RUNTIME_ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {RUNTIME_ROLE}")
    # Il ruolo non viene eliminato: potrebbe possedere sessioni attive o essere
    # condiviso. Rimuoverlo è una decisione operativa, non di schema.
