"""fase 2C: la proiezione si mantiene a ogni salvataggio

Due cose, e sono le due che la fase 2C non può prendere in prestito dalla 2B.

**`mapper_version` nello stato della proiezione.** La proiezione è una
rappresentazione DERIVATA, e una derivata è valida solo rispetto al codice che l'ha
prodotta. Se domani un campo passasse da `extra` a una colonna tipizzata, le righe
già scritte riassemblerebbero ancora lo stesso documento — quindi lo stesso digest,
quindi nessun allarme — mentre le query per cui quella colonna esiste non
troverebbero niente. Il confronto dei digest è cieco esattamente lì, e questo numero
è ciò che lo vede.

La colonna nasce **NULL e senza default**, e non è una scorciatoia. Le righe di stato
scritte dalla fase 2B non dichiarano nessuna versione della mappa, e noi non
sappiamo quale mappa le ha scritte: lo sappiamo per deduzione — ce n'è stata una
sola — ma «per deduzione» non è un dato. Scrivere `1` al loro posto significherebbe
inventare un'informazione che nessuno ha registrato, per far tornare un controllo.
NULL è la verità, e NULL fa fallire chiuso il controllo di attualità: la proiezione
va ricostruita con `project.py --rebuild`, che è il passo 3 della sequenza di
aggiornamento documentata (§8.44). Nessuna migrazione di dati, come per la 2B.

**I privilegi di scrittura all'API.** La 0010 li aveva deliberatamente NEGATI e ha
scritto perché: «i privilegi di scrittura li concede la fase 2C, con il codice che
li usa». È questa migrazione, e questo è quel codice.

Si concede il MINIMO che serve a mantenere lo stato corrente:

  - `INSERT, UPDATE, DELETE` sulle cinque tabelle della proiezione e sullo stato;
  - **niente `TRUNCATE`**, che è un privilegio diverso e non serve: la
    sincronizzazione usa `DELETE`, per non prendere un lock ACCESS EXCLUSIVE che
    bloccherebbe anche i lettori della fase 2D.

E si NON tocca nient'altro. In particolare restano come sono:

  - `inventory_versions`: `SELECT, INSERT`, mai `UPDATE`, mai `DELETE`. Un'istantanea
    immutabile resta immutabile: è l'unica fonte di verità del documento, e in fase
    2C acquista un secondo mestiere — è il riferimento contro cui la proiezione si
    verifica. Poterla riscrivere renderebbe quella verifica una tautologia;
  - `audit`: append-only;
  - `photos`: l'API non cancella e non modifica i byte delle immagini. Le
    referenzia;
  - `inventory_photo_refs`: i riferimenti STORICI, che tengono in vita le foto delle
    versioni passate;
  - le tabelle del worker: l'API continua a non averne bisogno.

`UPDATE` sullo stato serve perché la riga è un singleton: la sincronizzazione la
riscrive, non ne accumula una per versione.

Revision ID: 0012_dual_write
Revises: 0011_projection
Create Date: 2026-08-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_dual_write"
down_revision: Union[str, None] = "0011_projection"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

API_ROLE = "tsm_api"
WORKER_ROLE = "tsm_worker"

STATE_TABLE = "inventory_projection_state"

#: Le tabelle della proiezione, nell'ordine gerarchico. Lo stato è a parte perché
#: non è una entità: è la ricevuta di che cosa la proiezione rispecchia.
PROJECTION_TABLES = (
    "inventory_locations",
    "inventory_rooms",
    "inventory_racks",
    "inventory_devices",
    "inventory_manual_entries",
)

#: Le tabelle su cui l'API NON deve poter scrivere, e che questa migrazione non
#: tocca. Elencate qui perché un elenco esplicito è ciò che un test di privilegi può
#: leggere: «non abbiamo concesso niente» è una frase che si verifica solo se si sa
#: su che cosa.
UNTOUCHED = ("inventory_versions", "inventory_head", "audit", "photos",
             "inventory_photo_refs")


def upgrade() -> None:
    # ---------------------------------------------------- versione della mappa
    op.add_column(STATE_TABLE, sa.Column("mapper_version", sa.Integer))
    op.create_check_constraint(
        "ck_state_mapper_version", STATE_TABLE,
        "mapper_version IS NULL OR mapper_version > 0")

    # ------------------------------------------------------------- privilegi
    #
    # Solo all'API. Il worker resta in sola lettura: non scrive l'inventario, e in
    # fase 2C nemmeno la sua proiezione — le colonne data derivate esistono per le
    # query, e il passaggio dello scanner è una decisione successiva (§8.44).
    for table in PROJECTION_TABLES + (STATE_TABLE,):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {API_ROLE}")
        # Esplicita, non implicita: `TRUNCATE` non è compreso in `ALL` per caso —
        # non lo si sta concedendo, e va scritto che non lo si sta concedendo.
        op.execute(f"REVOKE TRUNCATE ON {table} FROM {API_ROLE}")
        op.execute(f"GRANT SELECT ON {table} TO {WORKER_ROLE}")
        op.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {table} "
                   f"FROM {WORKER_ROLE}")

    # Ribadire ciò che non cambia costa tre righe e vale la pena: se un domani
    # qualcuno concedesse `UPDATE` su `inventory_versions` «per correggere un
    # digest», queste REVOKE sono la riga che il diff mostra accanto.
    for role in (API_ROLE, WORKER_ROLE):
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON inventory_versions "
                   f"FROM {role}")
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON audit FROM {role}")
        op.execute(f"REVOKE UPDATE, TRUNCATE ON photos FROM {role}")


def downgrade() -> None:
    for role in (API_ROLE, WORKER_ROLE):
        for table in PROJECTION_TABLES + (STATE_TABLE,):
            op.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {table} "
                       f"FROM {role}")
    op.drop_constraint("ck_state_mapper_version", STATE_TABLE, type_="check")
    op.drop_column(STATE_TABLE, "mapper_version")
