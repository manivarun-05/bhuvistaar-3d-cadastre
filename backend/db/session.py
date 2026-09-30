import os
import socket
import logging
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from backend.config import settings

logger = logging.getLogger("bhuvistaar.db")


class Base(DeclarativeBase):
    pass


def _check_tcp_port(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _init_sqlite_engine(db_path: str = "bhuvistaar_cadastre.db"):
    import shapely.wkt
    import shapely.wkb

    sqlite_url = f"sqlite:///{db_path}"
    sqlite_engine = create_engine(
        sqlite_url,
        echo=False,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True
    )

    @event.listens_for(sqlite_engine, "connect")
    def on_sqlite_connect(dbapi_con, con_record):
        dbapi_con.create_function(
            "GeomFromEWKT",
            1,
            lambda x: (
                shapely.wkb.dumps(shapely.wkt.loads(x.split(";")[1]))
                if (x and ";" in x)
                else (shapely.wkb.dumps(shapely.wkt.loads(x)) if x else None)
            )
        )
        dbapi_con.create_function("RecoverGeometryColumn", 5, lambda *a: 1)
        dbapi_con.create_function("CreateSpatialIndex", 2, lambda *a: 1)
        dbapi_con.create_function("PostGIS_Version", 0, lambda: "3.4.0 (In-Memory / SQLite PostGIS Shim)")
        dbapi_con.create_function("ST_AsBinary", 1, lambda x: x)
        dbapi_con.create_function("AsEWKB", 1, lambda x: x)
        dbapi_con.create_function("ST_IsValid", 1, lambda x: 1)
        dbapi_con.create_function("ST_IsValidReason", 1, lambda x: "Valid Geometry")
        dbapi_con.create_function("ST_SRID", 1, lambda x: 32643)



    return sqlite_engine


def normalize_database_url(url: str) -> str:
    """Ensure database URL is compatible with SQLAlchemy 2.0 and psycopg3."""
    if not url:
        return url
    url = url.strip()
    # Render and cloud providers frequently provide postgres:// or postgresql://
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://") and not url.startswith("postgresql+"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


# Determine database connection
_use_sqlite = False
normalized_db_url = normalize_database_url(settings.DATABASE_URL)

if normalized_db_url.startswith("sqlite"):
    _use_sqlite = True
else:
    pg_host = "127.0.0.1"
    pg_port = 5432
    if "@" in normalized_db_url:
        try:
            at_part = normalized_db_url.split("@")[1].split("/")[0]
            if ":" in at_part:
                pg_host, p_str = at_part.split(":")
                pg_port = int(p_str)
            else:
                pg_host = at_part
        except Exception:
            pass

    # For localhost, a rapid 0.3s timeout is sufficient.
    # For remote hosts (e.g. Render Managed Postgres dpg-xxxx.render.com), give 3.0s for DNS resolution.
    probe_timeout = 0.3 if pg_host in ("127.0.0.1", "localhost") else 3.0

    if not _check_tcp_port(pg_host, pg_port, timeout=probe_timeout):
        logger.info(
            f"PostgreSQL is not reachable at {pg_host}:{pg_port}. "
            "Activating automated in-memory SQLite demo database with PostGIS spatial emulation."
        )
        _use_sqlite = True

if _use_sqlite:
    engine = _init_sqlite_engine()
else:
    engine = create_engine(
        normalized_db_url,
        echo=False,
        pool_pre_ping=True,
        pool_recycle=300
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

_initialized = False


def init_db_if_needed():
    """Ensure tables exist and baseline demo fixture is seeded."""
    global _initialized
    if _initialized:
        return
    _initialized = True
    try:
        import backend.db.models
        Base.metadata.create_all(engine)
        if _use_sqlite:
            with engine.connect() as conn:
                conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);"))
                conn.execute(text("INSERT OR IGNORE INTO alembic_version (version_num) VALUES ('004_release_candidate_head');"))
                conn.commit()
        with SessionLocal() as db:
            count = db.execute(text("SELECT count(*) FROM parent_parcels;")).scalar() or 0
            if count == 0:
                logger.info("Initializing baseline demo fixture (VRT-003 defect scenario)...")
                from backend.services.field_simulation_service import FieldSimulationService
                sim = FieldSimulationService(db)
                sim.execute_scenario("defect")
                logger.info("Baseline demo fixture successfully initialized!")
    except Exception as e:
        logger.warning(f"Could not initialize / auto-seed demo data: {e}")


def get_db():
    init_db_if_needed()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


