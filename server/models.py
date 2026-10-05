"""
Схема БД серверного распознавателя (Задачи 2–4).

Хранится **только статистика**: подтверждённые детекции, метаданные чанков, устройства
и события разрывов связи. Аудио на сервере не остаётся (см. td3.md, Задача 4).

Координаты хранятся в двух видах:
* `lat`/`lon` — переносимые числа (единый источник правды, работают и на SQLite);
* `geom` — PostGIS `geometry(Point, 4326)` с GiST-индексом (создаётся в Postgres,
  заполняется автоматически на вставке, см. `_fill_detection_geom`).

Партиционирование `detections` по месяцам (или TimescaleDB) — P2-оптимизация,
зафиксирована в `server/README.md`.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import UserDefinedType

DEVICE_TYPE_STATIONARY = "stationary"
DEVICE_TYPE_MOBILE = "mobile"
DEVICE_TYPES = (DEVICE_TYPE_STATIONARY, DEVICE_TYPE_MOBILE)

JOB_QUEUED = "queued"
JOB_PROCESSING = "processing"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_EXPIRED = "expired"

EVENT_REGISTERED = "registered"
EVENT_GAP = "gap"
EVENT_LATE_CHUNK = "late_chunk"
EVENT_REACTIVATED = "reactivated"
EVENT_DEACTIVATED = "deactivated"
EVENT_ERROR = "error"


def utcnow() -> datetime:
    """Текущее время в UTC (aware)."""
    return datetime.now(timezone.utc)


def ensure_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Привести дату к aware-UTC (SQLite не хранит таймзону, Postgres — хранит)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class GeometryPoint(UserDefinedType):
    """PostGIS-точка `geometry(Point, 4326)` (на других диалектах — обычный TEXT)."""

    cache_ok = True

    def get_col_spec(self, **kw) -> str:  # noqa: D102 - SQLAlchemy API
        return "geometry(Point, 4326)"


#: На SQLite геометрия хранится как TEXT (аналитика считается в Python, см. `geo.py`).
GEOMETRY_TYPE = GeometryPoint().with_variant(Text(), "sqlite")


class Device(Base):
    """Реестр микрофонов/устройств (Задача 2)."""

    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    owner: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    contact: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    device_type: Mapped[str] = mapped_column(String(16), nullable=False, default=DEVICE_TYPE_STATIONARY)
    lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lon: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    altitude_m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    place_note: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)

    # API-токен: в БД лежит только SHA-256-хеш, сам токен показывается один раз при регистрации.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    token_prefix: Mapped[str] = mapped_column(String(12), nullable=False, default="")

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expected_chunk_sec: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_seq: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_chunk_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_upload_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_remote_addr: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    detections: Mapped[list["Detection"]] = relationship(back_populates="device", cascade="all, delete-orphan")
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="device", cascade="all, delete-orphan")

    # -- хелперы ---------------------------------------------------------
    @staticmethod
    def new_id() -> str:
        return "dev_" + secrets.token_hex(6)

    @property
    def has_fixed_location(self) -> bool:
        return self.device_type == DEVICE_TYPE_STATIONARY and self.lat is not None and self.lon is not None

    def location_for_detection(self, lat: Optional[float], lon: Optional[float]) -> tuple[Optional[float], Optional[float]]:
        """Координаты детекции: для мобильных — из чанка, для стационарных — фиксированные."""
        if self.device_type == DEVICE_TYPE_MOBILE:
            return (lat, lon) if lat is not None and lon is not None else (None, None)
        return self.lat, self.lon

    def to_dict(self, include_status: bool = True) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "owner": self.owner,
            "contact": self.contact,
            "device_type": self.device_type,
            "lat": self.lat,
            "lon": self.lon,
            "altitude_m": self.altitude_m,
            "place_note": self.place_note,
            "is_active": self.is_active,
            "token_prefix": self.token_prefix,
            "expected_chunk_sec": self.expected_chunk_sec,
            "last_seq": self.last_seq,
            "note": self.note,
            "created_at": ensure_utc(self.created_at).isoformat() if self.created_at else None,
            "updated_at": ensure_utc(self.updated_at).isoformat() if self.updated_at else None,
        }
        if include_status:
            data.update(
                {
                    "last_seen_at": ensure_utc(self.last_seen_at).isoformat() if self.last_seen_at else None,
                    "last_upload_at": (
                        ensure_utc(self.last_upload_at).isoformat() if self.last_upload_at else None
                    ),
                    "last_chunk_started_at": (
                        ensure_utc(self.last_chunk_started_at).isoformat()
                        if self.last_chunk_started_at
                        else None
                    ),
                    "last_remote_addr": self.last_remote_addr,
                }
            )
        return data


class Chunk(Base):
    """Метаданные принятого чанка непрерывного потока (аудио не хранится — только числа)."""

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("device_id", "seq", name="uq_chunks_device_seq"),
        Index("ix_chunks_device_started", "device_id", "started_at"),
        Index("ix_chunks_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_sec: Mapped[float] = mapped_column(Float, nullable=False)
    overlap_with_prev_sec: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    gap_before_sec: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lon: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    audio_format: Mapped[str] = mapped_column(String(16), nullable=False, default="wav")
    sample_rate: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=JOB_QUEUED)
    windows_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    windows_stored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    device: Mapped[Device] = relationship(back_populates="chunks")

    @property
    def interval(self) -> tuple[float, float]:
        """Интервал чанка в Unix-секундах (начало, конец) — для дедупликации."""
        return ensure_utc(self.started_at).timestamp(), ensure_utc(self.ended_at).timestamp()

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.id,
            "device_id": self.device_id,
            "seq": self.seq,
            "started_at": ensure_utc(self.started_at).isoformat(),
            "ended_at": ensure_utc(self.ended_at).isoformat(),
            "duration_sec": round(self.duration_sec, 3),
            "overlap_with_prev_sec": round(self.overlap_with_prev_sec, 3),
            "gap_before_sec": round(self.gap_before_sec, 3),
            "lat": self.lat,
            "lon": self.lon,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "audio_format": self.audio_format,
            "sample_rate": self.sample_rate,
            "received_at": ensure_utc(self.received_at).isoformat() if self.received_at else None,
            "processed_at": (
                ensure_utc(self.processed_at).isoformat() if self.processed_at else None
            ),
            "status": self.status,
            "windows_total": self.windows_total,
            "error": self.error,
        }


class Job(Base):
    """Очередь обработки чанков (метаданные; сам звук живёт только в оперативной памяти)."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_created", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    chunk_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE"), nullable=True
    )
    device_id: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=JOB_QUEUED)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worker_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    detections_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicates_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    @staticmethod
    def new_id() -> str:
        return "job_" + secrets.token_hex(8)

    def to_dict(self) -> dict:
        return {
            "job_id": self.id,
            "chunk_id": self.chunk_id,
            "device_id": self.device_id,
            "status": self.status,
            "attempts": self.attempts,
            "worker_id": self.worker_id,
            "error": self.error,
            "detections_count": self.detections_count,
            "duplicates_count": self.duplicates_count,
            "created_at": ensure_utc(self.created_at).isoformat() if self.created_at else None,
            "started_at": ensure_utc(self.started_at).isoformat() if self.started_at else None,
            "finished_at": ensure_utc(self.finished_at).isoformat() if self.finished_at else None,
        }


