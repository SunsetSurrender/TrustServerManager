"""Veste grafica delle email di TSM (0.4.3).

Ogni messaggio ha DUE corpi: testo semplice (sempre, e per primo) e HTML. I client
che non mostrano l'HTML — o chi lo ha disattivato per politica — leggono il testo,
che resta completo. L'HTML è costruito per i client di posta reali, Outlook
compreso: tabelle, stili in linea, niente CSS esterno, niente script, niente
immagini remote. Il logo viaggia DENTRO il messaggio (parte `related`, `cid:`):
un'immagine remota sarebbe bloccata da Outlook e richiederebbe un server
raggiungibile da chi legge.

Testo non attendibile: tutto ciò che arriva dall'inventario o da un utente passa
da `esc()`. L'HTML è scritto qui; i dati entrano solo come testo.
"""
from __future__ import annotations

import html as _html
import re
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

LOGO = Path(__file__).with_name("assets") / "logo-mail.png"

BLU = "#004691"
ROSSO = "#EB0028"
AMBRA = "#B26B00"
VERDE = "#1E7B3A"
GRIGIO = "#5B6573"
BORDO = "#DDE2E8"
FONDO = "#F3F5F8"

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def esc(value: object) -> str:
    """Testo → HTML sicuro. Toglie i caratteri di controllo e fa l'escape."""
    return _html.escape(_CONTROL.sub(" ", str(value if value is not None else "")), quote=True)


# ------------------------------------------------------------ blocchi

def p(testo_html: str) -> str:
    """Paragrafo. `testo_html` è GIÀ HTML (usare `esc` e `b` per i dati)."""
    return (f'<p style="margin:0 0 14px 0;font-size:14px;line-height:21px;color:#1D2530">'
            f"{testo_html}</p>")


def b(value: object) -> str:
    return f"<strong>{esc(value)}</strong>"


def titolo(testo: str, colore: str = BLU) -> str:
    return (f'<h1 style="margin:0 0 16px 0;font-size:20px;line-height:26px;'
            f'font-weight:700;color:{colore}">{esc(testo)}</h1>')


def sottotitolo(testo: str) -> str:
    return (f'<h2 style="margin:22px 0 10px 0;font-size:13px;line-height:18px;'
            f'font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:{BLU}">'
            f"{esc(testo)}</h2>")


def chiave_valore(righe: list[tuple[str, str]]) -> str:
    """Tabellina etichetta → valore. I valori sono GIÀ HTML."""
    tr = "".join(
        f'<tr><td style="padding:6px 12px 6px 0;font-size:13px;color:{GRIGIO};'
        f'white-space:nowrap;vertical-align:top">{esc(k)}</td>'
        f'<td style="padding:6px 0;font-size:13px;color:#1D2530;font-weight:600">{v}</td></tr>'
        for k, v in righe)
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:0 0 16px 0">{tr}</table>')


def codice(valore: str) -> str:
    """Il codice di recupero: grande, monospaziato, centrato, facile da ricopiare."""
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:6px 0 18px 0"><tr><td align="center" '
            f'style="background:{FONDO};border:1px solid {BORDO};border-radius:6px;padding:18px">'
            f'<span style="font-family:Consolas,\'Courier New\',monospace;font-size:28px;'
            f'letter-spacing:4px;font-weight:700;color:{BLU}">{esc(valore)}</span></td></tr></table>')


def pulsante(testo: str, url: str) -> str:
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:4px 0 18px 0"><tr><td style="background:{BLU};border-radius:5px">'
            f'<a href="{esc(url)}" style="display:inline-block;padding:11px 22px;font-size:14px;'
            f'font-weight:700;color:#FFFFFF;text-decoration:none">{esc(testo)}</a></td></tr></table>')


def avviso(testo_html: str, colore: str = AMBRA) -> str:
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:6px 0 16px 0"><tr><td style="border-left:4px solid {colore};'
            f'background:{FONDO};padding:12px 14px;font-size:13px;line-height:19px;color:#1D2530">'
            f"{testo_html}</td></tr></table>")


