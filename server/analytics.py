"""
Слой запросов к таблице `detections` (общий для API карты и зон активности).

Все выборки идут по индексу: сначала дешёвый bbox-предикат по `lat/lon`
(`ix_detections_lat_lon`), затем точная фильтрация в Python (радиус, геохеш-ячейка).
На Postgres дополнительно доступен PostGIS-путь `ST_DWithin` по геометрии с GiST-индексом —
он включается автоматически, когда соединение — PostgreSQL (см. `db.py`).

Дубли на стыках чанков (`duplicate_of_id IS NOT NULL`) по умолчанию исключаются:
на карту попадают только канонические детекции.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .geo import bbox_around, haversine_m
from .models import Detection, ensure_utc

logger = logging.getLogger(__name__)

#: Предохранитель: больше этого числа строк аналитика не читает за раз.
MAX_SCAN_ROWS = 200_000


@dataclass
class DetectionFilter:
    """Фильтр выборки детекций для карты, экспорта и зон активности."""

    species: Optional[List[str]] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    bbox: Optional[Tuple[float, float, float, float]] = None  # (min_lat, min_lon, max_lat, max_lon)
    center: Optional[Tuple[float, float]] = None
    radius_m: Optional[float] = None
    device_ids: Optional[List[str]] = None
    min_confidence: Optional[float] = None
    include_duplicates: bool = False
    require_location: bool = True
    limit: int = 20_000

    def is_empty(self) -> bool:
        return all(
            v in (None, [], False)
            for v in (
                self.species,
                self.date_from,
                self.date_to,
                self.bbox,
                self.center,
                self.device_ids,
                self.min_confidence,
            )
        )


@dataclass
class DetectionPoint:
    """Плоская точка детекции для аналитики (без объектов SQLAlchemy)."""

    id: int
    species_slug: str
    confidence: float
    lat: float
    lon: float
    window_start_ts: datetime
    window_end_ts: datetime
    device_id: str

    @property
    def day(self) -> str:
        return ensure_utc(self.window_start_ts).strftime("%Y-%m-%d")

    @property
    def month(self) -> str:
        return ensure_utc(self.window_start_ts).strftime("%Y-%m")

    @property
    def hour(self) -> int:
        return ensure_utc(self.window_start_ts).hour


@dataclass
class DetectionQueryStats:
    """Диагностика выборки — попадает в ответ API (видно, по чему шёл запрос)."""

    matched: int
    scanned: int
    took_ms: float
    used_postgis: bool = False
    bbox_prefilter: Optional[Tuple[float, float, float, float]] = None
    notes: List[str] = field(default_factory=list)


@dataclass
class DetectionQueryResult:
    rows: List[Detection]
    points: List[DetectionPoint]
    stats: DetectionQueryStats


def supports_postgis(session: Session) -> bool:
    try:
        return session.get_bind().dialect.name == "postgresql"
    except Exception:  # pragma: no cover - защита
        return False


def _build_query(session: Session, flt: DetectionFilter):
    query = select(Detection)
    notes: List[str] = []
    prefilter: Optional[Tuple[float, float, float, float]] = None
    used_postgis = False

    if flt.species:
        query = query.where(Detection.species_slug.in_(flt.species))
    if flt.date_from is not None:
        query = query.where(Detection.window_start_ts >= _aware(flt.date_from))
    if flt.date_to is not None:
        query = query.where(Detection.window_start_ts <= _aware(flt.date_to))
    if flt.device_ids:
        query = query.where(Detection.device_id.in_(flt.device_ids))
    if flt.min_confidence is not None:
        query = query.where(Detection.confidence >= flt.min_confidence)
    if not flt.include_duplicates:
        query = query.where(Detection.duplicate_of_id.is_(None))
    if flt.require_location:
        # Для карты координаты обязательны (иначе точку некуда рисовать), но для
        # CSV-выгрузки запись «птица слышна, где именно — неизвестно» тоже ценна.
        query = query.where(Detection.lat.isnot(None), Detection.lon.isnot(None))

    if flt.bbox is not None:
        min_lat, min_lon, max_lat, max_lon = flt.bbox
        query = query.where(
            Detection.lat >= min_lat,
            Detection.lat <= max_lat,
            Detection.lon >= min_lon,
            Detection.lon <= max_lon,
        )
        prefilter = flt.bbox

    if flt.center is not None and flt.radius_m is not None:
        lat, lon = flt.center
        min_lat, min_lon, max_lat, max_lon = bbox_around(lat, lon, flt.radius_m)
        query = query.where(
            Detection.lat >= min_lat,
            Detection.lat <= max_lat,
            Detection.lon >= min_lon,
            Detection.lon <= max_lon,
        )
        prefilter = (min_lat, min_lon, max_lat, max_lon)
        if supports_postgis(session):
            # Точный радиус с использованием GiST-индекса по geometry(Point, 4326).
            query = query.where(
                func.ST_DWithin(
                    func.Geography(Detection.geom),
                    func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326),
                    flt.radius_m,
                )
            )
            used_postgis = True
            notes.append("радиус через PostGIS ST_DWithin")
        else:
            notes.append("точный радиус посчитан в Python по haversine (SQLite: ST_DWithin нет)")

    query = query.order_by(Detection.window_start_ts).limit(min(flt.limit, MAX_SCAN_ROWS))
    return query, DetectionQueryStats(0, 0, 0.0, used_postgis, prefilter, notes)


def query_detections(session: Session, flt: DetectionFilter) -> DetectionQueryResult:
    """Выбрать детекции по фильтру (bbox/радиус/период/вид) и посчитать статистику запроса."""
    import time

    started = time.perf_counter()
    query, stats = _build_query(session, flt)
    rows = list(session.execute(query).scalars())
    points = [
        DetectionPoint(
            id=row.id,
            species_slug=row.species_slug,
            confidence=row.confidence,
            lat=row.lat,
            lon=row.lon,
            window_start_ts=ensure_utc(row.window_start_ts),
            window_end_ts=ensure_utc(row.window_end_ts),
            device_id=row.device_id,
        )
        for row in rows
        if row.lat is not None and row.lon is not None
    ]

    if flt.center is not None and flt.radius_m is not None and not supports_postgis(session):
        # Без PostGIS точный радиус приходится считать в Python: SQL отсекает только
        # bbox, а haversine дочищает «углы» прямоугольника.
        lat, lon = flt.center
        points = [p for p in points if haversine_m(lat, lon, p.lat, p.lon) <= flt.radius_m]
    elif flt.radius_m is not None and flt.center is None:  # pragma: no cover - защита
        points = []

    stats.matched = len(points)
    stats.scanned = len(rows)
    stats.took_ms = round((time.perf_counter() - started) * 1000, 2)
    return DetectionQueryResult(rows=rows, points=points, stats=stats)


def count_detections(session: Session, flt: DetectionFilter) -> int:
    """Быстрый подсчёт детекций под фильтр (для легенды и сводок)."""
    query, _ = _build_query(session, flt)
    count_query = select(func.count()).select_from(query.subquery())
    return int(session.execute(count_query).scalar() or 0)


def species_summary(session: Session, flt: Optional[DetectionFilter] = None) -> List[dict]:
    """
    Сводка по видам: сколько детекций, когда последняя, средняя уверенность.

    По умолчанию считаются **все** детекции, включая те, у которых нет координат:
    для сводки «кого вообще слышали» координаты не нужны, а терять их из-за
    `lat IS NULL` значит занижать активность вида.
    """
    flt = flt or DetectionFilter(require_location=False)
    query, _ = _build_query(session, flt)
    sub = query.subquery()
    summary = (
        select(
            sub.c.species_slug,
            func.count(),
            func.max(sub.c.window_start_ts),
            func.avg(sub.c.confidence),
        )
        .group_by(sub.c.species_slug)
        .order_by(func.count().desc())
    )
    return [
        {
            "species_slug": species,
            "detections": int(count),
            "last_seen": ensure_utc(last).isoformat() if last else None,
            "mean_confidence": round(float(mean or 0.0), 4),
        }
        for species, count, last, mean in session.execute(summary)
    ]


def _aware(value: datetime) -> datetime:
    aware = ensure_utc(value)
    assert aware is not None
    return aware