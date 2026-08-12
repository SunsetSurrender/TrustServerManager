"""tabelle normalizzate dell'inventario (fase 2A: schema, nessuna scrittura)

Lo stato operativo corrente dell'inventario in forma relazionale. **Questa
migrazione crea le tabelle e nient'altro**: niente le popola, niente le legge,
`GET` e `PUT` non cambiano di una riga. Il popolamento è la fase 2B, la scrittura
la 2C, la lettura la 2D (§8.42).

L'architettura congelata, perché senza di essa lo schema non si capisce
------------------------------------------------------------------------
    tabelle normalizzate   → stato operativo CORRENTE, autorevole
    inventory_versions.doc → istantanee storiche CANONICHE, immutabili, per sempre

La fase 2 **non cancella la storia in JSON**. Un ripristino carica un'istantanea
storica, sincronizza le tabelle correnti su di essa e crea una versione NUOVA: la
storia non si modifica, ci si aggiunge. È la stessa regola dell'append-only
(§8.19) applicata un livello più su.

L'identità è l'`_uid`, e non è una preferenza
--------------------------------------------
La chiave primaria di ogni entità identificata è l'`_uid` immutabile (§8.4).
Codice del rack e identificativo del dispositivo restano ATTRIBUTI MUTABILI: una
rinomina è un `rename` che conserva l'identità, e una chiave primaria sul codice
trasformerebbe ogni rinomina in «entità diversa», spezzando la storia proprio nel
caso che §8.4 esiste per proteggere.

L'ordine è un dato
------------------
Ogni collezione ha una colonna `ordinal`. L'ordine delle righe che PostgreSQL
restituisce senza `ORDER BY` non è definito, e un riordino è un evento di dominio
(§8.10): affidarsi all'ordine fisico produrrebbe eventi `reorder` che nessuno ha
causato, al primo `VACUUM`.

NULL significa «non rappresentato qui»
--------------------------------------
Il documento è APERTO: lo schema congelato (§8.16) vincola le chiavi di radice, non
i campi delle entità. Un valore che una colonna tipizzata non può contenere senza
mentire — `u: "45"`, `seriali: ["ok", 12345]` — viaggia in `extra`, e la colonna
resta NULL. La regola vale in entrambi i versi:

    la colonna vale NULL  ⇔  la chiave è in `extra`

L'alternativa (colonna NOT NULL con un valore di comodo più la copia in `extra`)
darebbe una tabella interrogabile che risponde il falso, che è peggio di una che
dichiara di non sapere. Vedi `app/inventory/relational.py`.

Vincoli differibili
-------------------
I vincoli di unicità con ambito sono `DEFERRABLE INITIALLY IMMEDIATE`. Scambiare
il codice di due rack in una transazione è legittimo e a metà strada i due codici
collidono: la sincronizzazione (fase 2C) dichiarerà `SET CONSTRAINTS ALL DEFERRED`
dove serve. `INITIALLY IMMEDIATE` resta il default perché un errore che compare
sullo statement colpevole è molto più facile da diagnosticare di uno che compare
al commit.

Revision ID: 0010_normalised
Revises: 0009_photos
Create Date: 2026-08-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0010_normalised"
down_revision: Union[str, None] = "0009_photos"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

API_ROLE = "tsm_api"
WORKER_ROLE = "tsm_worker"

#: Tabelle create qui, in ordine di dipendenza.
TABLES = ("inventory_locations", "inventory_rooms", "inventory_racks",
          "inventory_devices", "inventory_manual_entries", "inventory_state")


def _uid_pk() -> sa.Column:
    """Chiave primaria: l'`_uid` del documento, immutabile (§8.4)."""
    return sa.Column("uid", pg.UUID(as_uuid=False), primary_key=True)


