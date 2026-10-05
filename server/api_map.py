"""
API карты активности (Задача 5, td3.md раздел 5).

Эндпоинты (все — под авторизацией дашборда, `PHOENIX_DASHBOARD_*`):

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/api/map/species` | справочник видов (ru/latin/биотоп) + число наблюдений |
| GET | `/api/map/devices` | стационарные микрофоны (GeoJSON) со статусом активности |
| GET | `/api/map/detections` | подтверждённые детекции точками (фильтры: вид, период, bbox, радиус) |
| GET | `/api/map/activity_zones` | зоны активности вида: сетка (heatmap) и/или KDE с границей зоны |
| GET | `/api/map/coverage` | слой покрытия сети (буферы вокруг активных микрофонов) |
| GET | `/api/map/period_diff` | сравнение двух периодов: где вид появился/пропал |
| GET | `/api/map/summary` | общая сводка по площадке (для шапки дашборда) |
| GET | `/api/map/export.csv` | выгрузка наблюдений в CSV (для внешних сервисов) |
| GET | `/api/map/export.geojson` | выгрузка в GeoJSON (для ГИС/QGIS) |
| GET | `/` | сам дашборд (Leaflet, статика из `webapp_map/`) |

Все ответы — GeoJSON/JSON, чтобы их можно было открыть и в ГИС, и в браузере.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import date, datetime, time, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from .alerts import build_gap_report
from .analytics import DetectionFilter, query_detections, species_summary
from .auth import require_dashboard_auth
from .config import get_settings
from .db import get_db
from .devices import device_status, list_devices
from .geo import feature_collection, parse_bbox, point_feature
from .models import ensure_utc, utcnow
from .species import get_species_registry
from .zones import (
    DEFAULT_COVERAGE_RADIUS_M,
    DEFAULT_PRECISION,
    compare_periods,
    coverage_layer,
    grid_heatmap,
    hotspots,
    kde_zones,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/map", tags=["map"])

MAX_EXPORT_ROWS = 100_000
MAX_SCAN_LIMIT = 200_000
MAX_LEAFLET_POINTS = 50_000


# ------------------------------------------------------------------ параметры


def _parse_datetime(value: Optional[str], field: str, end_of_day: bool = False) -> Optional[datetime]:
    """Разобрать `from`/`to`: дата (YYYY-MM-DD) или метка времени ISO-8601 (в UTC)."""
    if value is None or value == "":
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            parsed_date = date.fromisoformat(text)
            moment = datetime.combine(
                parsed_date, time.max if end_of_day else time.min, tzinfo=timezone.utc
            )
            return moment
        return ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Некорректный параметр {field}={value!r}: ожидается YYYY-MM-DD или ISO-8601",
        ) from exc


def _split_csv(value: Optional[str], field: str) -> Optional[List[str]]:
    if not value:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    if not items:
        return None
    return items


def _build_filter(
    species: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    bbox: Optional[str],
    lat: Optional[float],
    lon: Optional[float],
    radius_m: Optional[float],
    device_ids: Optional[str],
    min_confidence: Optional[float],
    include_duplicates: bool,
    limit: int,
    require_location: bool = True,
) -> DetectionFilter:
    registry = get_species_registry()
    species_list = _split_csv(species, "species")
    if species_list:
        unknown = registry.unknown_slugs(species_list)
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Неизвестные виды: {', '.join(unknown)} (см. /api/map/species)",
            )
    try:
        parsed_bbox = parse_bbox(bbox)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    if (lat is None) != (lon is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Радиусный фильтр требует обе координаты: lat и lon",
        )
    if lat is not None and lon is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Укажите lat, lon и radius_m вместе",
        )
    if lat is not None and lon is not None and (radius_m is None or radius_m <= 0):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Укажите положительный radius_m вместе с lat/lon",
        )
    if radius_m is not None and lat is None:
        radius_m = None
    return DetectionFilter(
        species=species_list,
        date_from=_parse_datetime(date_from, "from"),
        date_to=_parse_datetime(date_to, "to", end_of_day=True),
        bbox=parsed_bbox,
        center=(lat, lon) if lat is not None and lon is not None else None,
        radius_m=radius_m,
        device_ids=_split_csv(device_ids, "device_id"),
        min_confidence=min_confidence,
        include_duplicates=include_duplicates,
        require_location=require_location,
        limit=min(limit, MAX_SCAN_LIMIT),
    )


# ------------------------------------------------------------------ эндпоинты


@router.get("/species")
def read_species(
    session: Session = Depends(get_db), _: None = Depends(require_dashboard_auth)
) -> dict:
    """Справочник видов: русские названия, биотопы и число наблюдений за всё время."""
    registry = get_species_registry()
    catalog = registry.catalog()
    # Сводка по всем видам, включая те, у которых модель их ещё не распознаёт:
    # зачем экосистеме знать, что вид не определяется, если не слышно его пения?
    summary = {item["species_slug"]: item for item in species_summary(session)}
    for item in catalog:
        stats = summary.get(item["slug"], {})
        item["detections"] = int(stats.get("detections", 0))
        item["last_seen"] = stats.get("last_seen")
        item["mean_confidence"] = stats.get("mean_confidence")
    return {
        "count": len(catalog),
        "in_model": sum(1 for item in catalog if item["in_model"]),
        "in_model_classes": len(registry.model_classes),
        "observed": sum(1 for item in catalog if item["detections"] > 0),
        "habitats": registry.group_by_habitat(),
        "species": catalog,
    }


@router.get("/devices")
def read_map_devices(
    active_only: bool = Query(False),
    session: Session = Depends(get_db),
    _: None = Depends(require_dashboard_auth),
) -> JSONResponse:
    """Стационарные и мобильные микрофоны точками + статус активности для маркеров карты."""
    settings = get_settings()
    devices = list_devices(session, active_only=active_only)
    features = []
    for device in devices:
        status_data = device_status(device, settings.gap_alert_sec)
        lat, lon = device.lat, device.lon
        if lat is None or lon is None:
            # Мобильное устройство без последней фиксированной точки — точку не рисуем,
            # но карточка остаётся в списке (важно для отчёта по покрытию).
            features.append(
                {
                    "type": "Feature",
                    "geometry": None,
                    "properties": {**status_data, "located": False},
                }
            )
            continue
        features.append(point_feature(lat, lon, {**status_data, "located": True}))
    payload = feature_collection(features)
    payload["properties"] = {
        "gap_alert_sec": settings.gap_alert_sec,
        "devices_total": len(devices),
        "located": sum(1 for f in features if f["properties"].get("located")),
    }
    return JSONResponse(content=payload)


@router.get("/detections")
def read_detections(
    species: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    bbox: Optional[str] = Query(None),
    lat: Optional[float] = Query(None),
    lon: Optional[float] = Query(None),
    radius_m: Optional[float] = Query(None),
    device_id: Optional[str] = Query(None),
    min_confidence: Optional[float] = Query(None, ge=0.0, le=1.0),
    include_duplicates: bool = Query(False),
    limit: int = Query(20_000, ge=1, le=MAX_SCAN_LIMIT),
    session: Session = Depends(get_db),
    _: None = Depends(require_dashboard_auth),
) -> JSONResponse:
    """Подтверждённые детекции точками (GeoJSON) под фильтры вида/периода/географии."""
    registry = get_species_registry()
    flt = _build_filter(
        species, date_from, date_to, bbox, lat, lon, radius_m, device_id, min_confidence,
        include_duplicates, limit,
    )
    result = query_detections(session, flt)
    truncated = len(result.rows) >= min(limit, MAX_SCAN_LIMIT)
    features = [
        point_feature(
            point.lat,
            point.lon,
            {
                "id": point.id,
                "species_slug": point.species_slug,
                "species_ru": registry.name_ru(point.species_slug),
                "confidence": round(point.confidence, 4),
                "device_id": point.device_id,
                "window_start_ts": point.window_start_ts.isoformat(),
                "window_end_ts": point.window_end_ts.isoformat(),
            },
        )
        for point in result.points[:MAX_LEAFLET_POINTS]
    ]
    payload = feature_collection(features)
    payload["properties"] = {
        "count": len(features),
        "total_matched": result.stats.matched,
        "truncated": truncated,
        "took_ms": result.stats.took_ms,
        "used_postgis": result.stats.used_postgis,
        "notes": result.stats.notes,
        "filters": {
            "species": flt.species,
            "from": flt.date_from.isoformat() if flt.date_from else None,
            "to": flt.date_to.isoformat() if flt.date_to else None,
            "bbox": list(flt.bbox) if flt.bbox else None,
            "center": list(flt.center) if flt.center else None,
            "radius_m": flt.radius_m,
            "device_id": flt.device_ids,
            "min_confidence": flt.min_confidence,
            "include_duplicates": flt.include_duplicates,
        },
    }
    return JSONResponse(content=payload)


@router.get("/activity_zones")
def read_activity_zones(
    species: str = Query(..., description="Слаг одного вида (см. /api/map/species)"),
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    bbox: Optional[str] = Query(None),
    lat: Optional[float] = Query(None),
    lon: Optional[float] = Query(None),
    radius_m: Optional[float] = Query(None),
    device_id: Optional[str] = Query(None),
    min_confidence: Optional[float] = Query(None, ge=0.0, le=1.0),
    include_duplicates: bool = Query(False),
    limit: int = Query(20_000, ge=1, le=MAX_SCAN_LIMIT),
    precision: int = Query(DEFAULT_PRECISION, ge=3, le=10, description="Точность geohash-сетки"),
    level: str = Query("grid", description="grid | kde | both"),
    coverage_radius_m: Optional[float] = Query(
        DEFAULT_COVERAGE_RADIUS_M, description="Радиус покрытия микрофона для пометки ячеек; 0 — выключить"
    ),
    bandwidth_m: Optional[float] = Query(None, description="Ширина KDE в метрах (по умолчанию подбирается)"),
    session: Session = Depends(get_db),
    _: None = Depends(require_dashboard_auth),
) -> JSONResponse:
    """
    Зоны активности вида: сеточная тепловая карта и/или сглаженный контур с границей зоны.

    Требует конкретный вид: «зона активности» без указания вида не имеет смысла
    (иначе это просто «где что-то постучало»).
    """
    registry = get_species_registry()
    if species not in registry:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Неизвестный вид: {species} (см. /api/map/species)",
        )
    if level not in ("grid", "kde", "both"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="level должен быть grid, kde или both",
        )
    flt = _build_filter(
        species, date_from, date_to, bbox, lat, lon, radius_m, device_id, min_confidence,
        include_duplicates, limit,
    )
    devices = [d for d in list_devices(session, active_only=True) if d.lat is not None and d.lon is not None]

    layers: Dict[str, object] = {}
    if level in ("grid", "both"):
        report = grid_heatmap(
            session,
            flt,
            precision=precision,
            species_slug=species,
            coverage_radius_m=coverage_radius_m,
            active_devices=devices,
        )
        layers["grid"] = report.to_geojson()
        layers["grid_meta"] = report.meta
    if level in ("kde", "both"):
        report = kde_zones(
            session, flt, species_slug=species, bandwidth_m=bandwidth_m
        )
        layers["kde"] = report.to_geojson()
        layers["kde_meta"] = report.meta

    return JSONResponse(
        content={
            "species_slug": species,
            "species_ru": registry.name_ru(species),
            "level": level,
            "precision": precision,
            "layers": layers,
        }
    )


@router.get("/coverage")
def read_coverage(
    radius_m: float = Query(DEFAULT_COVERAGE_RADIUS_M, gt=0, le=50_000),
    precision: int = Query(DEFAULT_PRECISION, ge=3, le=10),
    include_inactive: bool = Query(False),
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    session: Session = Depends(get_db),
    _: None = Depends(require_dashboard_auth),
) -> JSONResponse:
    """
    Слой покрытия сети: где сервер способен услышать птиц.

    Без этого слоя пустая ячейкаheatmap неотличима от «птиц не было», что делает
    выводы экологов ошибочными.
    """
    report = coverage_layer(
        session,
        radius_m=radius_m,
        include_inactive=include_inactive,
        precision=precision,
        from_ts=_parse_datetime(date_from, "from"),
        to_ts=_parse_datetime(date_to, "to", end_of_day=True),
    )
    return JSONResponse(content={**report.to_geojson(), "properties": report.meta})


@router.get("/period_diff")
def read_period_diff(
    species: str = Query(...),
    a_from: str = Query(..., description="Период A: начало"),
    a_to: str = Query(..., description="Период A: конец"),
    b_from: str = Query(..., description="Период B: начало"),
    b_to: str = Query(..., description="Период B: конец"),
    precision: int = Query(DEFAULT_PRECISION, ge=3, le=10),
    min_count_delta: int = Query(1, ge=1, le=1000),
    normalize_by_devices: bool = Query(True, description="Сравнивать интенсивность, а не сырые счётчики"),
    session: Session = Depends(get_db),
    _: None = Depends(require_dashboard_auth),
) -> JSONResponse:
    """
    Режим «до / после»: ячейки, где вид появился или пропал за два периода.

    Отвечает на вопрос эколога «что изменилось после вмешательства/паводка».
    """
    registry = get_species_registry()
    if species not in registry:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Неизвестный вид: {species}",
        )
    period_a = (_parse_datetime(a_from, "a_from"), _parse_datetime(a_to, "a_to", True))
    period_b = (_parse_datetime(b_from, "b_from"), _parse_datetime(b_to, "b_to", True))
    if None in period_a or None in period_b:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Нужны все четыре границы периодов",
        )
    report = compare_periods(
        session,
        species,
        (period_a[0], period_a[1]),  # type: ignore[arg-type]
        (period_b[0], period_b[1]),  # type: ignore[arg-type]
        precision=precision,
        min_count_delta=min_count_delta,
        normalize_by_devices=normalize_by_devices,
    )
    return JSONResponse(content={**report.to_geojson(), "properties": report.meta})


@router.get("/hotspots")
def read_hotspots(
    species: Optional[str] = Query(None, description="Слаг вида (опционально — все виды)"),
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    bbox: Optional[str] = Query(None),
    eps_m: float = Query(500.0, gt=0, le=50_000),
    min_samples: int = Query(3, ge=2, le=1000),
    session: Session = Depends(get_db),
    _: None = Depends(require_dashboard_auth),
) -> JSONResponse:
    """P2: кластеры активности в пространстве «координаты × время»."""
    registry = get_species_registry()
    if species and species not in registry:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Неизвестный вид: {species}",
        )
    flt = _build_filter(
        species, date_from, date_to, bbox, None, None, None, None, None, False, 50_000
    )
    report = hotspots(session, flt, species_slug=species, eps_m=eps_m, min_samples=min_samples)
    return JSONResponse(content={**report.to_geojson(), "properties": report.meta})


@router.get("/summary")
def read_summary(
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    session: Session = Depends(get_db),
    _: None = Depends(require_dashboard_auth),
) -> dict:
    """Сводка для шапки дашборда: устройства, наблюдения, разрывы, последние данные."""
    settings = get_settings()
    registry = get_species_registry()
    # `require_location=False`: в шапке дашборда нужно «кого слышали», а не «где на карте».
    period = DetectionFilter(
        date_from=_parse_datetime(date_from, "from"),
        date_to=_parse_datetime(date_to, "to", end_of_day=True),
        require_location=False,
        limit=MAX_SCAN_LIMIT,
    )
    devices = list_devices(session)
    summary_species = species_summary(session, period)
    gaps = build_gap_report(session, notify=False)
    return {
        "generated_at": utcnow().isoformat(),
        "devices": {
            "total": len(devices),
            "active": sum(1 for d in devices if d.is_active),
            "with_location": sum(1 for d in devices if d.lat is not None and d.lon is not None),
            "states": _count_states(devices, settings.gap_alert_sec),
        },
        "detections": {
            "total": sum(item["detections"] for item in summary_species),
            "species_observed": len(summary_species),
            "top_species": [
                {**item, "species_ru": registry.name_ru(item["species_slug"])}
                for item in summary_species[:10]
            ],
        },
        "gaps": {
            "stale_devices": len(gaps.stale),
            "events_24h": len(gaps.gap_events),
            "latest_event": gaps.gap_events[0] if gaps.gap_events else None,
        },
        "queue": _queue_stats(),
    }


def _count_states(devices, gap_alert_sec: float) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for device in devices:
        state = device_status(device, gap_alert_sec)["status"]
        counts[state] = counts.get(state, 0) + 1
    return counts


def _queue_stats() -> dict:
    """Состояние оперативной очереди аудио (аудио не хранится, видно только загрузку)."""
    try:
        from .spool import get_spool

        return get_spool().stats()
    except Exception as exc:  # pragma: no cover - защита дашборда от сбоя очереди
        logger.warning("Не удалось получить статистику очереди: %s", exc)
        return {}


# ------------------------------------------------------------------ экспорт


@router.get("/export.csv")
def export_csv(
    species: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    bbox: Optional[str] = Query(None),
    lat: Optional[float] = Query(None),
    lon: Optional[float] = Query(None),
    radius_m: Optional[float] = Query(None),
    device_id: Optional[str] = Query(None),
    min_confidence: Optional[float] = Query(None, ge=0.0, le=1.0),
    include_duplicates: bool = Query(False),
    limit: int = Query(MAX_EXPORT_ROWS, ge=1, le=MAX_EXPORT_ROWS),
    session: Session = Depends(get_db),
    _: None = Depends(require_dashboard_auth),
) -> StreamingResponse:
    """Выгрузка наблюдений в CSV (без BOM — чтобы читалось в любом редакторе)."""
    registry = get_species_registry()
    flt = _build_filter(
        species, date_from, date_to, bbox, lat, lon, radius_m, device_id, min_confidence,
        include_duplicates, min(limit, MAX_EXPORT_ROWS), require_location=False,
    )
    result = query_detections(session, flt)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(
        [
            "species_slug",
            "species_ru",
            "confidence",
            "device_id",
            "lat",
            "lon",
            "window_start_ts",
            "window_end_ts",
            "votes",
            "windows_evaluated",
            "source",
            "duplicate_of_id",
        ]
    )
    # Идём по самим Detection-строкам: `result.points` отбрасывает записи без координат,
    # а в выгрузке нужны все (в т.ч. с пустым lat/lon — «птица слышна, где именно — неизвестно»).
    for row in result.rows:
        writer.writerow(
            [
                row.species_slug,
                registry.name_ru(row.species_slug),
                f"{row.confidence:.4f}",
                row.device_id,
                "" if row.lat is None else f"{row.lat:.6f}",
                "" if row.lon is None else f"{row.lon:.6f}",
                ensure_utc(row.window_start_ts).isoformat(),
                ensure_utc(row.window_end_ts).isoformat(),
                row.votes,
                row.windows_evaluated,
                row.source,
                "" if row.duplicate_of_id is None else row.duplicate_of_id,
            ]
        )
    payload = buffer.getvalue()
    return StreamingResponse(
        iter([payload]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="phoenix_detections.csv"'},
    )


@router.get("/export.geojson")
def export_geojson(
    species: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    bbox: Optional[str] = Query(None),
    lat: Optional[float] = Query(None),
    lon: Optional[float] = Query(None),
    radius_m: Optional[float] = Query(None),
    device_id: Optional[str] = Query(None),
    min_confidence: Optional[float] = Query(None, ge=0.0, le=1.0),
    include_duplicates: bool = Query(False),
    limit: int = Query(MAX_EXPORT_ROWS, ge=1, le=MAX_EXPORT_ROWS),
    session: Session = Depends(get_db),
    _: None = Depends(require_dashboard_auth),
) -> StreamingResponse:
    """Выгрузка в GeoJSON — открывается в QGIS/ArcGIS без конвертации."""
    registry = get_species_registry()
    flt = _build_filter(
        species, date_from, date_to, bbox, lat, lon, radius_m, device_id, min_confidence,
        include_duplicates, min(limit, MAX_EXPORT_ROWS), require_location=False,
    )
    result = query_detections(session, flt)
    located_ids = {point.id for point in result.points}
    features = []
    for row in result.rows:
        props = {
            "id": row.id,
            "species_slug": row.species_slug,
            "species_ru": registry.name_ru(row.species_slug),
            "confidence": round(row.confidence, 4),
            "device_id": row.device_id,
            "window_start_ts": ensure_utc(row.window_start_ts).isoformat(),
            "window_end_ts": ensure_utc(row.window_end_ts).isoformat(),
            "votes": row.votes,
            "windows_evaluated": row.windows_evaluated,
            "source": row.source,
            "model_version": row.model_version,
            "is_duplicate": bool(row.duplicate_of_id),
            "duplicate_of_id": row.duplicate_of_id,
        }
        if row.id in located_ids:
            features.append(point_feature(row.lat, row.lon, props))
        else:
            # Детекция без координат — валидная «непривязанная» фича GeoJSON
            # (geometry: null): так она не теряется в выгрузке, но и на карте не рисуется.
            features.append({"type": "Feature", "geometry": None, "properties": props})
    unlocated = sum(1 for f in features if f["geometry"] is None)
    payload = {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "generated_at": utcnow().isoformat(),
            "count": len(features),
            "located": len(features) - unlocated,
            "unlocated": unlocated,
            "source": "Phoenix_Bolotni server (автоматические предсказания модели, не экспертная разметка)",
        },
    }
    return StreamingResponse(
        iter([json.dumps(payload, ensure_ascii=False)]),
        media_type="application/geo+json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="phoenix_detections.geojson"'},
    )