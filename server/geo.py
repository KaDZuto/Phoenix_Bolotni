"""
Гео-утилиты сервера: расстояния, bbox-предикаты, сборка GeoJSON.

Расчёты расстояний и построение зон выполняются в Python, поэтому аналитика работает
одинаково и на Postgres/PostGIS (продакшен), и на SQLite (локальная разработка/тесты).
Индекс в БД используется через дешёвые bbox-предикаты по `lat/lon`, а для радиуса
на Postgres дополнительно доступен индекс по PostGIS-геометрии (см. `zones.py`,
`db.py` и миграции alembic).
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

EARTH_RADIUS_M = 6371008.8  # средний радиус Земли (WGS84), метры


def validate_coordinates(lat: float, lon: float) -> Tuple[float, float]:
    """Проверить диапазоны широты/долготы (WGS84)."""
    if not -90.0 <= lat <= 90.0:
        raise ValueError(f"Широта вне диапазона [-90, 90]: {lat}")
    if not -180.0 <= lon <= 180.0:
        raise ValueError(f"Долгота вне диапазона [-180, 180]: {lon}")
    return float(lat), float(lon)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние между двумя точками по сфере, метры."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def bbox_around(lat: float, lon: float, radius_m: float) -> Tuple[float, float, float, float]:
    """Ограничивающий прямоугольник (min_lat, min_lon, max_lat, max_lon) вокруг точки."""
    if radius_m <= 0:
        return lat, lon, lat, lon
    d_lat = math.degrees(radius_m / EARTH_RADIUS_M)
    cos_lat = math.cos(math.radians(lat))
    d_lon = math.degrees(radius_m / (EARTH_RADIUS_M * cos_lat)) if abs(cos_lat) > 1e-9 else 180.0
    return lat - d_lat, lon - d_lon, lat + d_lat, lon + d_lon


def parse_bbox(raw: Optional[str]) -> Optional[Tuple[float, float, float, float]]:
    """Разобрать строку `min_lon,min_lat,max_lon,max_lat` (как у GeoJSON-сервисов)."""
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox должен содержать 4 значения: min_lon,min_lat,max_lon,max_lat")
    try:
        min_lon, min_lat, max_lon, max_lat = (float(p) for p in parts)
    except ValueError as exc:
        raise ValueError(f"Некорректный bbox: {raw!r}") from exc
    if min_lon > max_lon or min_lat > max_lat:
        raise ValueError(f"Координаты bbox перепутаны местами: {raw!r}")
    validate_coordinates(min_lat, min_lon)
    validate_coordinates(max_lat, max_lon)
    return min_lat, min_lon, max_lat, max_lon


def format_bbox(bbox: Sequence[float]) -> str:
    """Собрать bbox обратно в строку (в порядке min_lon,min_lat,max_lon,max_lat)."""
    min_lat, min_lon, max_lat, max_lon = bbox
    return f"{min_lon:.6f},{min_lat:.6f},{max_lon:.6f},{max_lat:.6f}"


def feature_collection(features: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    return {"type": "FeatureCollection", "features": list(features)}


def point_feature(lat: float, lon: float, properties: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Точка в формате GeoJSON (порядок координат — lon, lat, как требует RFC 7946)."""
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [round(float(lon), 7), round(float(lat), 7)]},
        "properties": properties or {},
    }


def polygon_feature(
    ring: Iterable[Sequence[float]],
    properties: Optional[Dict[str, Any]] = None,
    holes: Optional[List[Iterable[Sequence[float]]]] = None,
) -> Dict[str, Any]:
    """Полигон в формате GeoJSON (кольцо замыкается автоматически)."""
    coordinates = [_close_ring(ring)]
    for hole in holes or []:
        coordinates.append(_close_ring(hole))
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": coordinates},
        "properties": properties or {},
    }


def _close_ring(ring: Iterable[Sequence[float]]) -> List[List[float]]:
    points = [[round(float(x), 7), round(float(y), 7)] for x, y in ring]
    if points and points[0] != points[-1]:
        points.append(list(points[0]))
    return points