"""
Дедупликация подтверждённых детекций на границах перекрывающихся чанков (Задача 3, п.1а).

Устройство шлёт чанки с перекрытием (по умолчанию 5 с), поэтому один и тот же момент
пения попадает в два соседних чанка, а сетка окон у них сдвинута. Сравнивать детекции
по точным меткам времени бесполезно — нужен именно **пересекающийся временной интервал**.

Правило: детекция считается дублем, если у того же устройства уже есть детекция того же
вида, с которой пересечение составляет не менее `dedup_overlap_frac` длины более
короткого интервала. Первую (более раннюю) детекцию считаем канонической.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Detection

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Interval:
    """Временной интервал в Unix-секундах."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def intersection_length(a: Interval, b: Interval) -> float:
    """Длина пересечения двух интервалов."""
    return max(0.0, min(a.end, b.end) - max(a.start, b.start))


def overlap_fraction(a: Interval, b: Interval) -> float:
    """Доля пересечения от длины более короткого интервала (0..1)."""
    shortest = min(a.duration, b.duration)
    if shortest <= 0:
        return 1.0 if a.start == b.start else 0.0
    return intersection_length(a, b) / shortest


def to_interval(start_ts, end_ts) -> Interval:
    """Собрать интервал из datetime-меток (aware или naive UTC)."""
    from .models import ensure_utc

    start = ensure_utc(start_ts)
    end = ensure_utc(end_ts)
    return Interval(start.timestamp(), end.timestamp())


def detection_interval(detection: Detection) -> Interval:
    """Интервал детекции из БД (точки времени хранятся как datetime)."""
    start, end = detection.interval
    return Interval(start, end)


def find_duplicate(
    session: Session,
    device_id: str,
    species_slug: str,
    interval: Interval,
    chunk_start_ts,
    chunk_end_ts,
    overlap_frac: float = 0.5,
    exclude_ids: Optional[Iterable[int]] = None,
) -> Optional[Detection]:
    """
    Найти уже сохранённую детекцию того же вида, продублированную на стыке чанков.

    Кандидаты ограничены перекрывающейся зоной стыка (очевидные детекции внутри чанка
    не могут быть дублями), поэтому выборка остаётся дешёвой даже при тысячах детекций.
    """
    interval_overlap = overlap_fraction(interval, to_interval(chunk_start_ts, chunk_end_ts))
    if interval_overlap <= 0:
        # Детекция целиком внутри чанка — соседним чанком она перекрыта не может быть.
        return None
    window_start = max(interval.start - 1.0, chunk_start_ts.timestamp() - 1.0)
    window_end = min(interval.end + 1.0, chunk_end_ts.timestamp() + 1.0)

    query = (
        select(Detection)
        .where(
            Detection.device_id == device_id,
            Detection.species_slug == species_slug,
            Detection.window_start_ts < to_datetime(window_end),
            Detection.window_end_ts > to_datetime(window_start),
            Detection.duplicate_of_id.is_(None),
        )
        .order_by(Detection.window_start_ts)
        .limit(50)
    )
    if exclude_ids:
        query = query.where(Detection.id.notin_(list(exclude_ids)))
    for candidate in session.execute(query).scalars():
        if overlap_fraction(interval, detection_interval(candidate)) >= overlap_frac:
            return candidate
    return None


def to_datetime(value: float):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value, tz=timezone.utc)


def dedup_detections(
    session: Session,
    detections: Sequence[Detection],
    overlap_frac: float = 0.5,
) -> Tuple[List[Detection], List[Detection]]:
    """
    Отобрать дубли детекций одного чанка (в т.ч. детекции, склеенные разными окнами).

    Возвращает `(уникальные, дубли)`. Помечает дубли полем `duplicate_of_id`,
    чтобы в отчётности и на карте их можно было отфильтровать единообразно.
    """
    unique: List[Detection] = []
    duplicates: List[Detection] = []
    for detection in detections:
        interval = detection.interval
        matched: Optional[Detection] = None
        for existing in unique:
            if existing.species_slug != detection.species_slug:
                continue
            if overlap_fraction(interval, detection_interval(existing)) >= overlap_frac:
                matched = existing
                break
        if matched is None:
            unique.append(detection)
        else:
            detection.duplicate_of_id = matched.id
            duplicates.append(detection)
            logger.debug(
                "Детекция %s (%s) признана дублем детекции %s на стыке чанков",
                detection.id,
                detection.species_slug,
                matched.id,
            )
    return unique, duplicates