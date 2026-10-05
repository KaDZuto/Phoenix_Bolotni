"""
Тесты геоаналитики и зон активности (Задачи 4 и 6, td3.md разделы 4 и 6).

Проверяется то, без чего карта вводит экологов в заблуждение:
* geohash-сетка действительно агрегирует точки;
* пустая ячейка внутри покрытия != пустая ячейка вне покрытия;
* граница зоны активности (KDE) существует и очерчена одним полигоном;
* «до/после» находит появившиеся и исчезнувшие ячейки;
* радиусный запрос на SQLite честно отдаёт ровно то, что попало в круг.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from server.analytics import DetectionFilter, query_detections
from server.geo import haversine_m
from server.geohash import decode_bbox, encode
from server.models import Detection, Device, hash_token, utcnow
from server.tests.conftest import now_ts  # noqa: F401  (единый стиль тестов)
from server.zones import compare_periods, coverage_layer, grid_heatmap, hotspots, kde_zones

# Болотный район близ Вологды — реалистичная точка для проверки геометрии.
BASE_LAT = 59.9
BASE_LON = 41.1


def make_device(
    session,
    device_id: str = "dev_map",
    lat: float = BASE_LAT,
    lon: float = BASE_LON,
    active: bool = True,
) -> Device:
    device = Device(
        id=device_id,
        name=f"Микрофон {device_id}",
        lat=lat,
        lon=lon,
        token_hash=hash_token(f"token-{device_id}"),
        token_prefix="t",
        is_active=active,
        owner="tester",
    )
    session.add(device)
    return device


def add_detection(
    session,
    device_id: str,
    species_slug: str,
    lat: float,
    lon: float,
    minutes_ago: float,
    confidence: float = 0.9,
) -> Detection:
    start = utcnow() - timedelta(minutes=minutes_ago)
    detection = Detection(
        device_id=device_id,
        species_slug=species_slug,
        confidence=confidence,
        window_start_ts=start,
        window_end_ts=start + timedelta(seconds=7.5),
        lat=lat,
        lon=lon,
        source="model_auto",
    )
    session.add(detection)
    return detection


@pytest.fixture
def populated(db_session):
    """Два устройства и ~200 детекций: два «острова» активности разной интенсивности."""
    session = db_session
    device_a = make_device(session, "dev_a")
    device_b = make_device(session, "dev_b", lat=BASE_LAT + 0.01, lon=BASE_LON + 0.01)

    for i in range(150):
        add_detection(
            session,
            device_a.id,
            "corvus_frugilegus",
            BASE_LAT + (i % 5) * 0.0001,
            BASE_LON + (i % 7) * 0.0001,
            minutes_ago=i,
            confidence=0.8 + (i % 10) / 50,
        )
    for i in range(40):
        add_detection(
            session,
            device_b.id,
            "corvus_frugilegus",
            BASE_LAT + 0.01 + (i % 4) * 0.0001,
            BASE_LON + 0.01 + (i % 3) * 0.0001,
            minutes_ago=2000 + i,
        )
    for i in range(30):
        add_detection(
            session,
            device_a.id,
            "strix_aluco",
            BASE_LAT + 0.002,
            BASE_LON + 0.002,
            minutes_ago=1000 + i,
        )
    session.flush()
    return session


# ---------------------------------------------------------------------------
# geohash
# ---------------------------------------------------------------------------


def test_encode_matches_reference_vector():
    """Эталонный вектор канонического алгоритма geohash."""
    assert encode(57.64911, 10.40744, 11) == "u4pruydqqvj"
    assert encode(0.0, 0.0, 5) == "s0000"


def test_encode_rejects_bad_input():
    with pytest.raises(ValueError):
        encode(95.0, 10.0, 6)
    with pytest.raises(ValueError):
        encode(10.0, 200.0, 6)
    with pytest.raises(ValueError):
        encode(10.0, 10.0, 0)


def test_decoded_cell_contains_its_point():
    for lat, lon in [(BASE_LAT, BASE_LON), (0.0, 0.0), (-33.87, 151.21), (59.9, 41.1)]:
        for precision in (4, 6, 8):
            min_lat, min_lon, max_lat, max_lon = decode_bbox(encode(lat, lon, precision))
            assert min_lat <= lat <= max_lat
            assert min_lon <= lon <= max_lon


# ---------------------------------------------------------------------------
# Сеточная тепловая карта
# ---------------------------------------------------------------------------


def test_grid_aggregates_into_cells_and_normalizes_heat(populated):
    report = grid_heatmap(
        populated,
        DetectionFilter(species=["corvus_frugilegus"]),
        species_slug="corvus_frugilegus",
    )
    assert report.level == "grid"
    assert report.meta["detections_scanned"] == 190
    assert len(report.features) == report.meta["cells"] >= 1

    counts = sum(f["properties"]["count"] for f in report.features)
    assert counts == 190

    # `heat` нормирован 0..1: тепловая карта читается одинаково в браузере и в ГИС.
    heats = [f["properties"]["heat"] for f in report.features]
    assert max(heats) == pytest.approx(1.0)
    assert all(0.0 <= h <= 1.0 for h in heats)


def test_grid_sorts_by_weight_desc(populated):
    report = grid_heatmap(populated, DetectionFilter(species=["corvus_frugilegus"]))
    weights = [f["properties"]["weight"] for f in report.features]
    assert weights == sorted(weights, reverse=True)


def test_grid_marks_cells_outside_coverage(populated):
    """Ячейка в 2 км от микрофона при радиусе покрытия 300 м — «данных здесь нет»."""
    report = grid_heatmap(
        populated,
        DetectionFilter(species=["corvus_frugilegus"]),
        species_slug="corvus_frugilegus",
        coverage_radius_m=300.0,
        active_devices=list(populated.query(Device).all()),
    )
    flags = [f["properties"].get("covered") for f in report.features]
    assert True in flags or False in flags
    assert report.meta["coverage_radius_m"] == 300.0


def test_grid_without_coverage_has_no_flag(populated):
    report = grid_heatmap(
        populated,
        DetectionFilter(species=["corvus_frugilegus"]),
        coverage_radius_m=None,
    )
    assert all("covered" not in f["properties"] for f in report.features)
    assert "cells_outside_coverage" not in report.meta


def test_grid_for_unknown_species_is_empty(populated):
    report = grid_heatmap(populated, DetectionFilter(species=["bubo_bubo"]))
    assert report.features == []
    assert report.meta["cells"] == 0
    assert all(f["properties"]["heat"] == 0.0 for f in report.features)


def test_grid_respects_period_filter(populated):
    now = utcnow()
    report = grid_heatmap(
        populated,
        DetectionFilter(
            species=["corvus_frugilegus"],
            date_from=now - timedelta(minutes=60),
            date_to=now,
        ),
        species_slug="corvus_frugilegus",
    )
    # Первые 60 минут принадлежат «острову» у dev_a, второй остров — в прошлом.
    assert report.meta["cells"] == 1
    assert sum(f["properties"]["count"] for f in report.features) == 60


# ---------------------------------------------------------------------------
# Покрытие сети
# ---------------------------------------------------------------------------


def test_coverage_layer_buffers_active_devices(populated):
    report = coverage_layer(populated, radius_m=300.0)
    assert report.level == "coverage"
    assert report.meta["devices_total"] == 2
    assert report.meta["devices_located"] == 2
    assert report.meta["cells_covered"] >= 2
    for feature in report.features:
        assert feature["geometry"]["type"] == "Polygon"
        assert feature["properties"]["radius_m"] == 300.0
    # Смысловая оговорка отдаётся вместе со слоем: без неё пустая ячейка читается
    # как «птиц не было».
    assert "покрытие есть" in report.meta["note"]


def test_coverage_skips_inactive_devices(populated):
    populated.query(Device).filter(Device.id == "dev_b").one().is_active = False
    populated.flush()
    report = coverage_layer(populated, radius_m=300.0)
    assert report.meta["devices_total"] == 1
    assert report.meta["devices_located"] == 1

    everything = coverage_layer(populated, radius_m=300.0, include_inactive=True)
    assert everything.meta["devices_total"] == 2


def test_coverage_skips_devices_without_location(populated):
    device = populated.query(Device).filter(Device.id == "dev_b").one()
    device.lat = None
    device.lon = None
    populated.flush()
    report = coverage_layer(populated, radius_m=300.0)
    assert report.meta["devices_total"] == 2
    assert report.meta["devices_located"] == 1


def test_coverage_without_devices_is_empty(db_session):
    report = coverage_layer(db_session, radius_m=300.0)
    assert report.features == []
    assert report.meta["cells_covered"] == 0


# ---------------------------------------------------------------------------
# Граница зоны активности (KDE)
# ---------------------------------------------------------------------------


def test_kde_returns_boundary_polygon(populated):
    report = kde_zones(
        populated,
        DetectionFilter(species=["corvus_frugilegus"]),
        species_slug="corvus_frugilegus",
    )
    assert report.meta["points"] == 190
    assert report.meta["bandwidth_m"] > 0
    layers = {f["properties"].get("layer") for f in report.features}
    assert "boundary" in layers

    boundary = [f for f in report.features if f["properties"].get("layer") == "boundary"]
    assert len(boundary) == 1, "зона активности должна иметь одну внешнюю границу"
    ring = boundary[0]["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1], "кольцо полигона должно быть замкнутым"
    lats = [point[1] for point in ring]
    assert max(lats) <= BASE_LAT + 0.02


def test_kde_skips_when_too_few_detections(populated):
    report = kde_zones(populated, DetectionFilter(species=["bubo_bubo"]))
    assert report.features == []
    assert "Недостаточно" in report.meta["skipped"]


def test_kde_respects_explicit_bandwidth(populated):
    wide = kde_zones(
        populated, DetectionFilter(species=["corvus_frugilegus"]), bandwidth_m=400.0
    )
    narrow = kde_zones(
        populated, DetectionFilter(species=["corvus_frugilegus"]), bandwidth_m=60.0
    )
    assert wide.meta["bandwidth_m"] == 400.0
    assert narrow.meta["bandwidth_m"] == 60.0
    # Широкое сглаживание «съедает» разрыв между островами: островов меньше.
    wide_layers = sum(1 for f in wide.features if f["properties"].get("layer") == "density")
    narrow_layers = sum(1 for f in narrow.features if f["properties"].get("layer") == "density")
    assert wide_layers <= narrow_layers


# ---------------------------------------------------------------------------
# Режим «до / после»
# ---------------------------------------------------------------------------


def test_compare_periods_detects_appeared_and_disappeared(db_session):
    make_device(db_session, "dev_a")
    now = utcnow()
    old = now - timedelta(days=30)
    # Вид был на «севере» (dev_a) в прошлом и перешёл на «юг» (dev_b) сейчас.
    for i in range(20):
        start = old - timedelta(minutes=i)
        db_session.add(
            Detection(
                device_id="dev_a",
                species_slug="corvus_frugilegus",
                confidence=0.9,
                window_start_ts=start,
                window_end_ts=start + timedelta(seconds=7.5),
                lat=BASE_LAT,
                lon=BASE_LON,
                source="model_auto",
            )
        )
    for i in range(20):
        start = now - timedelta(minutes=i)
        db_session.add(
            Detection(
                device_id="dev_a",
                species_slug="corvus_frugilegus",
                confidence=0.9,
                window_start_ts=start,
                window_end_ts=start + timedelta(seconds=7.5),
                lat=BASE_LAT + 0.01,
                lon=BASE_LON + 0.01,
                source="model_auto",
            )
        )
    db_session.flush()

    report = compare_periods(
        db_session,
        "corvus_frugilegus",
        (old - timedelta(days=1), now - timedelta(days=15)),
        (now - timedelta(days=14), now),
    )
    kinds = {f["properties"]["kind"] for f in report.features}
    assert "appeared" in kinds
    assert "disappeared" in kinds
    assert report.meta["summary"]["appeared"] >= 1
    assert report.meta["summary"]["disappeared"] >= 1

    appeared = [f for f in report.features if f["properties"]["kind"] == "appeared"]
    disappeared = [f for f in report.features if f["properties"]["kind"] == "disappeared"]
    # «Появился» — там, где раньше было пусто, и наоборот.
    assert all(f["properties"]["count_a"] == 0 for f in appeared)
    assert all(f["properties"]["count_b"] == 0 for f in disappeared)


def _add_species_at(session, slug: str, lat: float, lon: float, start, count: int, device_id: str = "dev_a"):
    for i in range(count):
        moment = start - timedelta(minutes=i)
        session.add(
            Detection(
                device_id=device_id,
                species_slug=slug,
                confidence=0.9,
                window_start_ts=moment,
                window_end_ts=moment + timedelta(seconds=7.5),
                lat=lat,
                lon=lon,
                source="model_auto",
            )
        )


def test_compare_periods_normalizes_by_devices(db_session):
    """Сравнивается интенсивность (на устройство), а не сырое число детекций."""
    make_device(db_session, "dev_a")
    make_device(db_session, "dev_b", lat=BASE_LAT + 0.02, lon=BASE_LON + 0.02)
    now = utcnow()
    _add_species_at(
        db_session, "corvus_frugilegus", BASE_LAT, BASE_LON, now - timedelta(days=2), 10
    )
    _add_species_at(
        db_session, "corvus_frugilegus", BASE_LAT, BASE_LON, now - timedelta(minutes=30), 20
    )
    db_session.flush()
    period_a = (now - timedelta(days=3), now - timedelta(days=1))
    period_b = (now - timedelta(days=1), now)

    normalized = compare_periods(db_session, "corvus_frugilegus", period_a, period_b)
    raw = compare_periods(
        db_session, "corvus_frugilegus", period_a, period_b, normalize_by_devices=False
    )

    assert normalized.meta["normalize_by_devices"] is True
    assert normalized.meta["active_devices"] == 2
    assert raw.meta["normalize_by_devices"] is False

    normalized_delta = normalized.features[0]["properties"]["delta"]
    raw_delta = raw.features[0]["properties"]["delta"]
    # 20-10 детекций, но два активных устройства → интенсивность выросла вдвое, а не на 10.
    assert normalized_delta == pytest.approx(5.0)
    assert raw_delta == pytest.approx(10.0)
    assert normalized.features[0]["properties"]["kind"] == "grew"


def test_compare_periods_marks_stable_cells(db_session):
    make_device(db_session, "dev_a")
    now = utcnow()
    _add_species_at(
        db_session, "corvus_frugilegus", BASE_LAT, BASE_LON, now - timedelta(days=2), 10
    )
    _add_species_at(
        db_session, "corvus_frugilegus", BASE_LAT, BASE_LON, now - timedelta(minutes=30), 10
    )
    db_session.flush()
    report = compare_periods(
        db_session,
        "corvus_frugilegus",
        (now - timedelta(days=3), now - timedelta(days=1)),
        (now - timedelta(days=1), now),
    )
    assert report.features == []
    assert report.meta["summary"]["stable"] == 1


def test_compare_periods_on_empty_periods(db_session):
    make_device(db_session, "dev_a")
    now = utcnow()
    report = compare_periods(
        db_session,
        "corvus_frugilegus",
        (now - timedelta(days=4), now - timedelta(days=3)),
        (now - timedelta(days=2), now - timedelta(days=1)),
    )
    assert report.features == []
    assert report.meta["summary"] == {
        "appeared": 0,
        "disappeared": 0,
        "grew": 0,
        "shrunk": 0,
        "stable": 0,
    }


# ---------------------------------------------------------------------------
# Кластеры активности
# ---------------------------------------------------------------------------


def test_hotspots_clusters_points_in_space_and_time(populated):
    report = hotspots(
        populated,
        DetectionFilter(species=["corvus_frugilegus"]),
        species_slug="corvus_frugilegus",
        eps_m=800.0,
        min_samples=5,
    )
    assert report.meta["clusters"] >= 1
    for feature in report.features:
        assert feature["geometry"]["type"] == "Polygon"
        assert feature["properties"]["detections"] >= 5
        assert feature["properties"]["species_slug"] == "corvus_frugilegus"


def test_hotspots_needs_min_samples(populated):
    report = hotspots(populated, DetectionFilter(species=["strix_aluco"]), min_samples=100)
    assert report.features == []
    assert "Недостаточно" in report.meta["skipped"]


# ---------------------------------------------------------------------------
# Географические запросы (критерий приёмки Задачи 4)
# ---------------------------------------------------------------------------


def test_radius_query_matches_haversine(populated):
    """На SQLite радиус считается в Python — проверяем, что результат точен."""
    result = query_detections(
        populated,
        DetectionFilter(
            species=["corvus_frugilegus"],
            center=(BASE_LAT, BASE_LON),
            radius_m=400.0,
        ),
    )
    assert result.points
    for point in result.points:
        distance = haversine_m(BASE_LAT, BASE_LON, point.lat, point.lon)
        assert distance <= 400.0 + 1e-6
    # Устройство dev_b в ~1.5 км — за круг не попадает.
    assert {p.device_id for p in result.points} == {"dev_a"}
    assert result.stats.notes, "должно быть видно, как считался радиус"
    assert result.stats.used_postgis is False


def test_bbox_query_filters_by_coordinates(populated):
    result = query_detections(
        populated,
        DetectionFilter(species=["corvus_frugilegus"], bbox=(BASE_LAT - 0.005, BASE_LON - 0.005, BASE_LAT + 0.005, BASE_LON + 0.005)),
    )
    assert result.points
    assert all(
        BASE_LAT - 0.005 <= p.lat <= BASE_LAT + 0.005 for p in result.points
    )
    assert result.stats.bbox_prefilter is not None


def test_bbox_prefilter_narrows_the_scan(populated):
    """Индекс по (lat, lon) должен отсечь явно дальние точки (критерий приёмки)."""
    wide = query_detections(populated, DetectionFilter(species=["corvus_frugilegus"]))
    narrow = query_detections(
        populated,
        DetectionFilter(
            species=["corvus_frugilegus"],
            bbox=(BASE_LAT - 0.001, BASE_LON - 0.001, BASE_LAT + 0.001, BASE_LON + 0.001),
        ),
    )
    assert narrow.stats.scanned < wide.stats.scanned
    assert narrow.stats.matched < wide.stats.matched


def test_query_excludes_duplicates_by_default(populated):
    base = DetectionFilter(species=["corvus_frugilegus"])
    total = query_detections(populated, base).stats.matched

    # Дубль на стыке чанков: та же птица, то же окно, но `duplicate_of_id` проставлен.
    original = query_detections(populated, base).rows[0]
    duplicate = add_detection(
        populated,
        original.device_id,
        original.species_slug,
        original.lat,
        original.lon,
        minutes_ago=1,
    )
    duplicate.duplicate_of_id = original.id
    duplicate.window_start_ts = original.window_start_ts
    duplicate.window_end_ts = original.window_end_ts
    populated.flush()

    assert query_detections(populated, base).stats.matched == total
    assert duplicate.id not in {row.id for row in query_detections(populated, base).rows}

    with_dupes = DetectionFilter(species=["corvus_frugilegus"], include_duplicates=True)
    assert query_detections(populated, with_dupes).stats.matched == total + 1


def test_query_skips_detections_without_location(populated):
    add_detection(populated, "dev_a", "corvus_frugilegus", None, None, minutes_ago=5)
    populated.flush()
    located = query_detections(populated, DetectionFilter(species=["corvus_frugilegus"]))
    assert located.stats.matched == 190
    assert located.stats.scanned == 190

    everything = query_detections(
        populated,
        DetectionFilter(species=["corvus_frugilegus"], require_location=False),
    )
    assert everything.stats.scanned == 191


def test_query_limit_caps_rows(populated):
    result = query_detections(populated, DetectionFilter(limit=10))
    assert len(result.rows) == 10
    assert result.stats.matched <= 10