class Detection(Base):
    """
    Подтверждённая детекция вида — единственное, что попадает в БД и на карту.

    Это автоматическое предсказание модели (`source='model_auto'`), а не экспертная
    разметка: такие записи не должны автоматически использоваться для дообучения.
    """

    __tablename__ = "detections"
    __table_args__ = (
        Index("ix_detections_device_species", "device_id", "species_slug"),
        Index("ix_detections_species_time", "species_slug", "window_start_ts"),
        Index("ix_detections_time", "window_start_ts"),
        Index("ix_detections_lat_lon", "lat", "lon"),
        Index("ix_detections_chunk", "chunk_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    chunk_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chunks.id", ondelete="SET NULL"), nullable=True
    )
    species_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    mean_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    votes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    windows_evaluated: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    window_start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lon: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    geom: Mapped[Optional[object]] = mapped_column(GEOMETRY_TYPE, nullable=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="model_auto")
    model_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    duplicate_of_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("detections.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    device: Mapped[Device] = relationship(back_populates="detections")

    @property
    def interval(self) -> tuple[float, float]:
        return (
            ensure_utc(self.window_start_ts).timestamp(),
            ensure_utc(self.window_end_ts).timestamp(),
        )

    def to_dict(self, species_name_ru: Optional[str] = None) -> dict:
        return {
            "id": self.id,
            "device_id": self.device_id,
            "chunk_id": self.chunk_id,
            "species_slug": self.species_slug,
            "species_ru": species_name_ru,
            "confidence": round(self.confidence, 6),
            "mean_confidence": round(self.mean_confidence, 6) if self.mean_confidence is not None else None,
            "votes": self.votes,
            "windows_evaluated": self.windows_evaluated,
            "window_start_ts": ensure_utc(self.window_start_ts).isoformat(),
            "window_end_ts": ensure_utc(self.window_end_ts).isoformat(),
            "lat": self.lat,
            "lon": self.lon,
            "source": self.source,
            "model_version": self.model_version,
            "duplicate_of_id": self.duplicate_of_id,
            "created_at": ensure_utc(self.created_at).isoformat() if self.created_at else None,
        }


class RawWindow(Base):
    """
    Отладочные per-window предсказания (только числа, без аудио).

    Заполняется только при `PHOENIX_STORE_RAW_WINDOWS=true` — в проде по умолчанию выключено.
    """

    __tablename__ = "raw_windows"
    __table_args__ = (Index("ix_raw_windows_chunk", "chunk_id", "window_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chunk_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE"), nullable=True
    )
    device_id: Mapped[str] = mapped_column(String(32), nullable=False)
    window_index: Mapped[int] = mapped_column(Integer, nullable=False)
    window_start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    top_species: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    top_probability: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown/uncertain")
    top_k_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class DeviceEvent(Base):
    """События по устройствам: разрывы связи, опоздавшие чанки, деактивация, ошибки."""

    __tablename__ = "device_events"
    __table_args__ = (Index("ix_device_events_device_created", "device_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_sec: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "device_id": self.device_id,
            "kind": self.kind,
            "started_at": ensure_utc(self.started_at).isoformat(),
            "ended_at": ensure_utc(self.ended_at).isoformat() if self.ended_at else None,
            "duration_sec": round(self.duration_sec, 1) if self.duration_sec is not None else None,
            "detail": self.detail,
            "acknowledged": self.acknowledged,
            "created_at": ensure_utc(self.created_at).isoformat() if self.created_at else None,
        }


def hash_token(token: str) -> str:
    """SHA-256 токена устройства (в БД хранится только хеш)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_prefix(token: str) -> str:
    """Короткий префикс токена для отображения в списке устройств (сам токен не хранится)."""
    return token[:12]


def new_device_token() -> str:
    return "pbk_" + secrets.token_urlsafe(32)


@event.listens_for(Detection, "before_insert")
def _fill_detection_geom(mapper, connection, target: Detection) -> None:
    """
    Заполнить PostGIS-геометрию из lat/lon (только для Postgres).

    На SQLite колонка `geom` остаётся пустой: аналитика и bbox-фильтры считаются в Python
    (`geo.py`, `zones.py`), поэтому одинаковый код работает в dev/тестах и в проде.
    """
    if connection.dialect.name != "postgresql":
        return
    if target.lat is None or target.lon is None:
        return
    target.geom = func.ST_SetSRID(func.ST_MakePoint(target.lon, target.lat), 4326)


@event.listens_for(Detection, "before_update")
def _update_detection_geom(mapper, connection, target: Detection) -> None:
    if connection.dialect.name != "postgresql":
        return
    if target.lat is None or target.lon is None:
        return
    target.geom = func.ST_SetSRID(func.ST_MakePoint(target.lon, target.lat), 4326)