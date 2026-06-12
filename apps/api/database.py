import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

# Import settings - handle both package and direct execution
try:
    from .config import settings
except ImportError:
    from config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_recycle=300,
    # Explicit pool sizing. Defaults (5 + 10 overflow) are tight against a
    # 4-worker API plus Celery sharing postgres max_connections=100, and an
    # N+1 elsewhere can saturate the pool. 10 + 20 overflow per process gives
    # headroom without blowing the connection ceiling.
    pool_size=10,
    max_overflow=20,
    # TCP keepalives keep the connection alive during long idle periods
    # (e.g., while a Celery task is running ffmpeg for minutes). Without
    # these, Neon will quietly drop the connection mid-transaction.
    connect_args={
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    },
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
