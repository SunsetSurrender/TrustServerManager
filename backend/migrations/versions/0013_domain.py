"""Fase 2G: presenza fisica e indirizzo interpretato.

Due colonne su `inventory_devices`, e sono di due specie diverse.

`presenza` — colonna TIPIZZATA, con una chiave nel documento
------------------------------------------------------------
Presenza FISICA dell'apparato nel rack: `presente` o `rimosso`. Sta accanto a `stato`
e non dentro: sono due domande indipendenti (§8.50). Un apparato `dismesso` può stare
fisicamente in sala per mesi e occupare unità che nessuno può assegnare; un apparato
`attivo` può essere stato portato altrove.

Torna nel documento, come `stato`: è un valore che l'utente scrive.

⚠ Nessun `CHECK` sul vocabolario, e non è una dimenticanza. L'inventario reale arriva
da fogli di calcolo e contiene sempre qualche valore fuori elenco; un vincolo qui
farebbe RIFIUTARE alla proiezione un documento che la fase 1 accetta, cioè cambierebbe
il comportamento del prodotto di straforo. `validate_model` lo segnala come AVVISO, che
è la cosa giusta: il dato si conserva e la stranezza si vede. È la stessa scelta già
fatta per `stato` e `type` (§8.42).

⚠ Nessun indice. La domanda è «quali dispositivi NON sono rimossi», che nel seed reale
è la quasi totalità delle righe: un indice su un predicato vero per il 99% dei casi non
viene usato dal pianificatore, e se venisse usato sarebbe più lento della scansione. Il
posto dove aggiungerlo — se un giorno i rimossi diventassero la maggioranza — è qui, e
la decisione va MISURATA, non presunta.

`ip_addr` — colonna DERIVATA, di tipo `inet`
-------------------------------------------
L'interpretazione di `ip`, esattamente come `garanzia_date` è l'interpretazione di
`garanzia`. `ip` resta TESTO e resta il dato autorevole: contiene nomi di host, campi
vuoti, `10.0.0.1/24`, e una colonna `inet` obbligatoria costringerebbe a scartare o a
reinterpretare quei valori.

⚠ Perché ora `inet` va bene, mentre nella fase 2E era stato escluso.

La 2E lo scartò per una ragione buona: `inet` ha una grammatica PROPRIA, che accetta
`10.1` come `10.0.0.1` e `10.0.0.0/8` come indirizzo — cioè aggiungerebbe semantica che
il prodotto non ha, e la aggiungerebbe in un solo posto dei tre. Quella obiezione
riguardava l'idea di far interpretare a PostgreSQL il testo dell'utente, e resta
valida.

Qui non succede. La colonna la scrive `domain.parse_address`, l'unico interprete di
indirizzi del prodotto (Python, SQL e frontend), e in `ip_addr` arriva soltanto una
forma già canonica. PostgreSQL riceve indirizzi normalizzati e li CONFRONTA — che è
quello che sa fare meglio di qualunque espressione: `>=`/`<=` su `inet` ordina per
famiglia e poi per indirizzo, quindi un intervallo IPv4 non può contenere un IPv6, e un
CIDR IPv6 diventa una condizione su due estremi invece di novantasei confronti di bit.

Sostituisce l'espressione `_IPNUM` della 2E — nove `btrim` e otto `split_part` per
riga, valutata a ogni ricerca — con una colonna indicizzabile. Ed è ciò che rende
possibili le due cose che §5 chiede: l'indirizzo ESATTO (`10.0.0.1` non trova
`10.0.0.100`) e IPv6.

`CHECK ip_addr IS NULL OR ip IS NOT NULL`: un indirizzo interpretato non può esistere
senza il testo da cui è stato interpretato. Stessa regola di
`ck_device_garanzia_date_needs_text`, e stessa ragione — impedisce lo stato in cui la
derivata sopravvive al dato.

Indice su `ip_addr`: sì, e MISURATO. La ricerca per indirizzo è la domanda «cade in
questo intervallo», che è precisamente ciò per cui un btree esiste; l'indice è PARZIALE
(`WHERE ip_addr IS NOT NULL`) perché nel seed reale una parte dei dispositivi non ha un
indirizzo interpretabile, e indicizzarne i NULL sarebbe indice sprecato. La misura sta
in §8.50.

MAPPER_VERSION
--------------
Passa da 1 a 2, ed è il caso per cui quel numero esiste. Le righe scritte dalla mappa
vecchia riassemblerebbero lo STESSO documento — quindi lo stesso digest — mentre
`presenza` starebbe in `extra` e `ip_addr` sarebbe vuota: la vista Capacità non
troverebbe la presenza e la ricerca non troverebbe l'indirizzo. Il digest non può
accorgersene (§8.44), la versione della mappa sì. Dopo questa migrazione la proiezione
si dichiara NON attuale finché non gira `project.py --rebuild`, e le rotte di lettura
rispondono 503 con `projection_not_current` invece di servire dati che non
corrispondono al codice.

Non è una migrazione di dati: le due colonne nascono NULL e le riempie la
ricostruzione. Nessun `UPDATE` di massa, nessuna riscrittura del documento.

Revision ID: 0013_domain
Revises: 0012_dual_write
Create Date: 2026-08-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0013_domain"
down_revision: Union[str, None] = "0012_dual_write"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "inventory_devices"


def upgrade() -> None:
    # ============================================ 1. presenza fisica
    op.add_column(TABLE, sa.Column("presenza", sa.Text))

    # ============================================ 2. indirizzo interpretato
    op.add_column(TABLE, sa.Column("ip_addr", pg.INET))
    op.create_check_constraint(
        "ck_device_ip_addr_needs_text", TABLE,
        "ip_addr IS NULL OR ip IS NOT NULL")
    op.create_index("ix_device_ip_addr", TABLE, ["ip_addr"],
                    postgresql_where=sa.text("ip_addr IS NOT NULL"))

    # Le colonne aggiunte a una tabella esistente ereditano i privilegi di tabella:
    # niente da concedere né da revocare. `tsm_worker` resta in sola lettura su tutto
    # lo schema dell'inventario, `tsm_api` conserva i suoi (§8.47.7).


def downgrade() -> None:
    op.drop_index("ix_device_ip_addr", table_name=TABLE)
    op.drop_constraint("ck_device_ip_addr_needs_text", TABLE, type_="check")
    op.drop_column(TABLE, "ip_addr")
    op.drop_column(TABLE, "presenza")
