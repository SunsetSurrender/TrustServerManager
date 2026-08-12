"""foto immutabili, riferimenti per versione, esecuzioni di manutenzione

Foto dei rack come oggetti binari FUORI dal documento versionato (§8.5), più le
due cose che rendono possibile cancellarne i byte senza rompere la storia.

1. `photos` — i byte, una volta sola. L'identità applicativa è un UUID, il
   contenuto è indirizzato dal suo `sha256`, che è UNIVOCO: caricare due volte la
   stessa immagine restituisce la stessa riga invece di raddoppiare lo spazio.
   Nessun `UPDATE` per il ruolo dell'API: una foto non si modifica, si sostituisce
   il riferimento in una versione nuova dell'inventario.

2. `inventory_photo_refs` — QUALE VERSIONE usa QUALE foto, dichiarato
   esplicitamente. È la tabella che permette alla garbage collection di essere
   corretta: la domanda «questa foto serve ancora?» diventa un `NOT EXISTS` su
   una chiave, invece di una scansione del testo del documento in testa.

   Determinare l'eleggibilità guardando solo l'inventario CORRENTE sarebbe il
   difetto peggiore possibile qui: la v20 referenzia la foto A, la v21 la
   sostituisce con B, e A «non serve più» — finché qualcuno non torna alla v20 e
   trova un riquadro rotto. I riferimenti storici sono intenzionali, quindi
   vengono registrati.

   ⚠ La chiave esterna `photo_id → photos(id)` è deliberatamente senza
   `ON DELETE`: il comportamento predefinito (NO ACTION) fa RIFIUTARE dal database
   la cancellazione di una foto ancora referenziata. È la difesa che regge se la
   query della GC viene riscritta male un giorno: il vincolo non dipende dal fatto
   che chi la riscrive si ricordi del problema.

3. `maintenance_runs` — identità durevole delle esecuzioni di manutenzione, con
   la stessa forma di `scheduler_runs` (chiave sulla data locale) ma tabella
   PROPRIA. Non è duplicazione: la GC delle foto e gli avvisi di scadenza sono due
   lavori indipendenti, e appoggiare la GC sul registro degli avvisi legherebbe il
   suo recupero al fatto che le notifiche siano accese — cioè spegnere gli avvisi
   fermerebbe anche la liberazione dello spazio.

Ruolo del worker
----------------
Qui nasce `tsm_worker`, distinto da `tsm_api`. La GC deve poter fare `DELETE` su
`photos`; l'API no, e non è una sfumatura: un difetto in una rotta HTTP non deve
poter cancellare byte che una versione storica dell'inventario referenzia. La
migrazione 0008 aveva dato al ruolo dell'API le tabelle del worker perché il
worker condivideva quel ruolo; qui la separazione diventa reale e quelle
concessioni vengono ritirate.

Nessuno dei due riceve la password del proprietario dello schema.

Revision ID: 0009_photos
Revises: 0008_reminders
Create Date: 2026-08-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0009_photos"
down_revision: Union[str, None] = "0008_reminders"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

API_ROLE = "tsm_api"
WORKER_ROLE = "tsm_worker"

#: Formati accettati, imposti anche dal database. L'elenco chiuso vive in due
#: posti — qui e in `app/photos/validate.py` — di proposito: il controllo
#: applicativo dà un errore comprensibile, il vincolo del database rende
#: impossibile scrivere altro anche per una via che oggi non esiste.
ALLOWED_MIME = ("image/jpeg", "image/png", "image/webp")

#: Lavori di manutenzione previsti. Aggiungerne uno richiede una migrazione, che
#: è il punto: un lavoro periodico nuovo è una decisione, non una stringa.
MAINTENANCE_JOBS = ("photo_gc",)


def upgrade() -> None:
    _mime_list = ", ".join(f"'{m}'" for m in ALLOWED_MIME)
    _job_list = ", ".join(f"'{j}'" for j in MAINTENANCE_JOBS)

    # ---------------------------------------------------------------- foto
    op.create_table(
        "photos",
        # `gen_random_uuid()` è nel core da PostgreSQL 13: nessuna estensione da
        # installare, che in rete chiusa è un problema in meno.
        sa.Column("id", pg.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("mime", sa.Text, nullable=False),
        sa.Column("bytes", pg.BYTEA, nullable=False),
        # Univoco: è il contenuto a decidere l'identità dei byte. Il vincolo È la
        # deduplicazione — non un indice per andare più veloce, ma l'impossibilità
        # di avere due righe con la stessa immagine.
        sa.Column("sha256", sa.Text, nullable=False, unique=True),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        # L'audit è il documento autorevole su chi ha caricato: qui il riferimento
        # serve solo a leggere comodamente, e sopravvive alla disattivazione
        # dell'utenza diventando NULL (§8.6).
        sa.Column("uploaded_by", pg.UUID(as_uuid=False),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"mime IN ({_mime_list})", name="ck_photo_mime"),
        # La dimensione dichiarata non può divergere dai byte serviti: è calcolata
        # dai byte stessi, quindi una risposta non può annunciare una lunghezza che
        # il contenuto non ha.
        sa.CheckConstraint("size_bytes = octet_length(bytes)",
                           name="ck_photo_size_matches_bytes"),
        sa.CheckConstraint("size_bytes > 0", name="ck_photo_not_empty"),
    )
    # La GC cerca per età; l'indice le evita di leggere tutta la tabella per
    # trovare le poche righe fuori dalla finestra di grazia.
    op.create_index("ix_photo_created", "photos", ["created_at"])

    # -------------------------------------------------- riferimenti espliciti
    op.create_table(
        "inventory_photo_refs",
        # CASCADE: se un giorno si introduce la potatura delle versioni storiche,
        # eliminare una versione porta via i suoi riferimenti — e solo allora la
        # foto può diventare eleggibile alla GC. La retention delle versioni
        # determina di fatto quella delle foto (§8.5).
        sa.Column("inventory_version", sa.BigInteger,
                  sa.ForeignKey("inventory_versions.version", ondelete="CASCADE"),
                  nullable=False),
        # ⚠ Nessun ON DELETE: il database RIFIUTA di cancellare una foto ancora
        # referenziata. Vedi la nota in testa al modulo.
        sa.Column("photo_id", pg.UUID(as_uuid=False),
                  sa.ForeignKey("photos.id"), nullable=False),
        sa.PrimaryKeyConstraint("inventory_version", "photo_id",
                                name="pk_inventory_photo_refs"),
    )
    # L'indice per la domanda della GC: «esiste ancora una versione che usa questa
    # foto?». Senza, ogni candidato costerebbe una scansione.
    op.create_index("ix_photo_refs_photo", "inventory_photo_refs", ["photo_id"])

    # ------------------------------------------- esecuzioni di manutenzione
    op.create_table(
        "maintenance_runs",
        sa.Column("job", sa.Text, nullable=False),
        # DATA LOCALE, come in `scheduler_runs`: la chiave primaria fa sia il
        # recupero di un'esecuzione perduta (la riga di oggi non c'è → si esegue)
        # sia la protezione dall'ora ripetuta del cambio ora d'autunno (la riga
        # c'è già → non si esegue).
        sa.Column("run_date", sa.Date, nullable=False),
        sa.Column("timezone", sa.Text, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("examined_count", sa.Integer),
        sa.Column("deleted_count", sa.Integer),
        sa.Column("outcome", sa.Text),
        sa.PrimaryKeyConstraint("job", "run_date", name="pk_maintenance_runs"),
        sa.CheckConstraint(f"job IN ({_job_list})", name="ck_maintenance_job"),
    )

    # ============================================================ privilegi
    #
    # API: legge e accoda. Non modifica una foto (è immutabile) e non ne cancella
    # i byte (li referenzia la storia).
    op.execute(f"GRANT SELECT, INSERT ON photos TO {API_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON photos FROM {API_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON inventory_photo_refs TO {API_ROLE}")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON inventory_photo_refs "
               f"FROM {API_ROLE}")
    # Sulle esecuzioni di manutenzione l'API non ha NIENTE: non è il suo lavoro, e
    # un privilegio che non serve è un privilegio che può essere sfruttato.

    # ---------------------------------------------------- ruolo del worker
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{WORKER_ROLE}') THEN
                CREATE ROLE {WORKER_ROLE} LOGIN NOINHERIT;
            END IF;
        END
        $$;
    """)
    # Si parte da zero, come per il ruolo dell'API (0003): nessun privilegio
    # ereditato o residuo, nessun DDL.
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {WORKER_ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {WORKER_ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {WORKER_ROLE}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {WORKER_ROLE}")

    # Stato del worker delle notifiche: macchina a stati, quindi UPDATE sì,
    # DELETE mai (§8.19).
    for table in ("reminder_deliveries", "reminders", "scheduler_runs"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {WORKER_ROLE}")
        op.execute(f"REVOKE DELETE, TRUNCATE ON {table} FROM {WORKER_ROLE}")
    # La riga del battito nasce nella migrazione 0008: nessun INSERT.
    op.execute(f"GRANT SELECT, UPDATE ON worker_heartbeat TO {WORKER_ROLE}")
    op.execute(f"REVOKE INSERT, DELETE, TRUNCATE ON worker_heartbeat "
               f"FROM {WORKER_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON maintenance_runs TO {WORKER_ROLE}")
    op.execute(f"REVOKE DELETE, TRUNCATE ON maintenance_runs FROM {WORKER_ROLE}")

    # Sola lettura di ciò che il worker deve valutare: il documento in testa, le
    # impostazioni correnti, il livello delle migrazioni.
    op.execute(f"GRANT SELECT ON inventory_versions TO {WORKER_ROLE}")
    op.execute(f"GRANT SELECT ON inventory_head TO {WORKER_ROLE}")
    op.execute(f"GRANT SELECT ON settings TO {WORKER_ROLE}")
    op.execute(f"GRANT SELECT ON alembic_version TO {WORKER_ROLE}")
    # Il registro si accoda, mai si riscrive — vale anche per il worker.
    op.execute(f"GRANT SELECT, INSERT ON audit TO {WORKER_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON audit FROM {WORKER_ROLE}")

    # ⚠ L'UNICO privilegio di cancellazione di tutto lo schema, e l'unico posto in
    # cui esiste: la GC delle foto orfane. Non c'è INSERT — il worker non carica
    # foto — e non c'è UPDATE — una foto è immutabile.
    op.execute(f"GRANT SELECT, DELETE ON photos TO {WORKER_ROLE}")
    op.execute(f"REVOKE INSERT, UPDATE, TRUNCATE ON photos FROM {WORKER_ROLE}")
    op.execute(f"GRANT SELECT ON inventory_photo_refs TO {WORKER_ROLE}")
    op.execute("REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON inventory_photo_refs "
               f"FROM {WORKER_ROLE}")

    # ------------------------------------ ritiro dal ruolo dell'API (0008)
    # La 0008 aveva concesso le tabelle del worker a `tsm_api` perché il worker
    # girava con quel ruolo. Adesso ha il proprio, e l'API non ha motivo di poter
    # scrivere lo stato di un processo che non è suo.
    for table in ("reminder_deliveries", "reminders", "scheduler_runs",
                  "worker_heartbeat"):
        op.execute(f"REVOKE ALL ON {table} FROM {API_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {WORKER_ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {WORKER_ROLE}")
    # Il ruolo non si elimina: potrebbe avere sessioni attive. Rimuoverlo è una
    # decisione operativa, non di schema (come in 0003).

    op.drop_table("maintenance_runs")
    op.drop_table("inventory_photo_refs")
    op.drop_table("photos")

    # Si ripristina ciò che la 0008 aveva concesso, altrimenti un
    # downgrade/upgrade lascerebbe il worker senza privilegi con l'unico sintomo
    # di un errore di permesso a runtime.
    for table in ("reminder_deliveries", "reminders", "scheduler_runs"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {API_ROLE}")
    op.execute(f"GRANT SELECT, UPDATE ON worker_heartbeat TO {API_ROLE}")
