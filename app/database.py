from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import database_url
from app.models import Base

logger = logging.getLogger(__name__)
engine = create_engine(database_url(), pool_pre_ping=True, pool_size=5, max_overflow=10)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def initialize_database(attempts: int = 30) -> None:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            Base.metadata.create_all(engine)
            return
        except Exception as exc:
            last_error = exc
            logger.warning("Database is not ready; retrying", extra={"step": attempt + 1})
            time.sleep(min(0.5 + attempt * 0.25, 4))
    if last_error:
        raise last_error


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
