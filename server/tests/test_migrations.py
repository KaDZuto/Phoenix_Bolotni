"""
Тесты миграций Alembic (Задача 4, td3.md раздел 4).

Проверяется ровно то, на что обычно спотыкается релиз: миграции не должны
разъезжаться с моделями. Поэтому тест сравнивает схему, созданную миграциями, со
схемой из `Base.metadata.create_all` и требует нулевой разницы.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from server.models import Base

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"

EXPECTED_TABLES = {
    "alembic_version",
    "chunks",
    "detections",
    "device_events",
    "devices",
    "jobs",
    "raw_windows",
}


@pytest.fixture
def alembic_config(tmp_path):
    """Конфигурация Alembic поверх временной SQLite-БД."""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'migrated.db'}")
    # env.py перезаписывает URL на PHOENIX_DATABASE_URL — выставляем его же.
    import os

    os.environ["PHOENIX_DATABASE_URL"] = config.get_main_option("sqlalchemy.url")
    from server import config as config_module
    from server import db as db_module

    config_module.reload_settings()
    db_module.reset_engine()
    yield config
    db_module.reset_engine()
    config_module.reload_settings()


def _render_sql(alembic_config, url: str) -> str:
    """
    Собрать DDL для указанного URL без подключения к БД.

    `env.py` намеренно перезаписывает `sqlalchemy.url` на `PHOENIX_DATABASE_URL`
    (чтобы миграции и приложение гарантированно смотрели в одну базу), поэтому
    менять нужно и конфиг, и окружение.
    """
    import io
    import os
    from contextlib import redirect_stdout

    alembic_config.set_main_option("sqlalchemy.url", url)
    os.environ["PHOENIX_DATABASE_URL"] = url
    from server import config as config_module

    config_module.reload_settings()

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        command.upgrade(alembic_config, "head", sql=True)
    return buffer.getvalue()


def _schema_snapshot(engine) -> dict:
    """
    Схема в виде, пригодном для сравнения: таблицы → колонки/индексы/FK.

    Из выдачи убираются две вещи, которые к делу не относятся:
    * служебная `alembic_version` — её создаёт сам alembic, а не миграции;
    * индексы без имени (`None`) — это автоиндексы SQLite на UNIQUE-колонках,
      они одинаковы у обеих схем и просто засоряют diff.
    """
    inspector = inspect(engine)
    snapshot: dict = {"tables": {}, "indexes": set(), "foreign_keys": set()}
    for table in sorted(set(inspector.get_table_names()) - {"alembic_version"}):
        columns = {
            col["name"]: {
                "type": str(col["type"]).lower(),
                "nullable": col["nullable"],
            }
            for col in inspector.get_columns(table)
        }
        indexes = {idx["name"] for idx in inspector.get_indexes(table) if idx["name"]}
        uniques = {uq["name"] for uq in inspector.get_unique_constraints(table) if uq["name"]}
        foreign_keys = {
            (table, tuple(fk["constrained_columns"]), fk["referred_table"])
            for fk in inspector.get_foreign_keys(table)
        }
        snapshot["tables"][table] = {
            "columns": columns,
            "indexes": indexes | uniques,
        }
        snapshot["indexes"] |= indexes | uniques
        snapshot["foreign_keys"] |= foreign_keys
    return snapshot


def test_upgrade_creates_full_schema(alembic_config):
    command.upgrade(alembic_config, "head")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    tables = set(inspect(engine).get_table_names())
    assert tables == EXPECTED_TABLES
    assert alembic_config.get_main_option("sqlalchemy.url")


def test_upgrade_downgrade_roundtrip_is_clean(alembic_config):
    """`downgrade base` должен убирать всё, кроме служебного `alembic_version`."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}


def test_migrations_match_models(alembic_config, tmp_path):
    """
    Схема из миграций должна совпадать со схемой из моделей.

    Расхождение означает одно из двух: либо миграция забывает новое поле (и прод
    сломается на первом же insert), либо модель содержит поле, которого в БД нет
    (и падает SELECT). Оба варианта ловятся здесь, до деплоя.
    """
    command.upgrade(alembic_config, "head")
    url = alembic_config.get_main_option("sqlalchemy.url")

    migrated = create_engine(url)
    fresh = create_engine(f"sqlite:///{tmp_path / 'from_models.db'}")
    Base.metadata.create_all(fresh)

    assert _schema_snapshot(migrated) == _schema_snapshot(fresh)


def test_detections_has_geom_column_for_postgis(alembic_config):
    """
    `geom` обязана быть в схеме даже на SQLite (где это TEXT).

    Иначе миграция, «успешно» прошедшая на SQLite, уронит прод на Postgres.
    """
    command.upgrade(alembic_config, "head")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    columns = {col["name"] for col in inspect(engine).get_columns("detections")}
    assert {"lat", "lon", "geom"} <= columns


def test_detection_indexes_match_queries(alembic_config):
    """Индексы под реальные запросы карты, а не «на всякий случай»."""
    command.upgrade(alembic_config, "head")
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    indexes = {idx["name"] for idx in inspect(engine).get_indexes("detections")}
    # «все детекции вида за период» и «в прямоугольнике карты» — два главных запроса.
    assert "ix_detections_species_time" in indexes
    assert "ix_detections_lat_lon" in indexes
    assert "ix_detections_time" in indexes


def test_postgres_specific_sql_is_rendered(alembic_config, capsys):
    """
    Postgres-специфичные куски (GiST, триггер) не должны попадать в SQLite.

    `--sql` собирает DDL без подключения, поэтому проверка работает без сервера.
    """
    postgres_sql = _render_sql(
        alembic_config, "postgresql+psycopg://phoenix:secret@db:5432/phoenix"
    )
    assert "geometry(Point, 4326)" in postgres_sql
    assert "USING gist (geom)" in postgres_sql
    assert "ST_MakePoint(NEW.lon, NEW.lat)" in postgres_sql
    assert "CREATE TRIGGER trg_detections_sync_geom" in postgres_sql

    # plpgsql и gist не должны просочиться в SQLite-вариант.
    sqlite_sql = _render_sql(alembic_config, "sqlite:///:memory:")
    assert "CREATE TABLE detections" in sqlite_sql
    assert "USING gist" not in sqlite_sql
    assert "CREATE TRIGGER" not in sqlite_sql
    assert "geometry(Point, 4326)" not in sqlite_sql


def test_offline_migration_runs_without_database(alembic_config):
    """`alembic upgrade head --sql` — планирование релиза до окна обслуживания."""
    sql = _render_sql(alembic_config, "postgresql+psycopg://phoenix:secret@db:5432/phoenix")
    assert "CREATE TABLE detections" in sql
    assert "INSERT INTO alembic_version" in sql
