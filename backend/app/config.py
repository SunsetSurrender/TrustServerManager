"""Configurazione dell'API.

La password di Postgres arriva da un Docker secret montato in sola lettura, non da una
variabile d'ambiente: le variabili si vedono in `docker inspect` e nei log di crash.
Il piano (§8.7) applica la stessa regola alla password SMTP quando arriverà.
"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TSM_", extra="ignore")

    db_host: str = "db"
    db_port: int = 5432
    db_name: str = "tsm"
    db_user: str = "tsm"

    # percorso del secret, non il valore
    db_password_file: Path = Path("/run/secrets/postgres_password")

    # timeout corto: /api/ready deve rispondere in fretta anche a DB fermo,
    # altrimenti l'healthcheck di Compose va in timeout invece di dire "not ready".
    db_connect_timeout: int = 3

    app_version: str = "0.1.0-skeleton"

    def db_password(self) -> str:
        """Legge il secret a ogni chiamata: la rotazione non richiede un rebuild.

        Non si mette in cache il valore, ma il costo è una lettura di file locale
        per apertura di connessione, non per richiesta.
        """
        try:
            return self.db_password_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"secret non trovato: {self.db_password_file}. "
                "In Compose deve essere montato da `secrets:`."
            ) from exc
        except PermissionError as exc:
            # Errore classico: file del secret leggibile solo da root mentre il
            # container gira come utente non-root. Vedi backend/README.md.
            raise RuntimeError(
                f"secret non leggibile: {self.db_password_file}. "
                "Il file deve essere leggibile dall'utente del container (uid 10001)."
            ) from exc

    def sqlalchemy_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.db_user}:{self.db_password()}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
