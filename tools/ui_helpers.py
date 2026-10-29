"""Aiuti comuni alle prove nel browser.

Dalla 0.4.3 Impostazioni è una pagina a schede: utenze, sicurezza accessi, stato
del sistema, notifiche e importazione non stanno più nel Profilo né in un menu a
tendina. `scheda(page, "utenti")` apre la pagina (se serve) e la scheda.
"""
from __future__ import annotations


def scheda(page, chiave: str, attesa_ms: int = 1200) -> None:
    if page.locator("#pagina-impostazioni").count() == 0:
        page.get_by_role("button", name="⚙ Impostazioni").first.click()
        page.wait_for_timeout(600)
    tab = page.locator(f"#tab-{chiave}")
    if tab.count() == 0:
        # Scheda non visibile per questo ruolo (es. operatore): la pagina resta
        # aperta sulla scheda Generale, e la prova verifica proprio questo.
        page.wait_for_timeout(attesa_ms)
        return
    tab.click()
    page.wait_for_timeout(attesa_ms)
