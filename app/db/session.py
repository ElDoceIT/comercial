from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


settings = get_settings()

if not settings.database_url:
    engine = None
    SessionLocal = None
else:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    if SessionLocal is None:
        raise RuntimeError("Falta configurar DATABASE_URL para conectar la base de datos.")

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
