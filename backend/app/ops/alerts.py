"""Avvisi di sistema via email (0.5.0).

Ogni pochi minuti la sentinella (`app.ops.sentinel`) rilegge lo «Stato del
sistema» — le stesse voci e le stesse soglie della pagina e di Nagios — e:

  - apre un problema per ogni voce critica (o anche «da controllare», se così è
    configurato) e lo segnala per email;
  - lo ricorda ogni `repeat_hours` finché resta aperto;
  - quando la voce torna a posto chiude il problema e lo dice («risolto»);
  - manda i promemoria del certificato TLS a 60, 30 e 7 giorni dalla scadenza.

Destinatari: gli amministratori attivi con un'email (se `include_admins`) più gli
indirizzi aggiuntivi delle Impostazioni. Un'email per giro, con tutto dentro:
cinque problemi nello stesso momento sono un messaggio, non cinque.

Se l'invio non riesce, i problemi restano «da segnalare» e si riprova dopo
`RETRY_AFTER`: un relay fermo non deve produrre un tentativo ogni cinque minuti,
né far perdere l'avviso.

Lo storico (`system_alerts`) non si cancella: è quello che la scheda Diagnostica
mostra come «cosa è successo».
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.notifications import mailtheme as T

log = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Rome")
RETRY_AFTER = timedelta(minutes=30)
RESOLVED_NOTICE_WINDOW = timedelta(hours=24)
CERT_THRESHOLDS = (60, 30, 7)
MAX_EXTRA = 20
_EMAIL = re.compile(r"^[^@\s,;<>\"']{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,}$")

SENT = "system.alerts.sent"
FAILED = "system.alerts.failed"
ACTOR = "(sentinella)"


# ------------------------------------------------------------ configurazione

def load_config(conn: Connection) -> dict:
    r = conn.execute(text("""
        SELECT enabled, include_admins, extra_recipients, min_level, repeat_hours,
               last_send_attempt_at, last_send_ok_at, last_send_error, last_test_at,
               last_run_at, updated_at, updated_by
          FROM system_alert_config WHERE id = 1""")).first()
    if r is None:
        return {"enabled": False, "includeAdmins": True, "extraRecipients": [],
                "minLevel": "crit", "repeatHours": 24}
    return {"enabled": r[0], "includeAdmins": r[1], "extraRecipients": list(r[2] or []),
            "minLevel": r[3], "repeatHours": r[4],
            "lastSendAttemptAt": r[5], "lastSendOkAt": r[6], "lastSendError": r[7],
            "lastTestAt": r[8], "lastRunAt": r[9], "updatedAt": r[10], "updatedBy": r[11]}


class ConfigRejected(ValueError):
    def __init__(self, field_name: str, message: str):
        super().__init__(message)
        self.field = field_name


def normalise_config(data: dict) -> dict:
    extra = data.get("extraRecipients") or []
    if isinstance(extra, str):
        extra = re.split(r"[\s,;]+", extra)
    pulite, visti = [], set()
    for e in extra:
        e = str(e).strip()
        if not e:
            continue
        if not _EMAIL.match(e):
            raise ConfigRejected("extraRecipients", f"indirizzo non valido: {e[:80]}")
        if e.lower() not in visti:
            visti.add(e.lower())
            pulite.append(e)
    if len(pulite) > MAX_EXTRA:
        raise ConfigRejected("extraRecipients", f"al massimo {MAX_EXTRA} indirizzi")
    livello = data.get("minLevel", "crit")
    if livello not in ("crit", "warn"):
        raise ConfigRejected("minLevel", "livello non valido")
    try:
        ore = int(data.get("repeatHours", 24))
    except (TypeError, ValueError):
        raise ConfigRejected("repeatHours", "numero di ore non valido") from None
    if not 1 <= ore <= 168:
        raise ConfigRejected("repeatHours", "fra 1 e 168 ore")
    return {"enabled": bool(data.get("enabled", True)),
            "includeAdmins": bool(data.get("includeAdmins", True)),
            "extraRecipients": pulite, "minLevel": livello, "repeatHours": ore}


def save_config(conn: Connection, data: dict, *, by: str) -> dict:
    c = normalise_config(data)
    conn.execute(text("""
        UPDATE system_alert_config
           SET enabled = :en, include_admins = :ia, extra_recipients = :ex,
               min_level = :lv, repeat_hours = :rh, updated_at = now(), updated_by = :by
         WHERE id = 1"""), {"en": c["enabled"], "ia": c["includeAdmins"],
                            "ex": c["extraRecipients"], "lv": c["minLevel"],
                            "rh": c["repeatHours"], "by": by})
    return load_config(conn)


def recipients(conn: Connection, cfg: dict) -> list[str]:
    out, visti = [], set()
    if cfg.get("includeAdmins"):
        for (e,) in conn.execute(text("""
                SELECT email FROM users WHERE role = 'admin' AND disabled_at IS NULL
                   AND coalesce(email, '') <> '' ORDER BY username""")).all():
            if e.lower() not in visti:
                visti.add(e.lower())
                out.append(e)
    for e in cfg.get("extraRecipients") or []:
        if e.lower() not in visti:
            visti.add(e.lower())
            out.append(e)
    return out


# ------------------------------------------------------------ valutazione

@dataclass
class Plan:
    new: list = field(default_factory=list)        # (id, key, level, title, detail, since)
    repeat: list = field(default_factory=list)
    resolved: list = field(default_factory=list)
    reminders: list = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.new or self.repeat or self.resolved or self.reminders)

    def problem_ids(self) -> list[int]:
        return [x[0] for x in self.new + self.repeat]


def _key(item: dict) -> str:
    return f"{item['gruppo']} · {item['nome']}"


def evaluate(conn: Connection, items: list[dict], *, now: datetime, cfg: dict,
             cert_expiry: datetime | None) -> Plan:
    """Aggiorna `system_alerts` e dice cosa va segnalato. Nella transazione del
    chiamante; non manda niente."""
    livelli = ("crit",) if cfg.get("minLevel", "crit") == "crit" else ("crit", "warn")
    problemi = {_key(i): i for i in items if i["stato"] in livelli}
    aperti = {r[1]: r for r in conn.execute(text("""
        SELECT id, key, notified_at, first_seen_at FROM system_alerts
         WHERE kind = 'problem' AND resolved_at IS NULL""")).all()}
    ripeti = timedelta(hours=int(cfg.get("repeatHours") or 24))
    plan = Plan()

    for key, it in problemi.items():
        row = aperti.get(key)
        if row is None:
            rid = conn.execute(text("""
                INSERT INTO system_alerts (key, kind, level, title, detail, first_seen_at,
                                           last_seen_at)
                VALUES (:k, 'problem', :lv, :t, :d, :now, :now) RETURNING id"""),
                {"k": key, "lv": it["stato"], "t": key, "d": it["valore"], "now": now}).scalar()
            plan.new.append((rid, key, it["stato"], key, it["valore"], now))
        else:
            conn.execute(text("""
                UPDATE system_alerts SET last_seen_at = :now, level = :lv, detail = :d
                 WHERE id = :id"""), {"now": now, "lv": it["stato"], "d": it["valore"],
                                      "id": row[0]})
            if row[2] is None:
                plan.new.append((row[0], key, it["stato"], key, it["valore"], row[3]))
            elif row[2] < now - ripeti:
                plan.repeat.append((row[0], key, it["stato"], key, it["valore"], row[3]))

    for key, row in aperti.items():
        if key not in problemi:
            conn.execute(text("UPDATE system_alerts SET resolved_at = :now WHERE id = :id"),
                         {"now": now, "id": row[0]})

    # Risolti da comunicare: solo quelli già segnalati, e recenti (dopo un lungo
    # periodo senza invii non si manda l'elenco di tutto quello che è passato).
    for r in conn.execute(text("""
            SELECT id, key, level, title, detail, first_seen_at, resolved_at
              FROM system_alerts
             WHERE kind = 'problem' AND resolved_at IS NOT NULL
               AND notified_at IS NOT NULL AND resolved_notified_at IS NULL
               AND resolved_at > :lim ORDER BY resolved_at"""),
            {"lim": now - RESOLVED_NOTICE_WINDOW}).all():
        plan.resolved.append(r)

    # ---- promemoria del certificato ----
    if cert_expiry is not None:
        giorni = (cert_expiry - now).days
        scadenza = cert_expiry.date().isoformat()
        for soglia in CERT_THRESHOLDS:
            if giorni <= soglia:
                conn.execute(text("""
                    INSERT INTO system_alerts (key, kind, level, title, detail,
                                               first_seen_at, last_seen_at, resolved_at)
                    SELECT :k, 'reminder', :lv, :t, :d, :now, :now, :now
                     WHERE NOT EXISTS (SELECT 1 FROM system_alerts WHERE key = :k)"""),
                    {"k": f"cert:{soglia}:{scadenza}",
                     "lv": "crit" if soglia <= 7 else "warn",
                     "t": "Certificato TLS in scadenza",
                     "d": f"scade il {cert_expiry.astimezone(TZ):%d/%m/%Y} ({giorni} giorni)",
                     "now": now})
        pendenti = conn.execute(text("""
            SELECT id, key, level, title, detail FROM system_alerts
             WHERE kind = 'reminder' AND notified_at IS NULL AND key LIKE 'cert:%'
               AND key LIKE :suffix"""), {"suffix": f"%:{scadenza}"}).all()
        if pendenti:
            # Solo il più urgente nel messaggio; tutti si segnano come comunicati.
            urgente = min(pendenti, key=lambda r: int(r[1].split(":")[1]))
            plan.reminders.append((urgente, [r[0] for r in pendenti]))

    # ---- interventi del ripristino automatico (ops/autoheal, lato VM) ----
    # Il timer sulla VM, quando riavvia qualcosa, lascia una riga qui: così chi
    # riceve gli avvisi sa che TSM si è «curato» da solo, e perché.
    for r in conn.execute(text("""
            SELECT id, key, level, title, detail FROM system_alerts
             WHERE kind = 'reminder' AND notified_at IS NULL AND key LIKE 'autoheal:%'
               AND first_seen_at > :lim ORDER BY first_seen_at LIMIT 20"""),
            {"lim": now - RESOLVED_NOTICE_WINDOW}).all():
        plan.reminders.append((r, [r[0]]))
    return plan


# ------------------------------------------------------------ messaggio

_LIV = {"crit": ("CRITICO", T.ROSSO), "warn": ("DA CONTROLLARE", T.AMBRA),
        "info": ("INFO", T.GRIGIO)}


def _quando(ts: datetime) -> str:
    return ts.astimezone(TZ).strftime("%d/%m %H:%M")


def build_message(sender: str, to: list[str], plan: Plan, *, now: datetime,
                  public_origin: str = "", test: bool = False,
                  open_problems: list | None = None) -> EmailMessage:
    aperti = plan.new + plan.repeat
    n_crit = sum(1 for x in aperti if x[2] == "crit")
    if test:
        oggetto = "[TSM] Prova degli avvisi di sistema"
    elif len(aperti) == 1 and not plan.resolved:
        oggetto = f"[TSM] {_LIV[aperti[0][2]][0]}: {aperti[0][3]}"
    elif aperti:
        parti = [f"{len(aperti)} {'problema' if len(aperti) == 1 else 'problemi'}"]
        if plan.resolved:
            parti.append(f"{len(plan.resolved)} {'risolto' if len(plan.resolved) == 1 else 'risolti'}")
        oggetto = "[TSM] " + ("CRITICO — " if n_crit else "") + ", ".join(parti)
    elif plan.resolved:
        oggetto = (f"[TSM] Risolto: {plan.resolved[0][3]}" if len(plan.resolved) == 1
                   else f"[TSM] {len(plan.resolved)} problemi risolti")
    else:
        primo = plan.reminders[0][0]
        oggetto = ("[TSM] Promemoria: certificato TLS in scadenza" if primo[1].startswith("cert:")
                   else f"[TSM] {primo[3]}")

    msg = EmailMessage()
    msg["Subject"] = oggetto
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Auto-Submitted"] = "auto-generated"

    righe_txt = []
    corpo = ""
    if test:
        corpo += (T.titolo("Prova degli avvisi di sistema", T.VERDE)
                  + T.p("Se leggi questo messaggio, <strong>gli avvisi di sistema ti "
                        "raggiungeranno</strong>: quando una voce dello Stato del sistema "
                        "diventa critica, TSM scrive a questo indirizzo."))
        righe_txt.append("Prova degli avvisi di sistema: se leggi questo messaggio, gli avvisi "
                         "ti raggiungeranno.")
        open_problems = open_problems or []
        if open_problems:
            corpo += T.sottotitolo("Problemi aperti adesso")
            corpo += T.tabella(["Livello", "Voce", "Valore"],
                               [[T.etichetta(*_LIV.get(r[2], _LIV["info"])), T.b(r[3]),
                                 T.esc(r[4] or "")] for r in open_problems])
            righe_txt += [f"- [{_LIV.get(r[2], _LIV['info'])[0]}] {r[3]}: {r[4] or ''}"
                          for r in open_problems]
        else:
            corpo += T.avviso("Al momento <strong>nessun problema aperto</strong>.", T.VERDE)
            righe_txt.append("Al momento nessun problema aperto.")
    if aperti:
        corpo += T.titolo(
            "Problemi da verificare" if len(aperti) > 1 else "Problema da verificare",
            T.ROSSO if n_crit else T.AMBRA)
        corpo += T.p("Trust Server Manager ha rilevato " + T.b(
            f"{len(aperti)} {'voce' if len(aperti) == 1 else 'voci'}")
            + " dello Stato del sistema fuori norma.")
        corpo += T.tabella(
            ["Livello", "Voce", "Valore", "Dal"],
            [[T.etichetta(*_LIV[x[2]]), T.b(x[3]) + (" <span style=\"color:#5B6573\">"
              "(promemoria)</span>" if x in plan.repeat else ""), T.esc(x[4] or ""),
              T.esc(_quando(x[5]))] for x in aperti],
            colori_riga=[_LIV[x[2]][1] for x in aperti])
        righe_txt.append("PROBLEMI DA VERIFICARE")
        righe_txt += [f"- [{_LIV[x[2]][0]}] {x[3]}: {x[4] or ''} (dal {_quando(x[5])})"
                      for x in aperti]
    if plan.resolved:
        corpo += T.sottotitolo("Tornati a posto")
        corpo += T.tabella(["Voce", "Ultimo valore", "Risolto alle"],
                           [[T.b(r[3]), T.esc(r[4] or ""), T.esc(_quando(r[6]))]
                            for r in plan.resolved],
                           colori_riga=[T.VERDE] * len(plan.resolved))
        righe_txt.append("RISOLTI")
        righe_txt += [f"- {r[3]} (alle {_quando(r[6])})" for r in plan.resolved]
    cert = [u for u, _ in plan.reminders if u[1].startswith("cert:")]
    heal = [u for u, _ in plan.reminders if u[1].startswith("autoheal:")]
    for urgente in cert:
        corpo += T.sottotitolo("Promemoria")
        corpo += T.avviso(T.b(urgente[3]) + ": " + T.esc(urgente[4]) + ". Il rinnovo si fa "
                          "sulla VM con <code>deploy/make-internal-tls.sh</code> e la stessa CA: "
                          "sul web gateway non serve alcuna modifica.",
                          T.ROSSO if urgente[2] == "crit" else T.AMBRA)
        righe_txt.append(f"PROMEMORIA: {urgente[3]}: {urgente[4]}")
    if heal:
        corpo += T.sottotitolo("Ripristino automatico")
        corpo += T.p("Il controllo sulla VM è intervenuto da solo. Se succede spesso, il "
                     "guasto va cercato: <code>journalctl -t tsm-autoheal</code>.")
        corpo += T.tabella(["Quando", "Cosa ha fatto"],
                           [[T.esc(_quando(_ts(h))), T.esc(h[4] or "")] for h in heal],
                           colori_riga=[T.AMBRA] * len(heal))
        righe_txt.append("RIPRISTINO AUTOMATICO")
        righe_txt += [f"- {h[4]}" for h in heal]

    dove = (f"{public_origin.split(',')[0].rstrip('/')}/" if public_origin else "l'applicazione")
    corpo += T.avviso("Dettagli in <strong>Impostazioni → Stato del sistema</strong> e "
                      "<strong>Diagnostica</strong>. Destinatari e livello degli avvisi si "
                      "cambiano in <strong>Impostazioni → Notifiche</strong>.", T.BLU)
    msg.set_content("Trust Server Manager — avvisi di sistema\n\n" + "\n".join(righe_txt)
                    + f"\n\nDettagli: {dove} → Impostazioni → Stato del sistema.\n"
                    + f"Generato il {now.astimezone(TZ):%d/%m/%Y alle %H:%M}.\n")
    anteprima = righe_txt[1] if len(righe_txt) > 1 else righe_txt[0] if righe_txt else oggetto
    return T.con_html(msg, T.pagina(corpo, anteprima=anteprima[:140], piede=T.PIEDE_STANDARD))


def _ts(row) -> datetime:
    """Istante di un intervento: è nella chiave, `autoheal:<epoch>[:n]`."""
    try:
        return datetime.fromtimestamp(float(row[1].split(":")[1]), tz=timezone.utc)
    except (IndexError, ValueError):
        return datetime.now(timezone.utc)


def mark_sent(conn: Connection, plan: Plan, *, now: datetime) -> None:
    ids = plan.problem_ids()
    if ids:
        conn.execute(text("UPDATE system_alerts SET notified_at = :now WHERE id = ANY(:ids)"),
                     {"now": now, "ids": ids})
    if plan.resolved:
        conn.execute(text("UPDATE system_alerts SET resolved_notified_at = :now "
                          " WHERE id = ANY(:ids)"),
                     {"now": now, "ids": [r[0] for r in plan.resolved]})
    for _u, rids in plan.reminders:
        conn.execute(text("UPDATE system_alerts SET notified_at = :now WHERE id = ANY(:ids)"),
                     {"now": now, "ids": rids})
