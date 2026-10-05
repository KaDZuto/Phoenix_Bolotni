"""
Геохеш-сетка для зон активности (Задача 6, п.1).

Собственная компактная реализация geohash (base32, чередование бит широты и долготы):
позволяет бинить детекции в ячейки фиксированной точности без внешних зависимостей
и одинаково работает на любой БД. Точность 5 → ячейка ≈ 4.9 × 4.9 км, 6 → ≈ 1.2 × 0.6 км.
"""

from __future__ import annotations

import math
from typing import Iterable, List, Sequence, Tuple

BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"

def _precision_degrees(precision: int) -> Tuple[float, float]:
    """
    Размер ячейки в градусах: (по широте, по долготе).

    Считается, а не берётся из таблицы: символ geohash кодирует 5 бит, которые
    чередуются долгота/широта, поэтому на чётной точности бит по широте на один
    меньше, чем по долготе. Табличные значения для lat/lon тут регулярно ошибаются
    вдвое (а на некоторых широтах — вчетверо), и ошибка всплывает как «ячейка
    крупнее, чем на самом деле».
    """
    total_bits = precision * 5
    lat_bits = total_bits // 2
    lon_bits = total_bits - lat_bits
    return (180.0 / 2**lat_bits, 360.0 / 2**lon_bits)


#: Размеры ячейки в градусах: точность → (по широте, по долготе).
PRECISION_DEGREES = {p: _precision_degrees(p) for p in range(1, 13)}

BBox = Tuple[float, float, float, float]  # (min_lat, min_lon, max_lat, max_lon)


def encode(lat: float, lon: float, precision: int = 6) -> str:
    """Закодировать точку в geohash заданной точности."""
    if not -90.0 <= lat <= 90.0:
        raise ValueError(f"Широта вне диапазона: {lat}")
    if not -180.0 <= lon <= 180.0:
        raise ValueError(f"Долгота вне диапазона: {lon}")
    if not 1 <= precision <= 12:
        raise ValueError(f"Недопустимая точность geohash: {precision}")

    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    bits = (16, 8, 4, 2, 1)
    characters: List[str] = []
    bit_index = 0
    char_value = 0
    even_bit = True

    while len(characters) < precision:
        if even_bit:
            mid = (lon_range[0] + lon_range[1]) / 2
            if lon >= mid:
                char_value |= bits[bit_index]
                lon_range[0] = mid
            else:
                lon_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat >= mid:
                char_value |= bits[bit_index]
                lat_range[0] = mid
            else:
                lat_range[1] = mid
        even_bit = not even_bit

        if bit_index < 4:
            bit_index += 1
        else:
            characters.append(BASE32[char_value])
            bit_index = 0
            char_value = 0
    return "".join(characters)


def decode_bbox(cell: str) -> BBox:
    """Границы ячейки geohash: `(min_lat, min_lon, max_lat, max_lon)`."""
    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    even_bit = True
    for char in cell:
        index = BASE32.find(char.lower())
        if index < 0:
            raise ValueError(f"Некорректный символ geohash: {char!r}")
        for mask in (16, 8, 4, 2, 1):
            if even_bit:
                mid = (lon_range[0] + lon_range[1]) / 2
                if index & mask:
                    lon_range[0] = mid
                else:
                    lon_range[1] = mid
            else:
                mid = (lat_range[0] + lat_range[1]) / 2
                if index & mask:
                    lat_range[0] = mid
                else:
                    lat_range[1] = mid
            even_bit = not even_bit
    return lat_range[0], lon_range[0], lat_range[1], lon_range[1]


def cell_center(cell: str) -> Tuple[float, float]:
    """Центр ячейки geohash."""
    min_lat, min_lon, max_lat, max_lon = decode_bbox(cell)
    return (min_lat + max_lat) / 2, (min_lon + max_lon) / 2


def cell_size_degrees(precision: int) -> Tuple[float, float]:
    """
    Максимальный размер ячейки: (по широте, по долготе) в градусах.

    Именно максимальный, а не средний: при выборе радиуса покрытия важно знать
    худший случай, иначе ячейка окажется «покрыта» микрофоном, который на другом
    её краю и в 700 м от детекции.
    """
    if precision in PRECISION_DEGREES:
        return PRECISION_DEGREES[precision]
    lat_size, lon_size = PRECISION_DEGREES[6]
    factor = 2.0 ** (precision - 6)
    return (lat_size * factor, lon_size * factor)


def cell_size_meters(precision: int, lat: float = 0.0) -> Tuple[float, float]:
    """Размер ячейки в метрах на заданной широте: (север-юг, запад-восток)."""
    lat_size, lon_size = cell_size_degrees(precision)
    lon_scale = max(math.cos(math.radians(lat)), 1e-6)
    return lat_size * 111_320.0, lon_size * 111_320.0 * lon_scale


def cell_bbox_polygon(cell: str) -> List[Tuple[float, float]]:
    """Кольцо ячейки для GeoJSON-полигона (замкнутое, порядок lon, lat)."""
    min_lat, min_lon, max_lat, max_lon = decode_bbox(cell)
    return [
        (min_lon, min_lat),
        (max_lon, min_lat),
        (max_lon, max_lat),
        (min_lon, max_lat),
        (min_lon, min_lat),
    ]


def cells_in_bbox(bbox: BBox, precision: int) -> List[str]:
    """Перечислить geohash-ячейки, перекрывающие прямоугольник (для слоя покрытия)."""
    min_lat, min_lon, max_lat, max_lon = bbox
    out: List[str] = []
    for lat in _sequence(min_lat, max_lat, precision):
        for lon in _sequence(min_lon, max_lon, precision):
            out.append(encode(lat, lon, precision))
    return sorted(set(out))


def _sequence(start: float, end: float, precision: int) -> Iterable[float]:
    """Точки с шагом примерно в размер ячейки (середина шага — надёжнее границ)."""
    lat_step, lon_step = cell_size_degrees(precision)
    if start > end:
        start, end = end, start
    step = min(lat_step, lon_step) * 0.75
    value = start + step / 2
    while value < end:
        yield min(max(value, -89.999999), 89.999999)
        value += step
    yield min(max(end - step / 2, -89.999999), 89.999999)


def merge_adjacent(values: Sequence[str]) -> List[List[str]]:
    """Сгруппировать geohash-ячейки по общему префиксу (для «укрупнения» сетки)."""
    if not values:
        return []
    out: List[List[str]] = []
    for value in sorted(values):
        if out and value.startswith(out[-1][0][:3]):
            out[-1].append(value)
        else:
            out.append([value])
    return out