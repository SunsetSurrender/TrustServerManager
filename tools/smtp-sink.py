#!/usr/bin/env python3
"""Un relay SMTP che non consegna niente: scrive su disco e risponde 250.

Serve alla qualificazione delle notifiche (§19, §20). Il worker deve parlare SMTP
con qualcuno, e quel qualcuno non può essere il relay aziendale — mandare posta
vera a destinatari veri per provare il software è precisamente ciò che l'item 14B
vieta di fare in questa fase.

Perché scritto a mano invece di usare una libreria
-------------------------------------------------
`smtpd` è stato rimosso da Python 3.12, `aiosmtpd` non è nell'immagine di
produzione e non deve entrarci: l'immagine è quella del rilascio, e aggiungere una
dipendenza per provarla significherebbe provarne un'altra. Il sottoinsieme di SMTP
che serve è piccolo — EHLO, MAIL FROM, RCPT TO, DATA, QUIT, più STARTTLS
RIFIUTATO — e sta in poche decine di righe di libreria standard.

⚠ NON è un server SMTP. Non fa autenticazione, non verifica gli indirizzi, non
consegna. Ascolta, registra, e dice sì. Non va esposto da nessuna parte.

Ogni messaggio finisce in un file JSON nella directory indicata:

    {"from": ..., "to": [...], "data": "...", "received_at": "..."}

Uso:
    python3 tools/smtp-sink.py --dir /tmp/posta --port 2525
    python3 tools/smtp-sink.py --dir /tmp/posta --fail-first 2   # per i ritentativi
"""
from __future__ import annotations

import argparse
import json
import os
import socketserver
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

DIR = Path("/tmp/posta")
#: Quanti dei prossimi comandi DATA devono essere RIFIUTATI con un 4xx transitorio.
#: Serve a provare i ritentativi e la stabilità del Message-ID senza dover rompere
#: la rete: un 421 è il modo in cui un relay vero dice «riprova più tardi».
FALLIRE = 0
_lock = threading.Lock()
_contatore = 0


class Sessione(socketserver.StreamRequestHandler):
    """Una conversazione SMTP. Niente pipelining, niente TLS: non servono."""

    timeout = 60

    def scrivi(self, testo: str) -> None:
        self.wfile.write((testo + "\r\n").encode("utf-8", "replace"))
        self.wfile.flush()

    def handle(self) -> None:  # noqa: C901
        global _contatore
        self.scrivi("220 tsm-sink pronto")
        mittente = ""
        destinatari: list[str] = []
        while True:
            riga = self.rfile.readline()
            if not riga:
                return
            testo = riga.decode("utf-8", "replace").strip()
            su = testo.upper()

            if su.startswith(("HELO", "EHLO")):
                # Si dichiarano SOLO le estensioni che si sanno fare. In
                # particolare NON si annuncia STARTTLS: annunciarlo e poi non
                # saperlo fare farebbe fallire il worker per un motivo che con le
                # notifiche non c'entra niente.
                self.scrivi("250-tsm-sink")
                self.scrivi("250 8BITMIME")
            elif su.startswith("MAIL FROM"):
                mittente = testo.partition(":")[2].strip()
                self.scrivi("250 ok")
            elif su.startswith("RCPT TO"):
                destinatari.append(testo.partition(":")[2].strip())
                self.scrivi("250 ok")
            elif su == "DATA":
                with _lock:
                    _contatore += 1
                    rifiuta = _contatore <= FALLIRE
                if rifiuta:
                    # 421: guasto TRANSITORIO. Il worker deve ritentare, non
                    # marcare la consegna come fallita per sempre.
                    self.scrivi("421 sink: rifiuto transitorio "
                                f"(numero {_contatore} di {FALLIRE})")
                    continue
                # 354 e non altro: `smtplib` confronta esattamente questo codice
                # dopo DATA e solleva `SMTPDataError` su qualunque altro.
                self.scrivi("354 avanti, chiudi con un punto su una riga sola")
                corpo: list[str] = []
                while True:
                    r = self.rfile.readline()
                    if not r:
                        return
                    t = r.decode("utf-8", "replace")
                    if t.rstrip("\r\n") == ".":
                        break
                    # Un punto raddoppiato in testa è la trasparenza di SMTP.
                    if t.startswith(".."):
                        t = t[1:]
                    corpo.append(t)
                self._salva(mittente, destinatari, "".join(corpo))
                self.scrivi("250 accettato")
                mittente, destinatari = "", []
            elif su.startswith("RSET"):
                mittente, destinatari = "", []
                self.scrivi("250 ok")
            elif su.startswith("NOOP"):
                self.scrivi("250 ok")
            elif su.startswith("STARTTLS"):
                # Non annunciato, quindi non dovrebbe arrivare. Se arriva, si dice
                # no in modo esplicito invece di far cadere la connessione.
                self.scrivi("454 TLS non disponibile su questo sink")
            elif su.startswith("QUIT"):
                self.scrivi("221 ciao")
                return
            else:
                self.scrivi("502 comando non implementato")

    def _salva(self, mittente: str, destinatari: list[str], dati: str) -> None:
        DIR.mkdir(parents=True, exist_ok=True)
        n = len(list(DIR.glob("msg-*.json"))) + 1
        (DIR / f"msg-{n:04d}.json").write_text(json.dumps({
            "from": mittente, "to": destinatari, "data": dati,
            "received_at": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[sink] messaggio {n}: da {mittente} a {destinatari} "
              f"({len(dati)} byte)", flush=True)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    global DIR, FALLIRE
    ap = argparse.ArgumentParser(description="relay SMTP di prova")
    ap.add_argument("--dir", default="/tmp/posta")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=2525)
    ap.add_argument("--fail-first", type=int, default=0,
                    help="rifiuta i primi N invii con un 421 transitorio")
    args = ap.parse_args()
    DIR = Path(args.dir)
    FALLIRE = args.fail_first
    DIR.mkdir(parents=True, exist_ok=True)

    # ⚠ Si prova a SCRIVERE adesso, e si esce se non si può.
    #
    # Senza questo controllo il sink accettava l'intera conversazione SMTP — EHLO,
    # MAIL FROM, RCPT TO, DATA, il corpo — e poi moriva con un `PermissionError` nel
    # salvare il file, chiudendo la connessione. Il worker vedeva
    # `SMTPServerDisconnected`, registrava `connection_failed` e metteva la consegna
    # in `pending`: comportamento corretto del worker, diagnosi impossibile per chi
    # guardava. Il container gira non-root (uid 10001, come in produzione) e la
    # directory montata era di root.
    #
    # Un relay che dice sì e non conserva è peggio di un relay che non parte.
    try:
        prova = DIR / ".prova-di-scrittura"
        prova.write_text("ok", encoding="utf-8")
        prova.unlink()
    except OSError as exc:
        print(f"[sink] NON POSSO SCRIVERE in {DIR}: {exc}\n"
              f"[sink] il processo gira come uid {os.getuid()}; dare a quella "
              f"directory un proprietario che possa scriverci.", file=sys.stderr,
              flush=True)
        return 2

    print(f"[sink] in ascolto su {args.host}:{args.port}, messaggi in {DIR} "
          f"(uid {os.getuid()})"
          + (f", i primi {FALLIRE} invii verranno rifiutati" if FALLIRE else ""),
          flush=True)
    with Server((args.host, args.port), Sessione) as s:
        s.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
