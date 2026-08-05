"""Configurazione dell'API.

La password del database arriva da un Docker secret montato in sola lettura, non
da una variabile d'ambiente: le variabili si vedono in `docker inspect` e nei log
di crash. Il piano (§8.7) applica la stessa regola alla password SMTP.
"""
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TSM_", extra="ignore")

    # ------------------------------------------------------------- ambiente
    #: `production` per default: un'installazione che non dichiara nulla deve
    #: essere quella sicura. Le deroghe si chiedono, non si ereditano.
    env: str = "production"

    db_host: str = "db"
    db_port: int = 5432
    db_name: str = "tsm"
    db_user: str = "tsm"

    # DSN completo, che scavalca i campi sopra e la lettura del secret. Serve ai
    # test di integrazione (TSM_DB_URL) e a un Postgres gestito con credenziali
    # fornite dall'infrastruttura. In produzione resta vuoto.
    db_url: str | None = None

    # percorso del secret, non il valore
    db_password_file: Path = Path("/run/secrets/postgres_password")

    # timeout corto: /api/ready deve rispondere in fretta anche a DB fermo,
    # altrimenti l'healthcheck va in timeout invece di dire "not ready".
    db_connect_timeout: int = 3

    app_version: str = "0.3.0"

    # ------------------------------------------------------------- sessioni
    #: Il cookie di sessione è `Secure` per default: senza HTTPS non si entra.
    #: In produzione la deroga è VIETATA e l'avvio fallisce (validatore sotto).
    cookie_secure: bool = True

    # --------------------------------------------------------------- origini
    #: Origine pubblica del servizio, per la validazione di `Origin` sulle
    #: richieste che modificano stato (§8.27). Più valori separati da virgola se
    #: il servizio è raggiungibile su più nomi.
    public_origin: str = ""

    #: Reti da cui accettare `X-Forwarded-For`. Solo il proxy davanti all'API:
    #: fidarsi dell'header da chiunque significa lasciare che il client dichiari
    #: il proprio IP, e con esso aggiri la limitazione dei tentativi (§8.28).
    trusted_proxies: str = "127.0.0.1,::1"

    # ------------------------------------------------- limitazione accessi
    login_max_failures_per_username: int = 5
    login_max_failures_per_ip: int = 20
    login_failure_window_seconds: int = 900

    # Limite della richiesta a livello applicativo. Nginx ha il proprio
    # (client_max_body_size): due livelli, perché uno solo salta quando qualcuno
    # raggiunge l'API senza passare dal proxy.
    max_request_bytes: int = 5 * 1024 * 1024

    # La documentazione OpenAPI elenca l'intera superficie dell'API.
    expose_docs: bool = False

    @field_validator("env")
    @classmethod
    def _known_env(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("production", "development", "test"):
            raise ValueError(f"TSM_ENV non riconosciuto: {v!r} "
                             "(production | development | test)")
        return v

    @model_validator(mode="after")
    def _refuse_insecure_cookies_in_production(self) -> "Settings":
        """In produzione un cookie di sessione non `Secure` fa FALLIRE L'AVVIO.

        Non è un avviso nei log, che nessuno legge fino al giorno dopo. Un cookie
        di sessione senza `Secure` viaggia in chiaro: chiunque sia sul percorso
        può prenderlo e diventare quell'utente. Meglio un servizio che non parte
        di uno che parte in modo insicuro, perché il primo si nota subito.

        Per provare in locale su HTTP c'è `compose.dev.yaml`, che dichiara
        `TSM_ENV=development` — cioè la deroga è esplicita e sta in un file che
        non si distribuisce.
        """
        if self.env == "production" and not self.cookie_secure:
            raise RuntimeError(
                "TSM_COOKIE_SECURE=false con TSM_ENV=production: rifiutato. "
                "Un cookie di sessione senza Secure viaggia in chiaro. "
                "Per lo sviluppo in HTTP usare `-f compose.dev.yaml`, che imposta "
                "TSM_ENV=development."
            )
        return self

    def allowed_origins(self) -> tuple[str, ...]:
        return tuple(o.strip().rstrip("/") for o in self.public_origin.split(",")
                     if o.strip())

    def trusted_proxy_list(self) -> tuple[str, ...]:
        return tuple(p.strip() for p in self.trusted_proxies.split(",") if p.strip())

    def db_password(self) -> str:
        """Legge il secret a ogni chiamata: la rotazione non richiede un rebuild."""
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
        if self.db_url:
            return self.db_url
        return (
            f"postgresql+psycopg://{self.db_user}:{self.db_password()}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
