#!/usr/bin/env python3
# Uso: python docs/manuale/build-scheda.py "docs/TSM - Scheda rapida.pdf"
import base64, sys, pathlib
from playwright.sync_api import sync_playwright
D = pathlib.Path(__file__).resolve().parent
logo = 'data:image/png;base64,' + base64.b64encode((D / 'logo.png').read_bytes()).decode()
html = ('<!doctype html><html lang="it"><head><meta charset="utf-8"><title>Trust Server Manager. Scheda rapida</title>'
        f'<style>{(D / "scheda.css").read_text(encoding="utf-8")}</style></head><body>'
        + (D / 'scheda.html').read_text(encoding='utf-8').replace('%%LOGO%%', logo) + '</body></html>')
tmp = D / '_scheda.html'; tmp.write_text(html, encoding='utf-8')
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(); pg.goto(tmp.as_uri()); pg.wait_for_timeout(400)
    pg.pdf(path=sys.argv[1], format='A4', print_background=True, prefer_css_page_size=True); b.close()
tmp.unlink()
from pypdf import PdfReader
print(sys.argv[1], len(PdfReader(sys.argv[1]).pages), 'pagine')
