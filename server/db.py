"""
Подключение к БД и фабрика сессий.

Продакшен: PostgreSQL + PostGIS (`PHOENIX_DATABASE_URL=postgresql+psycopg://...`),
схема создаётся миграциями alembic (`server/alembic/`).
Локальная разработка и тесты: SQLite-файл без PostGIS (`server/data/phoenix.db`).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

logger = logging.getLogger(__name__)

_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None


def _create_engine() -> Engine:
    settings = get_settings()
    url = settings.database_url
    kwargs: dict = {"echo": settings.database_echo, "future": True}
    if url.startswith("sqlite"):
        path = url.split("sqlite:///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_pre_ping"] = True
    return create_engine(url, **kwargs)


def get_engine() -> Engine:
    """Ленивая инициализация движка (один на процесс)."""
    global _engine
    if _engine is None:
        _engine = _create_engine()
        if _engine.dialect.name == "sqlite":
            _enable_sqlite_foreign_keys(_engine)
        logger.info("БД инициализирована: %s", get_settings().database_url.split("@")[-1])
    return _engine


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):  # pragma: no cover - тривиально
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_session_factory() -> sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
        )
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Транзакция с автокоммитом: `with session_scope() as s: ...`."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI-зависимость: сессия с коммитом при успешном завершении запроса."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """
    Создать схему из моделей.

    Используется только для локальной разработки и тестов (SQLite). В продакшене схема
    создаётся и обновляется только через `alembic upgrade head` (см. `server/alembic/`),
    чтобы PostGIS-геометрия, GiST-индексы и партиционирование контролировались миграциями.
    """
    engine = get_engine()
    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        _ensure_postgis(engine)
    logger.info("Схема БД готова (%s)", engine.dialect.name)


def _ensure_postgis(engine: Engine) -> None:  # pragma: no cover - только для Postgres
    """Включить расширение PostGIS, если его ещё нет (идемпотентно)."""
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        # Геосинхронизацию обеспечивает хук `models._fill_detection_geom`,
        # здесь только GiST-индекс для радиусных выборок.
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_detections_geom_gist ON detections USING gist (geom)")
        )


def reset_engine() -> None:
    """Сбросить движок и фабрику сессий (используется в тестах)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None