def tabella(intestazioni: list[str], righe: list[list[str]],
            colori_riga: list[str] | None = None) -> str:
    """Tabella dati. Le celle sono GIÀ HTML; `colori_riga` = bordo sinistro per riga."""
    th = "".join(
        f'<th align="left" style="padding:8px 10px;font-size:11px;letter-spacing:.04em;'
        f'text-transform:uppercase;color:#FFFFFF;background:{BLU};font-weight:700">{esc(h)}</th>'
        for h in intestazioni)
    corpo = []
    for i, r in enumerate(righe):
        bordo = (colori_riga or [""] * len(righe))[i]
        celle = "".join(
            f'<td style="padding:8px 10px;font-size:13px;line-height:18px;color:#1D2530;'
            f'border-bottom:1px solid {BORDO};vertical-align:top'
            + (f';border-left:4px solid {bordo}' if (j == 0 and bordo) else "")
            + f'">{c}</td>'
            for j, c in enumerate(r))
        sfondo = "#FFFFFF" if i % 2 == 0 else "#FAFBFC"
        corpo.append(f'<tr style="background:{sfondo}">{celle}</tr>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="border-collapse:collapse;margin:0 0 16px 0;border:1px solid {BORDO}">'
            f"<tr>{th}</tr>{''.join(corpo)}</table>")


def etichetta(testo: str, colore: str) -> str:
    return (f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
            f'font-size:11px;font-weight:700;color:#FFFFFF;background:{colore}">{esc(testo)}</span>')


# ------------------------------------------------------------ pagina

def pagina(corpo: str, *, anteprima: str, piede: str) -> str:
    """La pagina completa. `anteprima` è il testo che i client mostrano nell'elenco."""
    return f"""<!DOCTYPE html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>Trust Server Manager</title></head>
<body style="margin:0;padding:0;background:{FONDO};font-family:Segoe UI,Helvetica,Arial,sans-serif">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{esc(anteprima)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{FONDO}">
<tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0"
       style="width:640px;max-width:100%;background:#FFFFFF;border:1px solid {BORDO};border-radius:8px">
<tr><td style="padding:20px 28px 16px 28px;border-bottom:3px solid {ROSSO}">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
  <td><img src="cid:{{LOGO_CID}}" width="220" height="30" alt="Trust Technologies"
           style="display:block;border:0;width:220px;height:auto"></td>
  <td align="right" style="font-size:12px;color:{GRIGIO};font-weight:600">Server Manager</td>
  </tr></table></td></tr>
<tr><td style="padding:24px 28px 8px 28px">{corpo}</td></tr>
<tr><td style="padding:14px 28px 20px 28px;border-top:1px solid {BORDO};font-size:11px;
              line-height:16px;color:{GRIGIO}">{piede}</td></tr>
</table></td></tr></table></body></html>"""


PIEDE_STANDARD = ("Messaggio generato automaticamente da <strong>Trust Server Manager</strong>: "
                  "non rispondere a questo indirizzo.<br>Trust Technologies · uso interno")


def con_html(msg: EmailMessage, html_pagina: str) -> EmailMessage:
    """Aggiunge l'HTML (con il logo incorporato) a un messaggio che ha già il testo.

    Struttura risultante:
        multipart/alternative
          ├─ text/plain           (il testo di prima, invariato)
          └─ multipart/related
               ├─ text/html
               └─ image/png  (Content-ID del logo)
    """
    cid = make_msgid(domain="tsm.local")
    msg.add_alternative(html_pagina.replace("{LOGO_CID}", cid[1:-1]), subtype="html")
    try:
        dati = LOGO.read_bytes()
    except OSError:
        return msg                              # senza logo, il messaggio parte comunque
    html_part = msg.get_payload()[-1]
    html_part.add_related(dati, maintype="image", subtype="png", cid=cid,
                          filename="trust-technologies.png", disposition="inline")
    return msg
