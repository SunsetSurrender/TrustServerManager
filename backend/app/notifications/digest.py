"""Composizione del digest delle scadenze. Un messaggio, non uno per dispositivo.

Perché un digest
----------------
Trenta dispositivi in scadenza sono trenta righe di una tabella, non trenta
email. Un avviso per dispositivo rende la casella inutilizzabile proprio quando
c'è più da guardare, e la prima cosa che fa chi lo riceve è creare una regola che
li sposta in una cartella — cioè disattivare la notifica senza dirlo a nessuno.

Testo NON attendibile
---------------------
Nomi di dispositivo, rack, sala e sito arrivano dall'inventario, cioè li scrive
un utente. Non finiscono MAI in un'intestazione, e i caratteri di controllo si
rimuovono prima di comporre il corpo: un nome come
`srv-01\\r\\nBcc: qualcuno@altrove.example` non deve poter aggiungere un
destinatario né spezzare la tabella. Oggetto, mittente e struttura del corpo sono
scritti qui.

Riferimento: BACKEND-PLAN.md §8.41.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from email.message import EmailMessage
from email.utils import formatdate

from app.notifications.expiry import KIND_LABEL

SUBJECT_PREFIX = "Trust Server Manager — scadenze"

#: Larghezza massima di un valore nel corpo. Un nome di dispositivo lungo
#: duemila caratteri non deve rendere illeggibile la tabella.
MAX_FIELD = 60

#: Tutto ciò che non è testo stampabile su una riga sola. `\r` e `\n` sono i
#: caratteri che contano — sono quelli con cui si prova a iniettare
#: un'intestazione — ma si rimuovono anche gli altri caratteri di controllo,
#: perché non hanno alcun uso legittimo in un nome di dispositivo.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def sanitise_field(value: object) -> str:
    """Un valore dell'inventario, reso sicuro per il CORPO del messaggio.

    Non è una difesa contro l'HTML: il messaggio è testo semplice e non c'è
    interprete che possa eseguire niente. È una difesa contro l'iniezione di
    intestazioni e contro la rottura della tabella, e per questo lavora sui
    caratteri di controllo invece che sui caratteri «sospetti». Un dispositivo
    che si chiama `<script>` va scritto `<script>`: è il suo nome.
    """
    text = _CONTROL.sub(" ", str(value if value is not None else "")).strip()
    if len(text) > MAX_FIELD:
        text = text[:MAX_FIELD - 1] + "…"
    return text or "—"


def _row(item, threshold: int) -> str:
    giorni = item.days_remaining
    quando = "oggi" if giorni == 0 else (
        "domani" if giorni == 1 else f"fra {giorni} giorni")
    return (f"  {item.expiry.isoformat()}  ({quando})\n"
            f"      dispositivo: {sanitise_field(item.device)}\n"
            f"      posizione:   {sanitise_field(item.location)}"
            f" / {sanitise_field(item.room)} / rack {sanitise_field(item.rack)}\n"
            f"      preavviso:   {threshold} giorni\n")


def build_digest(selected: list[dict], *, sender: str, recipients: list[str],
                 message_id: str, now: datetime,
                 today: date) -> EmailMessage:
    """Il digest. `selected` sono le voci scelte da `register_and_select`.

    Il `Message-ID` arriva da FUORI: è quello della consegna, e si riusa a ogni
    ritentativo (§8.41). Generarlo qui significherebbe un identificativo nuovo a
    ogni tentativo, cioè trasformare un ritentativo in un secondo avviso.
    """
    by_kind: dict[str, list[dict]] = {}
    for entry in selected:
        by_kind.setdefault(entry["item"].kind, []).append(entry)

    total = len(selected)
    parti: list[str] = [
        "Riepilogo delle scadenze in avvicinamento rilevate da "
        "Trust Server Manager.",
        "",
        f"Data del controllo: {today.isoformat()}",
        f"Voci in scadenza: {total}",
        "",
    ]

    for kind in ("garanzia", "supporto"):
        entries = by_kind.get(kind)
        if not entries:
            continue
        parti.append("=" * 68)
        parti.append(f"{KIND_LABEL[kind].upper()}  ({len(entries)})")
        parti.append("=" * 68)
        # Dentro ogni tipo si raggruppa per urgenza: chi legge deve vedere prima
        # ciò che scade prima, non l'ordine in cui l'inventario è scritto.
        for entry in sorted(entries, key=lambda e: (e["item"].days_remaining,
                                                    e["item"].device)):
            parti.append(_row(entry["item"], entry["threshold_days"]))
        parti.append("")

    parti += [
        "-" * 68,
        "Messaggio generato automaticamente: non rispondere.",
        "Le finestre di preavviso e i destinatari si configurano in "
        "Impostazioni → Notifiche scadenze.",
        "Gli elementi già scaduti non sono elencati qui: si consultano nella "
        "vista Scadenze dell'applicazione.",
    ]

    msg = EmailMessage()
    # Oggetto definito dal server. Contiene un CONTEGGIO, mai un nome che venga
    # dall'inventario: l'oggetto è un'intestazione.
    msg["Subject"] = f"{SUBJECT_PREFIX}: {total} in avvicinamento"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Message-ID"] = message_id
    msg["Date"] = formatdate(now.timestamp(), localtime=False)
    # `auto-generated`: chiede ai risponditori automatici di stare zitti, che è
    # ciò che evita un ciclo fra un fuori-sede e questo indirizzo.
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content("\n".join(parti))
    return msg
