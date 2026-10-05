"""
Задача 6 [P1] — зоны активности видов и понимание общей картины по локации.

Уровни (от простого к сложному, можно останавливаться на любом):

1. `grid_heatmap()` — сеточная тепловая карта: geohash-ячейки фиксированной точности,
   счётчик/сумма уверенности детекций вида → GeoJSON с числовым весом (leaflet.heat).
2. `coverage_layer()` — **обязательный** слой «где мы вообще способны что-то услышать»
   (буфер вокруг активных микрофонов). Без него пустая ячейка неотличима от «птиц не было»,
   и выводы экологов будут ошибочны.
3. `kde_zones()` — ядерная оценка плотности (`scipy.stats.gaussian_kde`, веса по уверенности)
   с изолиниями и отдельным слоем **границы зоны** (самый интересный слой для гипотез).
4. `compare_periods()` — режим «до/после»: подсвечиваются ячейки, где вид появился или пропал.
5. `hotspots()` — P2: кластеризация DBSCAN в пространстве «время × координаты».

Все уровни — read-only аналитика поверх `detections`, ничего не пишут обратно.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .analytics import DetectionFilter, DetectionPoint, query_detections
from .geo import haversine_m, polygon_feature
from .geohash import (
    cell_bbox_polygon,
    cell_center,
    cell_size_degrees,
    cell_size_meters,
    encode,
)

logger = logging.getLogger(__name__)

DEFAULT_PRECISION = 6
DEFAULT_COVERAGE_RADIUS_M = 300.0
#: Широта, для которой считается размер ячейки в метрах (Вологда/Ростов — порядок величины).
BASE_COVERAGE_LAT = 59.9


@dataclass
class GridCell:
    """Ячейка сетки с агрегатами по виду."""

    cell: str
    lat: float
    lon: float
    count: int = 0
    confidence_sum: float = 0.0
    devices: set = field(default_factory=set)
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    #: Точность geohash, по которой собрана ячейка (влияет на проверку покрытия).
    precision: int = DEFAULT_PRECISION

    @property
    def lat_deg(self) -> float:
        return self.lat

    @property
    def lon_deg(self) -> float:
        return self.lon

    @property
    def weight(self) -> float:
        """Вес ячейки для heatmap: сумма уверенностей (мягче, чем просто счётчик)."""
        return round(self.confidence_sum, 6)

    @property
    def mean_confidence(self) -> float:
        return round(self.confidence_sum / self.count, 6) if self.count else 0.0

    def to_dict(self, covered: Optional[bool] = None) -> dict:
        data = {
            "cell": self.cell,
            "lat": self.lat,
            "lon": self.lon,
            "count": self.count,
            "weight": self.weight,
            "mean_confidence": self.mean_confidence,
            "devices": sorted(self.devices),
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
        }
        if covered is not None:
            data["covered"] = covered
        return data


@dataclass
class ZoneReport:
    """Ответ аналитики зон активности (GeoJSON + метаданные)."""

    level: str
    species_slug: Optional[str]
    features: List[dict]
    meta: dict = field(default_factory=dict)

    def to_geojson(self) -> dict:
        return {"type": "FeatureCollection", "features": self.features}

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "species_slug": self.species_slug,
            "meta": self.meta,
            "features": self.features,
        }


# ---------------------------------------------------------------------------
# Уровень 1 — сеточная тепловая карта
# ---------------------------------------------------------------------------


def grid_heatmap(
    session,
    flt: DetectionFilter,
    precision: int = DEFAULT_PRECISION,
    weight_by_confidence: bool = True,
    species_slug: Optional[str] = None,
    coverage_radius_m: Optional[float] = DEFAULT_COVERAGE_RADIUS_M,
    active_devices: Optional[Sequence] = None,
) -> ZoneReport:
    """
    Бить подтверждённые детекции по geohash-сетке и отдать GeoJSON с весом ячейки.

    Если передан `coverage_radius_m` и список активных устройств, каждая ячейка
    помечается признаком `covered` — «здесь есть данные» vs «здесь могло быть слышно,
    но птиц не зафиксировано» (Задача 6 п.2).
    """
    result = query_detections(session, flt)
    cells: Dict[str, GridCell] = {}
    species_counts: Dict[str, int] = {}

    for point in result.points:
        species_counts[point.species_slug] = species_counts.get(point.species_slug, 0) + 1
        if species_slug and point.species_slug != species_slug:
            continue
        cell_id = encode(point.lat, point.lon, precision)
        cell = cells.get(cell_id)
        if cell is None:
            lat, lon = cell_center(cell_id)
            cell = GridCell(cell=cell_id, lat=lat, lon=lon, precision=precision)
            cells[cell_id] = cell
        cell.count += 1
        cell.confidence_sum += point.confidence if weight_by_confidence else 1.0
        cell.devices.add(point.device_id)
        cell.first_seen = (
            point.window_start_ts if cell.first_seen is None else min(cell.first_seen, point.window_start_ts)
        )
        cell.last_seen = (
            point.window_start_ts if cell.last_seen is None else max(cell.last_seen, point.window_start_ts)
        )

    features = []
    covered_lookup = _coverage_lookup(active_devices or [], coverage_radius_m)
    for cell in cells.values():
        covered = None
        if covered_lookup:
            covered = _cell_is_covered(cell, covered_lookup)
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(cell.lon, 7), round(cell.lat, 7)]},
                "properties": cell.to_dict(covered=covered),
            }
        )
    features.sort(key=lambda f: -f["properties"]["weight"])

    meta = {
        "precision": precision,
        "weight_by_confidence": weight_by_confidence,
        "detections_scanned": len(result.points),
        "cells": len(cells),
        "species_seen": species_counts,
        "query": {
            "bbox_prefilter": result.stats.bbox_prefilter,
            "used_postgis": result.stats.used_postgis,
            "took_ms": result.stats.took_ms,
            "notes": result.stats.notes,
        },
    }
    if coverage_radius_m and coverage_radius_m > 0:
        meta["coverage_radius_m"] = coverage_radius_m
        meta["cells_outside_coverage"] = sum(
            1 for f in features if f["properties"].get("covered") is False
        )
        cell_lat_m, cell_lon_m = cell_size_meters(precision, BASE_COVERAGE_LAT)
        # `covered` — это «хотя бы часть ячейки в зоне покрытия». Если ячейка крупнее
        # радиуса покрытия, половина её может быть вне зоны: об этом надо сказать явно,
        # иначе слой создаёт ложное впечатление полноты данных.
        meta["coverage_cell_m"] = [round(cell_lat_m), round(cell_lon_m)]
        meta["coverage_check_granular"] = cell_lat_m <= 2 * float(coverage_radius_m)
        if not meta["coverage_check_granular"]:
            meta["coverage_check_note"] = (
                f"Ячейка геohash-{precision} (~{round(cell_lat_m)}×{round(cell_lon_m)} м) крупнее "
                f"радиуса покрытия {round(coverage_radius_m)} м: признак `covered` означает "
                f"«ячейка хотя бы частично в зоне покрытия». Для точной карты уменьшите precision."
            )
    # Нормированный вес 0..1 — чтобы и дашборд, и внешняя ГИС красили тепловую карту
    # одинаково, не пересчитывая максимум у себя.
    max_weight = features[0]["properties"]["weight"] if features else 0.0
    meta["max_weight"] = max_weight
    for feature in features:
        feature["properties"]["heat"] = (
            round(feature["properties"]["weight"] / max_weight, 4) if max_weight else 0.0
        )
    return ZoneReport(
        level="grid", species_slug=species_slug, features=features, meta=meta
    )


# ---------------------------------------------------------------------------
# Уровень 2 — карта покрытия
# ---------------------------------------------------------------------------


def coverage_layer(
    session,
    radius_m: float = DEFAULT_COVERAGE_RADIUS_M,
    include_inactive: bool = False,
    precision: int = DEFAULT_PRECISION,
    from_ts: Optional[datetime] = None,
    to_ts: Optional[datetime] = None,
) -> ZoneReport:
    """
    Зона покрытия сети: буфер фиксированного радиуса вокруг активных микрофонов.

    Отдельно возвращаются ячейки, покрытые сетью (в них выводы о виде корректны) и
    «белые» ячейки внутри bbox данных (здесь данных просто нет).
    """
    from sqlalchemy import select

    from .config import get_settings
    from .devices import device_status
    from .models import Device, utcnow

    settings_gap = get_settings().gap_alert_sec
    devices = list(session.execute(select(Device)).scalars())
    if not include_inactive:
        devices = [d for d in devices if d.is_active]

    features = []
    devices_info = []
    for device in devices:
        status = device_status(device, settings_gap, now=utcnow())
        lat, lon = device.lat, device.lon
        # Мобильное устройство « wherever оно было»: без последней координаты слой не рисуем.
        if lat is None or lon is None:
            devices_info.append({**status, "located": False})
            continue
        features.append(
            polygon_feature(
                _circle_ring(lat, lon, radius_m),
                {
                    "device_id": device.id,
                    "name": device.name,
                    "device_type": device.device_type,
                    "radius_m": radius_m,
                    "status": status["status"],
                    "seconds_since_last_seen": status["seconds_since_last_seen"],
                },
            )
        )
        devices_info.append({**status, "located": True})

    covered_cells = set()
    for device in devices:
        if device.lat is None or device.lon is None:
            continue
        covered_cells.update(
            _cells_around(device.lat, device.lon, radius_m, precision)
        )

    meta = {
        "radius_m": radius_m,
        "precision": precision,
        "devices_total": len(devices),
        "devices_located": len(features),
        "cells_covered": len(covered_cells),
        "from": from_ts.isoformat() if from_ts else None,
        "to": to_ts.isoformat() if to_ts else None,
        "devices": devices_info,
        "note": (
            "Пустая ячейка внутри покрытия = «покрытие есть, но вида не зафиксировано». "
            "Пустая ячейка вне покрытия = «данных здесь просто нет»."
        ),
    }
    return ZoneReport(level="coverage", species_slug=None, features=features, meta=meta)


def _coverage_lookup(
    devices: Sequence, radius_m: Optional[float]
) -> List[Tuple[float, float, float]]:
    """Список активных микрофонов с радиусом покрытия; пустой список = «слой выключен»."""
    if not radius_m or radius_m <= 0:
        return []
    lookup = []
    for device in devices:
        lat = getattr(device, "lat", None)
        lon = getattr(device, "lon", None)
        if lat is None or lon is None:
            continue
        lookup.append((float(lat), float(lon), float(radius_m)))
    return lookup


def _cell_is_covered(cell: GridCell, lookup: Sequence[Tuple[float, float, float]]) -> bool:
    """
    Покрыта ли ячейка: расстояние считается **между микрофоном и ближайшей точкой ячейки**.

    Проверка только по центру дала бы ложный `covered=True`: геohash-ячейка — это
    прямоугольник шириной до ~600 м (при precision 6), и её центр может лежать внутри
    круга покрытия, пока сама ячейка наполовину вне его.
    """
    cell_lat, cell_lon = cell_size_degrees(cell.precision)
    half_dlat = cell_lat / 2.0
    half_dlon = cell_lon / 2.0
    for lat, lon, radius in lookup:
        # Ближайшая к микрофону точка прямоугольного контура ячейки.
        near_lat = min(max(lat, cell.lat - half_dlat), cell.lat + half_dlat)
        cos_lat = max(math.cos(math.radians(near_lat)), 1e-6)
        near_lon = cell.lon + max(-half_dlon, min(half_dlon, (lon - cell.lon) / cos_lat))
        if haversine_m(lat, lon, near_lat, near_lon) <= radius:
            return True
    return False


def _cells_around(lat: float, lon: float, radius_m: float, precision: int) -> Iterable[str]:
    """geohash-ячейки, попадающие в круг покрытия устройства."""
    from .geohash import cell_size_degrees, decode_bbox

    lat_step, lon_step = cell_size_degrees(precision)
    min_lat, min_lon, max_lat, max_lon = decode_bbox(encode(lat, lon, precision))
    steps_lat = max(0, int(math.ceil(radius_m / 111_320.0 / lat_step)))
    steps_lon = max(0, int(math.ceil(radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6)) / lon_step)))
    for i in range(-steps_lat, steps_lat + 1):
        for j in range(-steps_lon, steps_lon + 1):
            probe_lat = cell_center(encode(min_lat, min_lon, precision))[0] + i * lat_step
            probe_lon = cell_center(encode(min_lat, min_lon, precision))[1] + j * lon_step
            if not -90 <= probe_lat <= 90 or not -180 <= probe_lon <= 180:
                continue
            cell_id = encode(probe_lat, probe_lon, precision)
            if haversine_m(lat, lon, probe_lat, probe_lon) <= radius_m * 1.5:
                yield cell_id


def _circle_ring(lat: float, lon: float, radius_m: float, segments: int = 48) -> List[Tuple[float, float]]:
    """Кольцо круга радиуса `radius_m` (в градусах) — для полигона покрытия."""
    lat_radius = radius_m / 111_320.0
    cos_lat = max(math.cos(math.radians(lat)), 1e-6)
    lon_radius = radius_m / (111_320.0 * cos_lat)
    ring = []
    for step in range(segments + 1):
        angle = 2 * math.pi * step / segments
        ring.append((lon + lon_radius * math.cos(angle), lat + lat_radius * math.sin(angle)))
    return ring


# ---------------------------------------------------------------------------
# Уровень 3 — KDE и границы зон
# ---------------------------------------------------------------------------


def kde_zones(
    session,
    flt: DetectionFilter,
    species_slug: Optional[str] = None,
    bandwidth_m: Optional[float] = None,
    grid_resolution: int = 64,
    levels: Sequence[float] = (0.2, 0.4, 0.6, 0.8),
    weight_by_confidence: bool = True,
    min_detections: int = 5,
) -> ZoneReport:
    """
    Сглаженная зона активности (KDE по точкам детекций вида за период).

    Возвращает полигоны-контуры и отдельный слой `boundary` — граница зоны активности,
    ради которой и строятся гипотезы («вот тут активность обрывается»).
    """
    result = query_detections(session, flt)
    points = [
        p
        for p in result.points
        if (species_slug is None or p.species_slug == species_slug)
        and p.lat is not None
        and p.lon is not None
    ]

    meta: Dict[str, object] = {
        "points": len(points),
        "species_slug": species_slug,
        "grid_resolution": grid_resolution,
        "min_detections": min_detections,
        "weights": weight_by_confidence,
        "query": {"took_ms": result.stats.took_ms, "notes": result.stats.notes},
    }
    if len(points) < max(3, min_detections):
        meta["skipped"] = (
            f"Недостаточно точек для KDE ({len(points)} < {max(3, min_detections)}): "
            "нужна серия наблюдений, а не единичные всплески"
        )
        return ZoneReport(level="kde", species_slug=species_slug, features=[], meta=meta)

    try:
        from scipy.stats import gaussian_kde
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        meta["skipped"] = f"scipy недоступна: {exc}"
        return ZoneReport(level="kde", species_slug=species_slug, features=[], meta=meta)

    lats = [p.lat for p in points]
    lons = [p.lon for p in points]
    weights = [p.confidence if weight_by_confidence else 1.0 for p in points]

    # Работаем в локальной плоскости (метры), чтобы изолинии были «круглыми», а не вытянутыми.
    lat0 = sum(lats) / len(lats)
    lon0 = sum(lons) / len(lons)
    xs = [(lon - lon0) * 111_320.0 * math.cos(math.radians(lat0)) for lon in lons]
    ys = [(lat - lat0) * 111_320.0 for lat in lats]

    bw = bandwidth_m or _auto_bandwidth_m(points)
    meta["bandwidth_m"] = round(float(bw), 1)

    try:
        values = np.asarray(weights)
        if values.std() < 1e-9:
            values = None  # все веса одинаковы — KDE без весов
        kde = gaussian_kde(np.vstack([xs, ys]), bw_method=bw / _spread_m(xs, ys), weights=values)
    except Exception as exc:  # noqa: BLE001 - вырожденные наборы точек
        meta["skipped"] = f"KDE не построилась: {exc}"
        return ZoneReport(level="kde", species_slug=species_slug, features=[], meta=meta)

    pad = _spread_m(xs, ys) * 0.25 + bw
    x_min, x_max = min(xs) - pad, max(xs) + pad
    y_min, y_max = min(ys) - pad, max(ys) + pad
    gx = [x_min + (x_max - x_min) * i / (grid_resolution - 1) for i in range(grid_resolution)]
    gy = [y_min + (y_max - y_min) * i / (grid_resolution - 1) for i in range(grid_resolution)]
    grid_x, grid_y = np.meshgrid(np.asarray(gx), np.asarray(gy))
    density = kde(np.vstack([grid_x.ravel(), grid_y.ravel()])).reshape(grid_x.shape)
    peak = float(density.max()) if density.size else 0.0
    meta["peak_density"] = round(peak, 8)

    features = _contour_features(density, grid_x, grid_y, lat0, lon0, peak, levels, species_slug)
    return ZoneReport(level="kde", species_slug=species_slug, features=features, meta=meta)


def _spread_m(xs: Sequence[float], ys: Sequence[float]) -> float:
    spread = max(
        max(xs) - min(xs) if xs else 0.0,
        max(ys) - min(ys) if ys else 0.0,
    )
    return float(spread or 100.0)


def _auto_bandwidth_m(points: Sequence[DetectionPoint]) -> float:
    """Эвристика: половина медианного расстояния между соседними точками, но не меньше 150 м."""
    if len(points) < 2:
        return 300.0
    sample = points[: min(len(points), 200)]
    distances = []
    for i in range(1, len(sample)):
        distances.append(
            haversine_m(sample[i - 1].lat, sample[i - 1].lon, sample[i].lat, sample[i].lon)
        )
    distances.sort()
    median = distances[len(distances) // 2]
    return float(max(150.0, min(median / 2, 2000.0)))


def _contour_features(
    density,
    grid_x,
    grid_y,
    lat0: float,
    lon0: float,
    peak: float,
    levels: Sequence[float],
    species_slug: Optional[str],
) -> List[dict]:
    """Полигоны-изолинии KDE + контур максимальной зоны как отдельный слой `boundary`."""
    if peak <= 0:
        return []
    features: List[dict] = []
    scale_x = 111_320.0 * math.cos(math.radians(lat0))
    scale_y = 111_320.0

    def to_ring(row: int, col: int) -> Tuple[float, float]:
        lat = lat0 + float(grid_y[row, col]) / scale_y
        lon = lon0 + float(grid_x[row, col]) / scale_x
        return (lon, lat)

    for level in levels:
        threshold = peak * float(level)
        ring = _marching_square_ring(density, grid_x, grid_y, threshold, to_ring)
        if not ring:
            continue
        features.append(
            polygon_feature(
                ring,
                {
                    "layer": "density",
                    "level": level,
                    "threshold": round(threshold, 8),
                    "species_slug": species_slug,
                },
            )
        )

    boundary = _marching_square_ring(density, grid_x, grid_y, peak * float(levels[-1]), to_ring)
    if boundary:
        features.append(
            polygon_feature(
                boundary,
                {
                    "layer": "boundary",
                    "note": "Граница зоны активности — слой для проверки гипотез",
                    "species_slug": species_slug,
                    "level": levels[-1],
                },
            )
        )
    return features


def _marching_square_ring(density, grid_x, grid_y, threshold: float, to_ring) -> List[Tuple[float, float]]:
    """
    Простая изолиния по сетке (marching squares) без внешних зависимостей.

    Возвращает внешнее кольцо контура (не идеально, но достаточно для визуальной зоны).
    """
    rows, cols = density.shape
    segments: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    for r in range(rows - 1):
        for c in range(cols - 1):
            values = [
                (density[r, c], (r, c)),
                (density[r, c + 1], (r, c + 1)),
                (density[r + 1, c + 1], (r + 1, c + 1)),
                (density[r + 1, c], (r + 1, c)),
            ]
            above = [i for i, (value, _) in enumerate(values) if value >= threshold]
            if len(above) in (0, 4):
                continue
            for i in range(4):
                j = (i + 1) % 4
                inside_i = i in above
                inside_j = j in above
                if inside_i == inside_j:
                    continue
                p_i = _edge_point(values[i], values[j], threshold)
                p_j = _edge_point(values[j], values[i], threshold)
                segments.append((p_i, p_j))
    if not segments:
        return []

    # Склеиваем отрезки в кольцо по близости концов.
    ring: List[Tuple[float, float]] = []
    current = segments[0][0]
    remaining = list(segments)
    tolerance = max(
        abs(float(grid_x[0, 1] - grid_x[0, 0])) if cols > 1 else 1.0,
        abs(float(grid_y[1, 0] - grid_y[0, 0])) if rows > 1 else 1.0,
    )
    while remaining:
        ring.append(to_ring(int(current[0]), int(current[1])))
        next_point = None
        for index, (start, end) in enumerate(remaining):
            if _close(start, current, tolerance) or _close(end, current, tolerance):
                next_point = end if _close(start, current, tolerance) else start
                remaining.pop(index)
                break
        if next_point is None:
            break
        current = next_point
        if len(ring) > 4 * len(segments):
            break
    if len(ring) >= 3:
        ring.append(ring[0])
        return ring
    return []


def _edge_point(a, b, threshold: float):
    (value_a, point_a), (value_b, point_b) = a, b
    denom = value_a - value_b
    if abs(denom) < 1e-15:
        t = 0.5
    else:
        t = (value_a - threshold) / denom
    t = min(1.0, max(0.0, t))
    row = point_a[0] + (point_b[0] - point_a[0]) * t
    col = point_a[1] + (point_b[1] - point_a[1]) * t
    return (row, col)


def _close(a, b, tolerance: float) -> bool:
    return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance


# ---------------------------------------------------------------------------
# Уровень 4 — сравнение периодов
# ---------------------------------------------------------------------------


def compare_periods(
    session,
    species_slug: str,
    period_a: Tuple[datetime, datetime],
    period_b: Tuple[datetime, datetime],
    precision: int = DEFAULT_PRECISION,
    min_count_delta: int = 1,
    normalize_by_devices: bool = True,
) -> ZoneReport:
    """
    «Период A против периода B»: ячейки, где вид **появился** или **пропал**.

    `normalize_by_devices` сравнивает не абсолютные счётчики, а интенсивность
    (детекций на активное устройство в ячейке) — иначе период с большим числом
    устройников всегда «побеждает» и сравнение бессмысленно.
    """
    from .devices import list_devices

    cells_a = _cells_for_period(session, species_slug, period_a, precision)
    cells_b = _cells_for_period(session, species_slug, period_b, precision)

    active_devices = [d for d in list_devices(session, active_only=False) if d.is_active]
    device_count = max(1, len(active_devices))

    all_cells = set(cells_a) | set(cells_b)
    features = []
    summary = {"appeared": 0, "disappeared": 0, "grew": 0, "shrunk": 0, "stable": 0}

    for cell in sorted(all_cells):
        a = cells_a.get(cell)
        b = cells_b.get(cell)
        count_a = a["count"] if a else 0
        count_b = b["count"] if b else 0
        norm_a = (count_a / device_count) if normalize_by_devices else count_a
        norm_b = (count_b / device_count) if normalize_by_devices else count_b
        delta = norm_b - norm_a

        if count_a == 0 and count_b >= min_count_delta:
            change = "appeared"
        elif count_b == 0 and count_a >= min_count_delta:
            change = "disappeared"
        elif delta >= min_count_delta:
            change = "grew"
        elif delta <= -min_count_delta:
            change = "shrunk"
        else:
            change = "stable"
        summary[change] += 1

        if change == "stable":
            continue
        features.append(
            polygon_feature(
                cell_bbox_polygon(cell),
                {
                    "layer": "period_diff",
                    # `kind` и `count_a/count_b` — короткие имена для дашборда,
                    # `change`/`period_*_count` — точные для внешних потребителей.
                    "kind": change,
                    "change": change,
                    "cell": cell,
                    "count_a": count_a,
                    "count_b": count_b,
                    "period_a_count": count_a,
                    "period_b_count": count_b,
                    "delta": round(delta, 4),
                    "devices_a": sorted(a["devices"]) if a else [],
                    "devices_b": sorted(b["devices"]) if b else [],
                    "species_slug": species_slug,
                },
            )
        )

    meta = {
        "species_slug": species_slug,
        "precision": precision,
        "period_a": [period_a[0].isoformat(), period_a[1].isoformat()],
        "period_b": [period_b[0].isoformat(), period_b[1].isoformat()],
        "normalize_by_devices": normalize_by_devices,
        "active_devices": device_count,
        "min_count_delta": min_count_delta,
        "summary": summary,
    }
    return ZoneReport(level="period_diff", species_slug=species_slug, features=features, meta=meta)


def _cells_for_period(
    session,
    species_slug: str,
    period: Tuple[datetime, datetime],
    precision: int = DEFAULT_PRECISION,
) -> Dict[str, dict]:
    flt = DetectionFilter(
        species=[species_slug], date_from=period[0], date_to=period[1], limit=50_000
    )
    result = query_detections(session, flt)
    cells: Dict[str, dict] = {}
    for point in result.points:
        cell_id = encode(point.lat, point.lon, precision)
        cell = cells.setdefault(cell_id, {"count": 0, "devices": set()})
        cell["count"] += 1
        cell["devices"].add(point.device_id)
    for cell in cells.values():
        cell["devices"] = sorted(cell["devices"])
    return cells


# ---------------------------------------------------------------------------
# Уровень 5 (P2) — «горячие точки» (кластеры в пространстве-время)
# ---------------------------------------------------------------------------


def hotspots(
    session,
    flt: DetectionFilter,
    species_slug: Optional[str] = None,
    eps_m: float = 500.0,
    min_samples: int = 3,
    time_bucket_hours: int = 24,
) -> ZoneReport:
    """
    Кластеризация DBSCAN детекций в пространстве «координаты + время».

    Даёт полигоны-кластеры с метаданными (вид, период, число детекций, средняя
  уверенность) — удобно для гипотез вида «эта зона активна только весной».
    """
    result = query_detections(session, flt)
    points = [p for p in result.points if not species_slug or p.species_slug == species_slug]
    meta: Dict[str, object] = {"points": len(points), "eps_m": eps_m, "min_samples": min_samples}
    if len(points) < min_samples:
        meta["skipped"] = "Недостаточно точек для кластеризации"
        return ZoneReport(level="hotspots", species_slug=species_slug, features=[], meta=meta)

    features = []
    buckets = _time_buckets(points, time_bucket_hours)
    for (species_key, bucket_start), bucket_points in buckets.items():
        clusters = _dbscan_coords(bucket_points, eps_m, min_samples)
        for index, cluster in enumerate(clusters, start=1):
            lats = [p.lat for p in cluster]
            lons = [p.lon for p in cluster]
            center_lat = sum(lats) / len(lats)
            center_lon = sum(lons) / len(lons)
            span = max(
                haversine_m(min(lats), min(lons), max(lats), max(lons)),
                eps_m,
            )
            features.append(
                polygon_feature(
                    _circle_ring(center_lat, center_lon, span / 2),
                    {
                        "layer": "hotspot",
                        "cluster": index,
                        "species_slug": species_key,
                        "bucket_start": bucket_start,
                        "detections": len(cluster),
                        "mean_confidence": round(
                            sum(p.confidence for p in cluster) / len(cluster), 4
                        ),
                        "radius_m": round(span / 2, 1),
                        "center_lat": center_lat,
                        "center_lon": center_lon,
                        "period_from": min(p.window_start_ts for p in cluster).isoformat(),
                        "period_to": max(p.window_start_ts for p in cluster).isoformat(),
                    },
                )
            )
    meta["clusters"] = len(features)
    return ZoneReport(level="hotspots", species_slug=species_slug, features=features, meta=meta)


def _time_buckets(points: Sequence[DetectionPoint], hours: int) -> Dict[str, List[DetectionPoint]]:
    from datetime import timedelta

    buckets: Dict[Tuple[str, str], List[DetectionPoint]] = {}
    for point in points:
        bucket = (point.window_start_ts - timedelta(hours=point.window_start_ts.hour % max(hours, 1))).replace(
            minute=0, second=0, microsecond=0
        )
        buckets.setdefault((point.species_slug, bucket.isoformat()), []).append(point)
    return {key: value for key, value in buckets.items()}


def _dbscan_coords(points: Sequence[DetectionPoint], eps_m: float, min_samples: int) -> List[List[DetectionPoint]]:
    """Небольшой DBSCAN по координатам (без sklearn, чтобы не тянуть зависимость в рантайм)."""
    remaining = list(points)
    clusters: List[List[DetectionPoint]] = []
    while remaining:
        seed = remaining.pop()
        neighbors = [seed] + [p for p in remaining if _close_points(seed, p, eps_m)]
        if len(neighbors) < min_samples:
            continue
        remaining = [p for p in remaining if p not in neighbors]
        cluster = list(neighbors)
        for index in range(len(neighbors)):
            current = neighbors[index]
            new_neighbors = [p for p in remaining if _close_points(current, p, eps_m)]
            if len(new_neighbors) >= min_samples - 1:
                for p in new_neighbors:
                    if p not in neighbors:
                        neighbors.append(p)
                remaining = [p for p in remaining if p not in new_neighbors]
                cluster.extend(new_neighbors)
        clusters.append(cluster)
    return clusters


def _close_points(a: DetectionPoint, b: DetectionPoint, eps_m: float) -> bool:
    return haversine_m(a.lat, a.lon, b.lat, b.lon) <= eps_m

