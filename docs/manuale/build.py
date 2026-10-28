#!/usr/bin/env python3
# Uso: python docs/manuale/build.py "docs/TSM - Manuale tecnico e applicativo.pdf"
# Richiede: playwright (chromium), pdfplumber, pypdf.
import base64, re, sys, pathlib
from playwright.sync_api import sync_playwright
import pdfplumber
from pypdf import PdfReader, PdfWriter

D = pathlib.Path(__file__).resolve().parent
VERSIONE, DATA, CODICE = '0.4', '28 ottobre 2026', 'TT-TSM-MAN-01'
css = (D / 'style.css').read_text(encoding='utf-8')
body = (D / 'body.html').read_text(encoding='utf-8')
logo = base64.b64encode((D / 'logo.png').read_bytes()).decode()

def motivo(colore, cls):
    # Le tre barre del marchio, come elemento grafico.
    return (f'<svg class="{cls}" viewBox="0 0 175 135" xmlns="http://www.w3.org/2000/svg" fill="{colore}">'
            '<rect x="0" y="0" width="175" height="27" rx="5"/>'
            '<rect x="0" y="54" width="70" height="27" rx="5"/><rect x="106" y="54" width="69" height="27" rx="5"/>'
            '<rect x="0" y="108" width="70" height="27" rx="5"/><rect x="106" y="108" width="69" height="27" rx="5"/></svg>')

body = body.replace('%%MOTIVO%%', motivo('#eb0028', 'motivo'))

cover = f"""<style>@page {{ margin: 0; }}</style><div class="cover">
<img class="logo" src="data:image/png;base64,{logo}">
{motivo('#eb0028', 'motivo')}
<div class="titolo"><div class="tipo">Manuale tecnico e applicativo</div>
<h1>Trust Server Manager</h1>
<div class="sub">Gestione delle sale server</div></div>
<div class="classif">Uso interno</div>
<div class="dati"><div><span>Codice</span>{CODICE}</div><div><span>Versione</span>{VERSIONE}</div><div><span>Data</span>{DATA}</div><div><span>Stato</span>Bozza</div></div>
</div>"""

entries = re.findall(r'id="([^"]+)" data-toc="([^|"]+)\|([^"]+)"', body)

def toc(pages):
    out = ['<div class="toc"><h2>Indice</h2><ol>']
    for id_, n, t in entries:
        if n == '1':  out.append('<li class="part"><span class="t">Parte I. Manuale tecnico</span></li>')
        if n == '10': out.append('<li class="part"><span class="t">Parte II. Manuale applicativo</span></li>')
        if n == 'A':  out.append('<li class="part"><span class="t">Appendici</span></li>')
        out.append(f'<li><span class="n">{n}</span><span class="t"><a href="#{id_}">{t}</a></span>'
                   f'<span class="d"></span><span class="p">{pages.get(n, "")}</span></li>')
    return ''.join(out) + '</ol></div>'

def html(inner):
    return ('<!doctype html><html lang="it"><head><meta charset="utf-8">'
            f'<title>Trust Server Manager. Manuale tecnico e applicativo</title><style>{css}</style></head><body>{inner}</body></html>')

F = "font-family:'Public Sans',sans-serif;font-size:7.2px;color:#646e7b;"
header = (f'<div style="width:100%;{F}margin:0 20mm 0 22mm;padding-bottom:5px;border-bottom:.5px solid #d9dee5;'
          f'display:flex;justify-content:space-between;align-items:flex-end">'
          f'<img src="data:image/png;base64,{logo}" style="height:10px">'
          f'<span>Trust Server Manager · Manuale tecnico e applicativo</span></div>')
footer = (f'<div style="width:100%;{F}margin:0 20mm 0 22mm;padding-top:5px;border-top:.5px solid #d9dee5;'
          f'display:flex;justify-content:space-between">'
          f'<span>{CODICE} · Versione {VERSIONE} · {DATA}</span><span>Uso interno</span>'
          f'<span>Pagina <span class="pageNumber"></span> di <span class="totalPages"></span></span></div>')

def render(page, content, out, hf):
    tmp = D / '_tmp.html'
    tmp.write_text(html(content), encoding='utf-8')
    page.goto(tmp.as_uri()); page.wait_for_timeout(500)
    page.pdf(path=str(out), format='A4', print_background=True, prefer_css_page_size=True,
             display_header_footer=hf, header_template=header if hf else '<span></span>',
             footer_template=footer if hf else '<span></span>')
    tmp.unlink()

with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page()
    render(pg, cover, D / '_cover.pdf', False)
    pages = {}
    for _ in range(2):
        i = body.index('<!-- ================================================= PARTE I -->')
        render(pg, body[:i] + toc(pages) + body[i:], D / '_main.pdf', True)
        found = {}
        with pdfplumber.open(D / '_main.pdf') as pdf:
            for i, page in enumerate(pdf.pages):
                if i == 0 or ('Parte II. Manuale applicativo' in (page.extract_text() or '') and 'Appendici' in (page.extract_text() or '')): continue
                txt = page.extract_text() or ''
                for id_, n, t in entries:
                    if n not in found and re.search(r'(^|\n)' + re.escape(n) + r'\s*' + re.escape(t[:14]), txt):
                        found[n] = i + 1
        pages = found
    b.close()
print('pagine:', pages, 'mancanti:', [n for _, n, _ in entries if n not in pages])
w = PdfWriter()
for f in ('_cover.pdf', '_main.pdf'):
    for pg_ in PdfReader(D / f).pages: w.add_page(pg_)
    (D / f).unlink()
w.add_metadata({'/Title': 'Trust Server Manager. Manuale tecnico e applicativo', '/Author': 'Trust Technologies',
                '/Subject': f'{CODICE} versione {VERSIONE}'})
out = pathlib.Path(sys.argv[1]); w.write(out); print(out, len(w.pages), 'pagine')