def upgrade() -> None:
    # ============================================================== siti
    op.create_table(
        "inventory_locations",
        _uid_pk(),
        # `code` è l'`id` del documento: MUTABILE. Nullable perché un valore non
        # rappresentabile (un id numerico, per esempio) viaggia in `extra`.
        sa.Column("code", sa.Text),
        sa.Column("nome", sa.Text),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("extra", pg.JSONB, nullable=False, server_default="{}"),
        sa.CheckConstraint("ordinal >= 0", name="ck_location_ordinal"),
        # Ambito: il documento. Differibile perché un riordino scambia gli
        # ordinali, e a metà transazione due siti condividono una posizione.
        sa.UniqueConstraint("ordinal", name="uq_location_ordinal",
                            deferrable=True, initially="IMMEDIATE"),
        sa.UniqueConstraint("code", name="uq_location_code",
                            deferrable=True, initially="IMMEDIATE"),
    )

    # ============================================================== sale
    op.create_table(
        "inventory_rooms",
        _uid_pk(),
        sa.Column("location_uid", pg.UUID(as_uuid=False),
                  sa.ForeignKey("inventory_locations.uid", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("code", sa.Text),
        sa.Column("nome", sa.Text),
        sa.Column("w", sa.Numeric),
        sa.Column("h", sa.Numeric),
        sa.Column("area", sa.Text),
        sa.Column("dim", sa.Text),
        sa.Column("segnaposto", sa.Boolean),
        # ⚠ I `vani` restano un VALUE OBJECT della sala, in JSONB (§8.12).
        #
        # Sono già stati classificati così: nessuna identità immutabile visibile
        # all'utente, nessun CRUD indipendente, nessuna semantica di spostamento,
        # nessuna interrogazione globale. La geometria della porta è annidata dentro
        # il vano e segue la stessa regola — e un vano può averne due (`porta`,
        # `porta2`): il seed di produzione ne contiene già un caso.
        #
        # Una tabella `vani` più una tabella `porte` costerebbero due join per
        # disegnare una pianta, un ordinale in più da mantenere e due tabelle da
        # cancellare a cascata, in cambio di nessuna garanzia: non c'è nessun
        # vincolo di integrità fra un vano e il resto del mondo. Normalizzare serve
        # all'integrità e all'interrogabilità, non a trasformare ogni oggetto
        # annidato in una tabella.
        sa.Column("vani", pg.JSONB),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("extra", pg.JSONB, nullable=False, server_default="{}"),
        sa.CheckConstraint("ordinal >= 0", name="ck_room_ordinal"),
        sa.UniqueConstraint("location_uid", "ordinal", name="uq_room_ordinal",
                            deferrable=True, initially="IMMEDIATE"),
        # Ambito: il sito. Due siti possono avere entrambi una «Sala 1».
        sa.UniqueConstraint("location_uid", "code", name="uq_room_code",
                            deferrable=True, initially="IMMEDIATE"),
    )
    op.create_index("ix_room_location", "inventory_rooms", ["location_uid"])

    # ============================================================== rack
    op.create_table(
        "inventory_racks",
        _uid_pk(),
        sa.Column("room_uid", pg.UUID(as_uuid=False),
                  sa.ForeignKey("inventory_rooms.uid", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("code", sa.Text),
        sa.Column("name", sa.Text),
        # `row` nel documento. Rinominata perché `row` è una parola chiave SQL, e
        # una colonna che va sempre citata è una colonna che prima o poi qualcuno
        # cita male.
        sa.Column("row_label", sa.Text),
        sa.Column("u", sa.Integer),
        sa.Column("x", sa.Numeric),
        sa.Column("y", sa.Numeric),
        sa.Column("w", sa.Numeric),
        sa.Column("h", sa.Numeric),
        # `text[]` e non JSONB: i seriali si cercano (matching con gli asset), e un
        # array di testo si interroga con `= ANY(...)` e si indicizza. Il prezzo è
        # che una lista con dentro un numero non ci sta: quella finisce in `extra`,
        # perché `{"ok","12345"}` restituirebbe il numero come stringa e il giro
        # completo non sarebbe più fedele.
        sa.Column("seriali", pg.ARRAY(sa.Text)),
        # Foto CORRENTE del rack.
        #
        # Nessun `ON DELETE`: come per `inventory_photo_refs` (§8.5), il database
        # rifiuta di cancellare una foto ancora riferita. Le foto che servono alle
        # versioni STORICHE le protegge quella tabella; questa colonna dice qual è
        # la foto adesso. Se le due cose divergessero, la GC risulterebbe bloccata
        # su una foto orfana — un guasto nella direzione sicura.
        sa.Column("photo_id", pg.UUID(as_uuid=False),
                  sa.ForeignKey("photos.id")),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("extra", pg.JSONB, nullable=False, server_default="{}"),
        sa.CheckConstraint("ordinal >= 0", name="ck_rack_ordinal"),
        sa.UniqueConstraint("room_uid", "ordinal", name="uq_rack_ordinal",
                            deferrable=True, initially="IMMEDIATE"),
        # Ambito: la sala. È il vincolo che l'interfaccia già applica («ID già
        # esistente in questa sala»), e il caso per cui serve `DEFERRABLE`: due rack
        # che si scambiano il codice collidono a metà transazione.
        sa.UniqueConstraint("room_uid", "code", name="uq_rack_code",
                            deferrable=True, initially="IMMEDIATE"),
    )
    op.create_index("ix_rack_room", "inventory_racks", ["room_uid"])
    op.create_index("ix_rack_photo", "inventory_racks", ["photo_id"])

    # ========================================================= dispositivi
    op.create_table(
        "inventory_devices",
        _uid_pk(),
        sa.Column("rack_uid", pg.UUID(as_uuid=False),
                  sa.ForeignKey("inventory_racks.uid", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("code", sa.Text),
        sa.Column("name", sa.Text),
        sa.Column("type", sa.Text),
        sa.Column("stato", sa.Text),
        sa.Column("model", sa.Text),
        sa.Column("ip", sa.Text),
        sa.Column("serial", sa.Text),
        sa.Column("owner", sa.Text),
        # ⚠ TESTO, non `date`.
        #
        # L'inventario reale contiene «in attesa», date malformate e caselle vuote:
        # sono valori dell'utente, e una colonna `date` costringerebbe a scartarli o
        # a reinterpretarli — cioè a perdere il dato per farlo entrare in un tipo.
        # Il posto dove si decide che una data non è leggibile è già lo scanner
        # delle scadenze (§8.41), che la ignora in silenzio e la mostra nella vista
        # Scadenze. La validazione del modello lo segnala come AVVISO, non errore.
        sa.Column("garanzia", sa.Text),
        sa.Column("supporto", sa.Text),
        sa.Column("note", sa.Text),
        sa.Column("u", sa.Integer),
        sa.Column("h", sa.Integer),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("extra", pg.JSONB, nullable=False, server_default="{}"),
        sa.CheckConstraint("ordinal >= 0", name="ck_device_ordinal"),
        sa.UniqueConstraint("rack_uid", "ordinal", name="uq_device_ordinal",
                            deferrable=True, initially="IMMEDIATE"),
        # ⚠ NESSUN vincolo di unicità su (rack_uid, code), e non è una
        # dimenticanza.
        #
        # L'identificativo di un dispositivo arriva dall'import tabellare, dove due
        # righe con lo stesso identificativo di asset nello stesso rack sono un caso
        # reale; il validatore di identità (§8.4) le tollera da sempre e
        # l'interfaccia non le impedisce. Un vincolo qui farebbe rifiutare alla fase
        # 2C documenti che la fase 1 accetta — un cambio di comportamento
        # introdotto di straforo. `validate_model` lo segnala come avviso, e
        # diventerà una decisione di prodotto quando qualcuno vorrà prenderla.
    )
    op.create_index("ix_device_rack", "inventory_devices", ["rack_uid"])
    # Gli indici che servono alle interrogazioni per cui la normalizzazione esiste:
    # cercare un dispositivo per identificativo, per IP o per seriale senza leggere
    # e deserializzare l'intero documento.
    op.create_index("ix_device_code", "inventory_devices", ["code"])
    op.create_index("ix_device_ip", "inventory_devices", ["ip"])
    op.create_index("ix_device_serial", "inventory_devices", ["serial"])

    # ======================================================= voci di manuale
    op.create_table(
        "inventory_manual_entries",
        _uid_pk(),
        sa.Column("code", sa.Text),
        sa.Column("titolo", sa.Text),
        # Value object della voce, come i vani per la sala: paragrafi senza identità
        # propria, che si modificano solo insieme alla voce che li contiene.
        sa.Column("blocchi", pg.JSONB),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("extra", pg.JSONB, nullable=False, server_default="{}"),
        sa.CheckConstraint("ordinal >= 0", name="ck_manual_ordinal"),
        sa.UniqueConstraint("ordinal", name="uq_manual_ordinal",
                            deferrable=True, initially="IMMEDIATE"),
        sa.UniqueConstraint("code", name="uq_manual_code",
                            deferrable=True, initially="IMMEDIATE"),
    )

    # ==================================== stato di radice della proiezione
    #
    # Tabella a ZERO O UNA riga, e l'assenza è il dato: nessuna riga significa «la
    # proiezione non rispecchia nessuna versione».
    #
    # ⚠ Non si semina una riga qui, e la ragione l'ha trovata un test.
    # `head_version` referenzia `inventory_versions`, quindi questa tabella è
    # dipendente da quella: qualunque `TRUNCATE inventory_versions CASCADE` — che
    # tutte le suite di integrazione fanno per ripulire — porta via anche la riga di
    # stato. Con una riga seminata la si ritroverebbe misteriosamente sparita; senza,
    # il comportamento è coerente con il significato, perché se non esiste nessuna
    # versione la proiezione non può rispecchiarne una. La fase 2B scriverà lo stato
    # con un `INSERT ... ON CONFLICT`, non con un `UPDATE` che presume una riga.
    op.create_table(
        "inventory_state",
        sa.Column("id", sa.Boolean, primary_key=True,
                  server_default=sa.text("TRUE")),
        # Versione che la proiezione RISPECCHIA. Serve a poter dire «le tabelle
        # corrispondono alla versione N» invece di sperarlo: è il confronto su cui si
        # regge la fase 2B, e poi il passaggio della 2D.
        sa.Column("head_version", sa.BigInteger,
                  sa.ForeignKey("inventory_versions.version")),
        sa.Column("schema_version", sa.Integer),
        # `manuale` ASSENTE e `manuale: []` sono due documenti diversi, e la
        # canonicalizzazione conserva la differenza (§8.14). Senza questo booleano
        # il primo salvataggio dopo la migrazione aggiungerebbe una radice che
        # nessuno ha creato, e comparirebbe nell'audit come una modifica mai fatta.
        sa.Column("has_manual", sa.Boolean, nullable=False,
                  server_default=sa.text("FALSE")),
        # Chiavi di radice che il modello non rappresenta. Con lo schema congelato è
        # sempre vuoto; esiste perché l'invariante non deve dipendere da quel fatto.
        sa.Column("root_extra", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("synchronised_at", sa.DateTime(timezone=True)),
        # Al massimo UNA riga. La chiave primaria su un booleano più questo controllo
        # rendono impossibile una seconda riga: due stati di radice sarebbero due
        # risposte alla domanda «quale versione rispecchiano le tabelle».
        sa.CheckConstraint("id IS TRUE", name="ck_state_singleton"),
    )

    # ============================================================ privilegi
    #
    # SOLO lettura, per entrambi i ruoli di runtime. Le tabelle esistono e nessuno
    # le scrive: la sincronizzazione arriva nella fase 2C, ed è quella migrazione a
    # dover concedere i privilegi di scrittura.
    #
    # Concederli adesso significherebbe lasciare in giro il permesso di modificare
    # lo stato operativo dell'inventario mesi prima che esista il codice che lo fa —
    # e un privilegio che non serve è un privilegio che può essere sfruttato
    # (§8.19). Le `REVOKE` esplicite mettono l'intenzione nello schema invece che in
    # un commento.
    #
    # Il popolamento della fase 2B gira come PROPRIETARIO dello schema, come le
    # migrazioni e il bootstrap: non ha bisogno di questi privilegi.
    for table in TABLES:
        for role in (API_ROLE, WORKER_ROLE):
            op.execute(f"GRANT SELECT ON {table} TO {role}")
            op.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {table} "
                       f"FROM {role}")


def downgrade() -> None:
    op.drop_table("inventory_state")
    op.drop_table("inventory_manual_entries")
    op.drop_table("inventory_devices")
    op.drop_table("inventory_racks")
    op.drop_table("inventory_rooms")
    op.drop_table("inventory_locations")
