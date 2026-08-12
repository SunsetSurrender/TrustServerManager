"""promemoria di scadenza durevoli, consegne, esecuzioni pianificate, battito

Stato del worker delle notifiche (§8.41). Quattro tabelle, ognuna con un compito
preciso, e i vincoli che rendono impossibili gli errori che contano.

1. `reminders` — UN promemoria logico. La chiave unica
   `(entity_uid, expiry_kind, expiry_date, threshold_days)` è ciò che rende il
   worker idempotente: due worker che valutassero lo stesso inventario nello
   stesso momento non possono creare due volte lo stesso promemoria, perché il
   database non glielo permette. Ed è anche il motivo per cui **cambiare la data
   di scadenza di un dispositivo apre un ciclo di vita nuovo**: la data è parte
   della chiave, quindi il promemoria vecchio resta «inviato» e quello nuovo
   nasce da zero, senza codice dedicato.

2. `reminder_deliveries` — UNA consegna, cioè un digest. Porta il `Message-ID`
   generato dal server, che si **riusa a ogni ritentativo**: se il relay ha
   accettato il messaggio e il processo è morto prima di registrarlo, il
   ritentativo manda un messaggio con lo stesso identificativo, e un client di
   posta può riconoscerlo come duplicato. Non elimina il duplicato — niente lo
   può fare con SMTP (§8.41) — ma lo rende riconoscibile.

3. `scheduler_runs` — la DATA LOCALE di ogni esecuzione, come chiave primaria.
   È la parte che risolve due problemi in una riga:
     - il recupero di un'esecuzione perduta: se la macchina era spenta all'ora
       prevista, alla riaccensione la riga di oggi non c'è e l'esecuzione parte;
     - l'ora ripetuta del cambio ora d'autunno: alle 02:30 che accade DUE volte,
       la seconda trova la riga di oggi già presente e non manda niente.
   Nessuna delle due dipende dalla memoria di un processo.

4. `worker_heartbeat` — riga unica con l'ultimo battito, per il monitoraggio e
   per l'healthcheck del container. `/api/ready` NON la guarda: l'API deve
   restare pronta anche con il worker fermo (§8.41).

Revision ID: 0008_reminders
Revises: 0007_settings
Create Date: 2026-08-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0008_reminders"
down_revision: Union[str, None] = "0007_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNTIME_ROLE = "tsm_api"


def upgrade() -> None:
    # ------------------------------------------------------------ consegne
    op.create_table(
        "reminder_deliveries",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        # Univoco: l'identificativo del messaggio è l'identità della consegna, e
        # due consegne non possono condividerlo.
        sa.Column("message_id", sa.Text, nullable=False, unique=True),
        sa.Column("state", sa.Text, nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_after", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        # Istantanea dei destinatari, per l'auditabilità. Si registra l'IMPRONTA
        # e il numero, non l'elenco: a chi legge il registro serve sapere se la
        # configurazione era quella, non ricopiare indirizzi di persone in una
        # seconda tabella (§8.41).
        sa.Column("recipients_hash", sa.Text),
        sa.Column("recipients_count", sa.Integer),
        sa.Column("reminder_count", sa.Integer, nullable=False, server_default="0"),
        # Categoria da un elenco chiuso, mai il testo dell'errore SMTP: quello
        # contiene host, utenza e risposta del relay.
        sa.Column("failure_category", sa.Text),
        # `retry_exhausted` e non `abandoned`: dopo l'attesa i promemoria di questa
        # consegna tornano eleggibili e finiscono in un digest nuovo, quindi
        # «abbandonato» direbbe a chi legge che un avviso è stato perso quando è
        # soltanto in attesa. Lo stato chiude la CONSEGNA, non il promemoria.
        sa.CheckConstraint("state IN ('pending','sent','retry_exhausted')",
                           name="ck_delivery_state"),
        sa.CheckConstraint("attempts >= 0", name="ck_delivery_attempts"),
    )
    # L'indice serve alla domanda che il worker fa a ogni giro: c'è una consegna
    # da ritentare?
    op.create_index("ix_delivery_retryable", "reminder_deliveries",
                    ["state", "next_attempt_after"])

    # ---------------------------------------------------------- promemoria
    op.create_table(
        "reminders",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("entity_uid", pg.UUID(as_uuid=False), nullable=False),
        sa.Column("expiry_kind", sa.Text, nullable=False),
        sa.Column("expiry_date", sa.Date, nullable=False),
        sa.Column("threshold_days", sa.Integer, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="pending"),
        # ON DELETE non serve: le consegne non si cancellano.
        sa.Column("delivery_id", sa.BigInteger,
                  sa.ForeignKey("reminder_deliveries.id"), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        # Attesa dopo una consegna con i tentativi esauriti: evita che un relay
        # guasto faccia ricreare un digest a ogni giro, per sempre.
        sa.Column("hold_until", sa.DateTime(timezone=True)),
        sa.CheckConstraint("expiry_kind IN ('garanzia','supporto')",
                           name="ck_reminder_kind"),
        sa.CheckConstraint("state IN ('pending','sent','superseded')",
                           name="ck_reminder_state"),
        # Soglia positiva: `warningDays = 0` (avviso il giorno della scadenza)
        # sarebbe un cambiamento di prodotto, non un effetto collaterale di
        # questa implementazione (§8.41).
        sa.CheckConstraint("threshold_days > 0", name="ck_reminder_threshold"),
        # ⚠ IL vincolo di questo commit. Senza, due worker creerebbero due
        # promemoria identici e manderebbero due email.
        sa.UniqueConstraint("entity_uid", "expiry_kind", "expiry_date",
                            "threshold_days", name="uq_reminder_identity"),
    )
    op.create_index("ix_reminder_pending", "reminders", ["state", "hold_until"])
    op.create_index("ix_reminder_delivery", "reminders", ["delivery_id"])

    # ------------------------------------------------ esecuzioni pianificate
    op.create_table(
        "scheduler_runs",
        # La DATA LOCALE come chiave primaria: una sola esecuzione per giorno di
        # calendario nel fuso configurato, imposta dal database.
        sa.Column("run_date", sa.Date, primary_key=True),
        sa.Column("timezone", sa.Text, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("due_count", sa.Integer),
        sa.Column("sent_count", sa.Integer),
        sa.Column("outcome", sa.Text),
    )

    # ------------------------------------------------------------- battito
    op.create_table(
        "worker_heartbeat",
        sa.Column("id", sa.Boolean, primary_key=True,
                  server_default=sa.text("TRUE")),
        sa.Column("last_tick_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("last_run_date", sa.Date),
        sa.Column("state", sa.Text, nullable=False, server_default="starting"),
        sa.Column("detail", sa.Text),
        sa.CheckConstraint("id IS TRUE", name="ck_heartbeat_singleton"),
    )
    op.execute("INSERT INTO worker_heartbeat (id) VALUES (TRUE)")

    # ------------------------------------------------------------ privilegi
    # Il worker gira con il ruolo di runtime, come l'API. Ha bisogno di
    # aggiornare lo stato operativo (non è storia: è una macchina a stati), ma
    # NON di cancellare niente — come per tutto il resto (§8.19), ciò che non si
    # può cancellare non può essere perso da un difetto.
    for table in ("reminder_deliveries", "reminders", "scheduler_runs"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {RUNTIME_ROLE}")
        op.execute(f"REVOKE DELETE, TRUNCATE ON {table} FROM {RUNTIME_ROLE}")
    # La riga del battito nasce qui: nessun INSERT per il runtime.
    op.execute(f"GRANT SELECT, UPDATE ON worker_heartbeat TO {RUNTIME_ROLE}")
    op.execute("REVOKE INSERT, DELETE, TRUNCATE ON worker_heartbeat "
               f"FROM {RUNTIME_ROLE}")


def downgrade() -> None:
    op.drop_table("worker_heartbeat")
    op.drop_table("scheduler_runs")
    op.drop_table("reminders")
    op.drop_table("reminder_deliveries")
