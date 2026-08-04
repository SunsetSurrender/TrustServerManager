"""Engine SQLAlchemy. Nessun modello: lo scheletro non persiste ancora niente."""
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.config import get_settings

_engine: Engine | None = None


def get_engine() -> Engine:
    """Engine creato al primo uso, non all'import.

    Se lo si creasse all'import, l'API non partirebbe quando il DB è momentaneamente
    giù — e allora /api/health non potrebbe più distinguere "processo vivo" da
    "dipendenze a posto", che è tutto il punto di avere due endpoint separati.
    """
    global _engine
    if _engine is None:
        s = get_settings()
        _engine = create_engine(
            s.sqlalchemy_url(),
            pool_pre_ping=True,
            connect_args={"connect_timeout": s.db_connect_timeout},
        )
    return _engine


def check_database() -> None:
    """Solleva se il DB non è raggiungibile. Usata da /api/ready."""
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
