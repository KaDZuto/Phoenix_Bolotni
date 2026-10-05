"""initial schema: devices, chunks, jobs, detections, events

Задача 4 (td3.md, раздел 4) — «хранение только статистики, без аудио».

Что здесь важно и не выражается в моделях:

* `detections.geom` — PostGIS `geometry(Point, 4326)`. Источник правды остаётся
  `lat`/`lon` (числа, работают и на SQLite); `geom` заполняется триггером, чтобы
  радиусные выборки шли через индекс, а не через полный скан.
* GiST-индекс по `geom` создаётся только на Postgres: на SQLite геометрия лежит
  текстом и аналитика считается в Python (`server/geo.py`, `server/zones.py`).
* Аудио в схеме нет вообще — ни колонок, ни таблиц. Байты чанка живут в оперативной
  очереди (`server/spool.py`) и удаляются сразу после инференса.

Revision ID: fc4d9f6f407a
Revises:
Create Date: 2026-10-05
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.types import UserDefinedType

revision: str = "fc4d9f6f407a"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


class GeometryPoint(UserDefinedType):
    """
    PostGIS-точка `geometry(Point, 4326)`.

    Тип объявлен здесь, а не импортирован из `server.models`: миграция должна
    оставаться исполнимой, даже если определение типа в моделях изменится.
    """

    cache_ok = True

    def get_col_spec(self, **kw) -> str:  # noqa: D102 - SQLAlchemy API
        return "geometry(Point, 4326)"


GEOMETRY_TYPE = GeometryPoint().with_variant(sa.Text(), "sqlite")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _create_gist_index() -> None:
    """Геопространственный индекс: без него радиусный запрос читает всю таблицу."""
    if not _is_postgres():
        return
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_detections_geom_gist ON detections USING gist (geom)"
    )


def _drop_gist_index() -> None:
    if not _is_postgres():
        return
    op.execute("DROP INDEX IF EXISTS ix_detections_geom_gist")


def _create_geom_trigger() -> None:
    """
    Держать `geom` в синхроне с `lat`/`lon` на уровне БД.

    ORM-хук (`models._fill_detection_geom`) делает то же самое, но триггер ловит и
    правки, сделанные в обход приложения: psql, скрипт анализа, `COPY`. Расхождение
    между `lat/lon` и `geom` — это тихая поломка радиусных выборок, поэтому лучше
    перестраховаться.
    """
    if not _is_postgres():
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION phoenix_sync_detection_geom() RETURNS trigger AS $$
        BEGIN
            IF NEW.lat IS NULL OR NEW.lon IS NULL THEN
                NEW.geom := NULL;
            ELSE
                NEW.geom := ST_SetSRID(ST_MakePoint(NEW.lon, NEW.lat), 4326);
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_detections_sync_geom
        BEFORE INSERT OR UPDATE OF lat, lon ON detections
        FOR EACH ROW EXECUTE FUNCTION phoenix_sync_detection_geom()
        """
    )


def _drop_geom_trigger() -> None:
    if not _is_postgres():
        return
    op.execute("DROP TRIGGER IF EXISTS trg_detections_sync_geom ON detections")
    op.execute("DROP FUNCTION IF EXISTS phoenix_sync_detection_geom()")


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("owner", sa.String(length=120), nullable=False),
        sa.Column("contact", sa.String(length=200), nullable=True),
        sa.Column("device_type", sa.String(length=16), nullable=False),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("altitude_m", sa.Float(), nullable=True),
        sa.Column("place_note", sa.String(length=300), nullable=True),
        # Только SHA-256-хеш токена: украденная копия БД не даёт возможности
        # заливать данные от имени чужого микрофона.
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=12), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("expected_chunk_sec", sa.Float(), nullable=True),
        sa.Column("last_seq", sa.Integer(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_chunk_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_upload_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_remote_addr", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )

    op.create_table(
        "chunks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("device_id", sa.String(length=32), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_sec", sa.Float(), nullable=False),
        sa.Column("overlap_with_prev_sec", sa.Float(), nullable=False),
        sa.Column("gap_before_sec", sa.Float(), nullable=False),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("audio_format", sa.String(length=16), nullable=False),
        sa.Column("sample_rate", sa.Integer(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("windows_total", sa.Integer(), nullable=False),
        sa.Column("windows_stored", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Регистрация чанка идемпотентна: повторная отправка того же `seq` отклоняется.
        sa.UniqueConstraint("device_id", "seq", name="uq_chunks_device_seq"),
    )
    op.create_index("ix_chunks_device_started", "chunks", ["device_id", "started_at"])
    op.create_index("ix_chunks_status", "chunks", ["status"])

    op.create_table(
        "device_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("device_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_sec", sa.Float(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("acknowledged", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_device_events_device_created", "device_events", ["device_id", "created_at"]
    )

    op.create_table(
        "detections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("device_id", sa.String(length=32), nullable=False),
        sa.Column("chunk_id", sa.Integer(), nullable=True),
        sa.Column("species_slug", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("mean_confidence", sa.Float(), nullable=True),
        sa.Column("votes", sa.Integer(), nullable=True),
        sa.Column("windows_evaluated", sa.Integer(), nullable=True),
        sa.Column("window_start_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("geom", GEOMETRY_TYPE, nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=True),
        sa.Column("duplicate_of_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["duplicate_of_id"], ["detections.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_detections_device_species", "detections", ["device_id", "species_slug"])
    op.create_index("ix_detections_species_time", "detections", ["species_slug", "window_start_ts"])
    op.create_index("ix_detections_time", "detections", ["window_start_ts"])
    op.create_index("ix_detections_lat_lon", "detections", ["lat", "lon"])
    op.create_index("ix_detections_chunk", "detections", ["chunk_id"])
    _create_gist_index()
    _create_geom_trigger()

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("chunk_id", sa.Integer(), nullable=True),
        sa.Column("device_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detections_count", sa.Integer(), nullable=False),
        sa.Column("duplicates_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_status_created", "jobs", ["status", "created_at"])

    # Отладочные per-window предсказания: только числа, без аудио. Заполняется лишь
    # при PHOENIX_STORE_RAW_WINDOWS=true, в проде выключено (см. td3.md, Задача 4).
    op.create_table(
        "raw_windows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chunk_id", sa.Integer(), nullable=True),
        sa.Column("device_id", sa.String(length=32), nullable=False),
        sa.Column("window_index", sa.Integer(), nullable=False),
        sa.Column("window_start_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("top_species", sa.String(length=64), nullable=True),
        sa.Column("top_probability", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("top_k_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_raw_windows_chunk", "raw_windows", ["chunk_id", "window_index"])


def downgrade() -> None:
    op.drop_index("ix_raw_windows_chunk", table_name="raw_windows")
    op.drop_table("raw_windows")

    op.drop_index("ix_jobs_status_created", table_name="jobs")
    op.drop_table("jobs")

    _drop_geom_trigger()
    _drop_gist_index()
    op.drop_index("ix_detections_chunk", table_name="detections")
    op.drop_index("ix_detections_lat_lon", table_name="detections")
    op.drop_index("ix_detections_time", table_name="detections")
    op.drop_index("ix_detections_species_time", table_name="detections")
    op.drop_index("ix_detections_device_species", table_name="detections")
    op.drop_table("detections")

    op.drop_index("ix_device_events_device_created", table_name="device_events")
    op.drop_table("device_events")

    op.drop_index("ix_chunks_status", table_name="chunks")
    op.drop_index("ix_chunks_device_started", table_name="chunks")
    op.drop_table("chunks")

    op.drop_table("devices")
