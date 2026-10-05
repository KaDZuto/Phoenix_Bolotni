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
    Привести схему БД в рабочее состояние при старте процесса.

    Развилка по диалекту принципиальна:

    * **Postgres (прод)** — только `alembic upgrade head`. `create_all` здесь недопустим:
      он создал бы таблицы вне версионирования, и следующая же миграция упала бы с
      «relation already exists», а PostGIS-геометрия, GiST-индекс и триггер
      синхронизации `lat/lon` → `geom` остались бы не созданными. Плюс `alembic_version`
      не появился бы, и Alembic решил бы, что база пустая.
    * **SQLite (разработка и тесты)** — `create_all`: миграции там гонять каждый раз
      неудобно, а проверка их актуальности живёт в `test_migrations.py`.

    Конкуренция (`api` и `worker` поднимаются одновременно) снимается advisory-блокировкой
    Postgres: без неё два процесса могли бы одновременно начать одну и ту же миграцию.
    """
    engine = get_engine()
    if engine.dialect.name == "postgresql":
        _migrate_postgres(engine)
        return

    Base.metadata.create_all(engine)
    logger.info("Схема БД готова (%s)", engine.dialect.name)


def _migrate_postgres(engine: Engine) -> None:  # pragma: no cover - нужен сервер
    """Накатить миграции под advisory-локом, защищая от гонки между процессами."""
    from alembic import command
    from alembic.config import Config

    from .config import SERVER_DIR

    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        conn.commit()
        # pg_advisory_lock блокирует на уровне сессии: держим соединение открытым
        # ровно на время миграции.
        conn.execute(text("SELECT pg_advisory_lock(hashtext('phoenix_schema_migrations'))"))
        conn.commit()

    try:
        config = Config(str(SERVER_DIR / "alembic.ini"))
        command.upgrade(config, "head")
    finally:
        with engine.connect() as conn:
            conn.execute(
                text("SELECT pg_advisory_unlock(hashtext('phoenix_schema_migrations'))")
            )
            conn.commit()

    logger.info("Схема БД актуальна (postgres, alembic head)")


def reset_engine() -> None:
    """Сбросить движок и фабрику сессий (используется в тестах)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None