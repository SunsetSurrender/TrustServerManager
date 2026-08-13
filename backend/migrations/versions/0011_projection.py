"""colonne data derivate e stato della proiezione (fase 2B: popolamento del solo head)

Tre cose, e nessuna cambia il comportamento di `GET` o `PUT`:

  1. `inventory_state` → `inventory_projection_state`. Il nome vecchio diceva
     «stato dell'inventario», che è falso: lo stato dell'inventario è la testa in
     `inventory_head`. Questa riga dice quale versione la PROIEZIONE rispecchia, ed
     è una cosa diversa. Si rinomina adesso perché la tabella è vuota e nessuno la
     legge: rinominarla dopo costerebbe una migrazione di dati.
  2. `head_sha256` accanto a `head_version`. La versione dice *quale* istantanea la
     proiezione dichiara di rispecchiare; il digest dice *che cosa* si è
     effettivamente verificato in quel momento. Sono due domande diverse e la
     seconda è quella che scopre una proiezione modificata a mano o un ripristino
     parziale.
  3. `garanzia_date` e `supporto_date`: colonne DERIVATE, che è il punto di tutto
     il commit.

Perché due colonne data invece di cambiare il tipo di `garanzia`
---------------------------------------------------------------
`garanzia` e `supporto` restano TESTO (§8.42): l'inventario reale contiene «in
attesa», date malformate e caselle vuote, e sono valori dell'utente. Una colonna
`date` costringerebbe a scartarli o a reinterpretarli, cioè a perdere il dato per
farlo entrare in un tipo.

Ma finché la data esiste solo come testo, «quali dispositivi scadono entro trenta
giorni» non è una query: è una scansione di tutto il documento in Python, che è
esattamente ciò che lo scanner delle scadenze fa oggi (§8.41). Le due colonne
aggiungono la forma interrogabile SENZA togliere quella autorevole:

    garanzia       testo dell'utente, autorevole, torna nel documento
    garanzia_date  data interpretata, derivata, NON torna nel documento

⚠ L'interpretazione usa il parser dello scanner delle scadenze, non un secondo
parser scritto qui. Un `CHECK` con un'espressione SQL o una colonna
`GENERATED ALWAYS AS` sarebbero stati la scelta ovvia — e sbagliata: sarebbero una
seconda idea di «data valida», e due idee di data valida divergono, proprio sui
casi limite. Il prezzo è che la derivazione va tenuta allineata da chi scrive, e il
prezzo si paga con un controllo: `validate_model` confronta la colonna con il
parser e chiama `derived_mismatch` la differenza.

Il `CHECK` che si può fare senza reimplementare il parser
---------------------------------------------------------
`garanzia_date IS NULL OR garanzia IS NOT NULL`: una data interpretata non può
esistere se non c'è il testo da cui è stata interpretata. Non verifica *quale*
data sia — quello lo fa il parser — ma esclude la deriva più grossa, cioè la
colonna derivata che sopravvive alla cancellazione dell'originale.

Nessun privilegio di scrittura, ancora
--------------------------------------
Il popolamento è un comando ESPLICITO che gira come proprietario dello schema
(`scripts/project.py --rebuild`), non una migrazione di dati e non un servizio.
Entrambi i ruoli di runtime restano in sola lettura: la sincronizzazione a ogni
salvataggio è la fase 2C, ed è quella migrazione a dover concedere i privilegi che
le servono.

Perché il popolamento NON è qui dentro
--------------------------------------
Una migrazione di dati si esegue una volta sola, all'avvio, senza che nessuno la
guardi, e se aborta ferma il deployment. Il popolamento della proiezione deve
poter essere rieseguito, deve confrontare un digest e deve poter dire di no: è un
comando, si lancia quando si vuole, e il suo esito si legge. Vedi §8.42.

Revision ID: 0011_projection
Revises: 0010_normalised
Create Date: 2026-08-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_projection"
down_revision: Union[str, None] = "0010_normalised"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

API_ROLE = "tsm_api"
WORKER_ROLE = "tsm_worker"

STATE_TABLE = "inventory_projection_state"

#: (colonna derivata, colonna di testo da cui deriva).
DERIVED_DATES = (("garanzia_date", "garanzia"), ("supporto_date", "supporto"))


def upgrade() -> None:
    # ============================================= 1. il nome giusto
    op.rename_table("inventory_state", STATE_TABLE)
    # I vincoli conservano il nome vecchio, e un `inventory_state_pkey` su una
    # tabella che non si chiama più così è il genere di dettaglio che fa perdere
    # dieci minuti a chi legge un messaggio di errore fra sei mesi.
    op.execute(f"ALTER TABLE {STATE_TABLE} RENAME CONSTRAINT "
               f"inventory_state_pkey TO {STATE_TABLE}_pkey")
    op.execute(f"ALTER TABLE {STATE_TABLE} RENAME CONSTRAINT "
               f"ck_state_singleton TO ck_projection_state_singleton")

    # ======================================= 2. che cosa si è verificato
    op.add_column(STATE_TABLE, sa.Column("head_sha256", sa.Text))

    # Una riga di stato senza versione o senza digest non significa niente: «la
    # proiezione rispecchia... boh». L'assenza della RIGA è il modo di dire «non
    # rispecchia nulla», e questi due `NOT NULL` rendono impossibile la terza via,
    # cioè una riga scritta a metà.
    #
    # Si può fare adesso senza migrazione di dati perché la tabella è vuota
    # ovunque: la 0010 non semina niente e niente la scrive.
    op.alter_column(STATE_TABLE, "head_version", nullable=False)
    op.alter_column(STATE_TABLE, "head_sha256", nullable=False)
    op.alter_column(STATE_TABLE, "synchronised_at", nullable=False,
                    server_default=sa.text("now()"))

    # ======================================= 3. le date interpretate
    for column, source in DERIVED_DATES:
        op.add_column("inventory_devices", sa.Column(column, sa.Date))
        op.create_check_constraint(
            f"ck_device_{column}_needs_text", "inventory_devices",
            f"{column} IS NULL OR {source} IS NOT NULL")
        # Indice PARZIALE: la domanda è sempre «quali scadenze cadono fra due
        # date», che implica `IS NOT NULL`. Nel seed reale la maggior parte dei
        # dispositivi non ha date, e indicizzare 86 NULL per trovarne 12 sarebbe
        # indice sprecato.
        op.create_index(f"ix_device_{column}", "inventory_devices", [column],
                        postgresql_where=sa.text(f"{column} IS NOT NULL"))

    # ============================================================ privilegi
    #
    # La rinomina porta con sé i privilegi (seguono la tabella, non il nome), ma
    # l'intenzione va scritta nella migrazione che introduce il nome nuovo, non
    # dedotta da quella precedente. Le colonne aggiunte a `inventory_devices`
    # ereditano i privilegi di tabella: niente da concedere là.
    for role in (API_ROLE, WORKER_ROLE):
        op.execute(f"GRANT SELECT ON {STATE_TABLE} TO {role}")
        op.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {STATE_TABLE} "
                   f"FROM {role}")


def downgrade() -> None:
    for column, _source in DERIVED_DATES:
        op.drop_index(f"ix_device_{column}", table_name="inventory_devices")
        op.drop_constraint(f"ck_device_{column}_needs_text", "inventory_devices",
                           type_="check")
        op.drop_column("inventory_devices", column)

    op.alter_column(STATE_TABLE, "synchronised_at", nullable=True,
                    server_default=None)
    op.alter_column(STATE_TABLE, "head_version", nullable=True)
    op.drop_column(STATE_TABLE, "head_sha256")
    op.execute(f"ALTER TABLE {STATE_TABLE} RENAME CONSTRAINT "
               f"ck_projection_state_singleton TO ck_state_singleton")
    op.execute(f"ALTER TABLE {STATE_TABLE} RENAME CONSTRAINT "
               f"{STATE_TABLE}_pkey TO inventory_state_pkey")
    op.rename_table(STATE_TABLE, "inventory_state")
