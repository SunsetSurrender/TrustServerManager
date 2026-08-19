"""Engine SQLAlchemy. Due, e il secondo esiste per impedire uno STALLO.

    get_engine()        la connessione della richiesta: legge e scrive
    get_read_engine()   lo snapshot di lettura dell'inventario (§8.45)

Perché due pool e non uno, spiegato una volta sola qui perché è aritmetica e non
gusto. Dalla fase 2D un `GET /api/inventory` tiene DUE connessioni insieme: quella
della richiesta (che l'autenticazione ha già usato — e in cui **scrive**
`sessions.last_seen_at`) e quella dello snapshot in `REPEATABLE READ, READ ONLY`.

Con un pool solo questo è il classico stallo da acquisizione a due fasi. Con la
capienza predefinita di SQLAlchemy (`pool_size=5` + `max_overflow=10` = 15) e il
threadpool di Starlette per gli endpoint sincroni (40 lavoratori), **quindici `GET`
simultanei** prendono tutte e quindici le connessioni per autenticarsi e poi
aspettano tutti la seconda: non se ne libera nessuna, perché a liberarle dovrebbe
essere qualcuno che è già in attesa. Il risultato è trenta secondi di blocco e poi
un timeout del pool — sotto carico, e solo sotto carico.

Con due pool distinti l'attesa non può chiudersi in cerchio: i portatori della
prima connessione sono al massimo quanti la capienza del PRIMO pool, e il secondo ne
ha altrettante, quindi ognuno di loro la ottiene. Da qui l'invariante da non
rompere:

    capienza(pool di lettura) >= capienza(pool delle richieste)

C'è un test che la controlla. Rimpicciolire il pool di lettura «perché legge poco»
reintrodurrebbe lo stallo, e lo reintrodurrebbe in produzione sotto carico, dove non
si riproduce a mano.
"""
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.config import get_settings

_engine: Engine | None = None
_read_engine: Engine | None = None


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


def get_read_engine() -> Engine:
    """Engine dedicato allo snapshot di lettura dell'inventario. Vedi il modulo.

    Stesse credenziali e stessa capienza: non è una connessione «privilegiata
    diversamente», è un pool separato, e il pool separato è tutto il punto. La
    capienza NON va ridotta — vedi l'invariante in testa al modulo.
    """
    global _read_engine
    if _read_engine is None:
        s = get_settings()
        _read_engine = create_engine(
            s.sqlalchemy_url(),
            pool_pre_ping=True,
            connect_args={"connect_timeout": s.db_connect_timeout},
        )
    return _read_engine


def check_database() -> None:
    """Solleva se il DB non è raggiungibile. Usata da /api/ready."""
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
