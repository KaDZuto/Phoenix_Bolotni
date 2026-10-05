"""
Конфигурация Alembic для серверного распознавателя (Задача 4, td3.md раздел 4).

Схема в проде создаётся и меняется **только** миграциями (`alembic upgrade head`),
а не `Base.metadata.create_all`: PostGIS-геометрия, GiST-индексы и партиционирование
`detections` по времени должны быть под контролем версий.

URL берётся из того же `PHOENIX_DATABASE_URL`, что и у приложения, поэтому
`alembic upgrade head` и `docker compose up` не могут разойтись.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Импорт моделей обязателен: alembic autogenerate видит метаданные только после того,
# как все таблицы описаны.
SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parent
for path in (str(REPO_ROOT), str(SERVER_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from server.config import get_settings  # noqa: E402
from server.models import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def include_object(object_, name, type_, reflected, compare_to) -> bool:
    """
    Исключить из автогенерации то, что не является нашей схемой.

    В частности, таблицы PostGIS-системы (`spatial_ref_sys`, `geography_columns`,
    `geometry_columns`) alembic видит, но они созданы расширением и трогать их нельзя.
    """
    schema = getattr(object_, "schema", None)
    if type_ == "table" and schema in ("topology", "tiger", "public"):
        if name in {"spatial_ref_sys", "geometry_columns", "geography_columns", "raster_columns", "raster_overviews"}:
            return False
    return True


def run_migrations_offline() -> None:
    """Миграции без подключения (`alembic upgrade head --sql`) — для планирования заранее."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Миграции с подключением к БД."""
    connectable = config.attributes.get("connection", None)
    if connectable is None:
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )

    with connectable.connect() as connection:
        if settings.is_postgres:
            # Расширение PostGIS ставится до первой миграции: `detections.geom`
            # не создастся без него.
            connection.execute(
                __import__("sqlalchemy").text("CREATE EXTENSION IF NOT EXISTS postgis")
            )
            connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()