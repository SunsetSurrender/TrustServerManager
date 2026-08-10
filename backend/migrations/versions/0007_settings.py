"""impostazioni tipizzate con revisione, e contatore degli invii di prova

Due tabelle, entrambe al servizio di `/api/settings` e `/api/notifications/test`
(§8.38).

1. `settings`: RIGA UNICA. Non è una tabella chiave/valore, e non lo è di
   proposito: una chiave/valore libera è un archivio in cui prima o poi finisce
   una password, perché niente glielo impedisce. Qui il documento è un `jsonb`
   il cui contenuto è imposto dallo schema tipizzato dell'applicazione, e la
   riga esiste dal primo istante — il servizio non ha uno stato «non ancora
   configurato» in cui possa comportarsi diversamente.

   `version` è la revisione monotona per la concorrenza ottimistica. Serve
   perché due amministratori che aprono la stessa schermata e salvano a distanza
   di un minuto non devono sovrascriversi in silenzio: il secondo deve sapere
   che qualcosa è cambiato sotto.

   Privilegi: SELECT e UPDATE. **Niente INSERT**, come per `inventory_head`
   (§8.19): la riga nasce qui, una volta sola, e il ruolo di runtime non ha il
   privilegio di crearne una seconda. Niente DELETE: la configurazione non si
   cancella, si modifica.

2. `notification_test_attempts`: contatore durevole degli invii di prova. In
   memoria non servirebbe a niente — si azzera al riavvio, e non sopravvive a
   più repliche. La limitazione è separata da quella degli accessi (§8.28)
   perché protegge da un danno diverso: non un attaccante che indovina
   password, ma una sessione di amministratore compromessa usata per generare
   posta.

Revision ID: 0007_settings
Revises: 0006_audit_read
Create Date: 2026-08-07
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0007_settings"
down_revision: Union[str, None] = "0006_audit_read"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNTIME_ROLE = "tsm_api"

#: Documento iniziale. DEVE coincidere con la forma canonica prodotta da
#: `app.settings.schema.canonicalise({})`: un test lo verifica, perché se i due
#: divergessero la prima GET restituirebbe un documento che la successiva PUT
#: considera «modificato», e la revisione salirebbe senza che nessuno abbia
#: cambiato nulla.
#:
#: `enabled` è FALSO all'inizio. Un'installazione nuova non deve cominciare a
#: mandare posta perché qualcuno l'ha accesa e nessuno l'ha ancora configurata.
DEFAULT_SETTINGS = {
    "notifications": {
        "enabled": False,
        "timezone": "Europe/Rome",
        "warningDays": [30],
        "recipients": [],
        "schedule": {"hour": 8, "minute": 0},
    },
}


def upgrade() -> None:
    # --- 1. impostazioni: riga unica, documento tipizzato, revisione monotona ---
    op.create_table(
        "settings",
        # `id` fissato a 1 da un CHECK: la singolarità è un vincolo del database,
        # non una convenzione del codice. Senza, una seconda riga inserita per
        # errore renderebbe non deterministico quale configurazione è quella viva.
        sa.Column("id", sa.SmallInteger, primary_key=True,
                  server_default=sa.text("1")),
        sa.Column("version", sa.BigInteger, nullable=False,
                  server_default=sa.text("1")),
        sa.Column("data", pg.JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        # Chi ha salvato per ultimo, per comodità di lettura.
        #
        # `ON DELETE SET NULL`, al contrario di `audit.actor_user_id` che non ce
        # l'ha di proposito. La differenza è quale delle due è la fonte
        # autorevole: chi ha cambiato le impostazioni lo dice la RIGA DI AUDIT,
        # che è append-only e conserva `actor_username` come istantanea storica
        # (§8.30). Questa colonna è una scorciatoia, e una scorciatoia non deve
        # poter bloccare per sempre una manutenzione legittima del proprietario
        # dello schema — cosa che farebbe, perché la riga delle impostazioni è
        # unica e non viene mai cancellata.
        #
        # §8.6 resta in piedi: è `audit` a impedire la cancellazione di un'utenza,
        # e il ruolo di runtime non ha comunque il privilegio `DELETE`.
        sa.Column("updated_by", pg.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_settings_singleton"),
        sa.CheckConstraint("version > 0", name="ck_settings_version_positive"),
    )
    # `CAST(:d AS jsonb)` con un parametro di TESTO, e non un bindparam tipizzato
    # JSONB: passando una stringa già serializzata a un parametro JSONB, il driver
    # la serializza una seconda volta e nella colonna finisce una *stringa* JSON
    # (`"{\"notifications\": …}"`) invece di un oggetto. Il difetto non si vede
    # leggendo il valore da Python — `json.loads` lo apre comunque — ma qualsiasi
    # query che usi gli operatori jsonb (`data -> 'notifications'`) restituisce
    # NULL, e lo scoprirebbe lo scheduler, non questa migrazione.
    op.execute(
        sa.text("INSERT INTO settings (id, version, data) "
                "VALUES (1, 1, CAST(:d AS jsonb))")
        .bindparams(d=json.dumps(DEFAULT_SETTINGS))
    )

    # --- 2. invii di prova: contatore durevole per la limitazione ---
    op.create_table(
        "notification_test_attempts",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        # Come sopra: il contatore serve a limitare, non a documentare. Chi ha
        # chiesto l'invio lo dice l'audit.
        sa.Column("actor_user_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("ip", pg.INET),
    )
    # Le due finestre che il limitatore interroga: complessiva e per attore.
    op.create_index("ix_notif_test_ts", "notification_test_attempts",
                    [sa.text("ts DESC")])
    op.create_index("ix_notif_test_actor_ts", "notification_test_attempts",
                    ["actor_user_id", sa.text("ts DESC")])

    # --- 3. privilegi del ruolo di runtime ---
    # Legge e modifica la riga; non può crearne un'altra né cancellarla.
    op.execute(f"GRANT SELECT, UPDATE ON settings TO {RUNTIME_ROLE}")
    op.execute(f"REVOKE INSERT, DELETE, TRUNCATE ON settings FROM {RUNTIME_ROLE}")
    # Conta e accoda. La potatura è manutenzione del proprietario, come per
    # `login_attempts`: senza DELETE, un difetto non può azzerare il contatore.
    op.execute(f"GRANT SELECT, INSERT ON notification_test_attempts TO {RUNTIME_ROLE}")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON notification_test_attempts "
               f"FROM {RUNTIME_ROLE}")


def downgrade() -> None:
    op.drop_table("notification_test_attempts")
    op.drop_table("settings")
