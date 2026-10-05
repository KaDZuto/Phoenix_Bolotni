"""
Тесты API карты активности (Задача 5, td3.md раздел 5) и доступа к дашборду.

Карта — единственный интерфейс, через который смотрят данные, поэтому проверяется
и формат ответов (GeoJSON, который понимают ГИС), и честность сообщений об ошибках:
лучше явный `422`, чем молча пустая карта.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import timedelta

import pytest

from server.db import session_scope
from server.ingest import app
from server.models import Detection, Device, hash_token, utcnow

BASE_LAT = 59.9
BASE_LON = 41.1

SPECIES = "corvus_frugilegus"
OTHER_SPECIES = "strix_aluco"


@pytest.fixture
def seeded(db_session):
    """Два устройства + детекции двух видов, часть — вне покрытия и без координат."""
    session = db_session
    for device_id, lat, lon in (
        ("dev_a", BASE_LAT, BASE_LON),
        ("dev_b", BASE_LAT + 0.01, BASE_LON + 0.01),
    ):
        session.add(
            Device(
                id=device_id,
                name=f"Микрофон {device_id}",
                lat=lat,
                lon=lon,
                token_hash=hash_token(f"token-{device_id}"),
                token_prefix="t",
                is_active=True,
                owner="tester",
            )
        )
    now = utcnow()
    for i in range(60):
        start = now - timedelta(minutes=i)
        session.add(
            Detection(
                device_id="dev_a",
                species_slug=SPECIES,
                confidence=0.85,
                window_start_ts=start,
                window_end_ts=start + timedelta(seconds=7.5),
                lat=BASE_LAT + (i % 5) * 0.0001,
                lon=BASE_LON + (i % 5) * 0.0001,
                votes=2,
                windows_evaluated=3,
                source="model_auto",
            )
        )
    for i in range(25):
        start = now - timedelta(minutes=1000 + i)
        session.add(
            Detection(
                device_id="dev_b",
                species_slug=SPECIES,
                confidence=0.9,
                window_start_ts=start,
                window_end_ts=start + timedelta(seconds=7.5),
                lat=BASE_LAT + 0.01,
                lon=BASE_LON + 0.01,
                source="model_auto",
            )
        )
    for i in range(10):
        start = now - timedelta(minutes=1000 + i)
        session.add(
            Detection(
                device_id="dev_a",
                species_slug=OTHER_SPECIES,
                confidence=0.7,
                window_start_ts=start,
                window_end_ts=start + timedelta(seconds=7.5),
                lat=None,
                lon=None,
                source="model_auto",
            )
        )
    # Коммит: API-эндпоинты читают БД через собственные сессии.
    session.commit()
    session.expire_all()
    return session


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Справочник видов и устройства
# ---------------------------------------------------------------------------


def test_species_catalog_carries_russian_names_and_counts(client, seeded):
    response = client.get("/api/map/species")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] >= 100, "в whitelist репозитория модели порядка сотни видов"
    slugs = {item["slug"] for item in body["species"]}
    assert SPECIES in slugs
    entry = next(item for item in body["species"] if item["slug"] == SPECIES)
    assert entry["ru"] == "Грач"
    assert entry["habitat_ru"]
    assert entry["detections"] == 85
    assert set(body["habitats"]) <= {"wetland", "forest", "field_steppe", "urban", "other"}


def test_map_devices_returns_geojson_with_statuses(client, seeded):
    response = client.get("/api/map/devices")
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "FeatureCollection"
    assert body["properties"]["devices_total"] == 2
    assert body["properties"]["located"] == 2
    statuses = {f["properties"]["status"] for f in body["features"]}
    assert statuses <= {"online", "silent", "offline", "never_seen"}


def test_map_devices_reports_unlocated_device(client, seeded):
    device = seeded.query(Device).filter(Device.id == "dev_b").one()
    device.lat = None
    device.lon = None
    seeded.commit()
    body = client.get("/api/map/devices").json()
    assert body["properties"]["located"] == 1
    unlocated = [f for f in body["features"] if f["geometry"] is None]
    assert len(unlocated) == 1
    assert unlocated[0]["properties"]["located"] is False


# ---------------------------------------------------------------------------
# Детекции
# ---------------------------------------------------------------------------


def test_detections_geojson_filters_by_species(client, seeded):
    body = client.get(f"/api/map/detections?species={SPECIES}").json()
    assert body["type"] == "FeatureCollection"
    assert body["properties"]["count"] == 85
    assert {f["properties"]["species_slug"] for f in body["features"]} == {SPECIES}
    assert body["features"][0]["properties"]["species_ru"] == "Грач"


def test_detections_accept_multiple_species(client, seeded):
    body = client.get(f"/api/map/detections?species={SPECIES},{OTHER_SPECIES}").json()
    assert body["properties"]["total_matched"] == 85  # 10 без координат не рисуются


def test_detections_bbox_and_radius(client, seeded):
    bbox = client.get(
        f"/api/map/detections?species={SPECIES}&bbox={BASE_LON - 0.001},{BASE_LAT - 0.001},"
        f"{BASE_LON + 0.001},{BASE_LAT + 0.001}"
    ).json()
    assert bbox["properties"]["count"] == 60
    # GeoJSON: координаты идут как [долгота, широта].
    for feature in bbox["features"]:
        lon, lat = feature["geometry"]["coordinates"]
        assert BASE_LAT - 0.001 <= lat <= BASE_LAT + 0.001
        assert BASE_LON - 0.001 <= lon <= BASE_LON + 0.001

    radius = client.get(
        f"/api/map/detections?species={SPECIES}&lat={BASE_LAT}&lon={BASE_LON}&radius_m=500"
    ).json()
    assert radius["properties"]["count"] == 60
    assert radius["properties"]["filters"]["radius_m"] == 500


def test_detections_period_filter_with_plain_dates(client, seeded):
    today = utcnow().date().isoformat()
    body = client.get(f"/api/map/detections?species={SPECIES}&from={today}&to={today}").json()
    assert body["properties"]["count"] == 60


def test_detections_truncation_is_reported(client, seeded):
    body = client.get(f"/api/map/detections?species={SPECIES}&limit=10").json()
    assert body["properties"]["count"] == 10
    assert body["properties"]["truncated"] is True


def test_detections_reports_query_diagnostics(client, seeded):
    body = client.get(f"/api/map/detections?species={SPECIES}").json()
    props = body["properties"]
    assert props["took_ms"] >= 0
    assert "used_postgis" in props
    assert isinstance(props["notes"], list)


# ---------------------------------------------------------------------------
# Зоны активности, покрытие, периоды
# ---------------------------------------------------------------------------


def test_activity_zones_returns_grid_and_kde(client, seeded):
    body = client.get(f"/api/map/activity_zones?species={SPECIES}&level=both").json()
    assert body["species_slug"] == SPECIES
    assert body["species_ru"] == "Грач"
    assert body["layers"]["grid_meta"]["cells"] >= 1
    assert body["layers"]["grid"]["features"][0]["properties"]["heat"] == 1.0
    assert "kde_meta" in body["layers"]


def test_activity_zones_mark_coverage_gaps(client, seeded):
    """
    Ячейка вне зоны покрытия = «данных здесь просто нет», и это должно быть видно.

    Точность 8 (≈19 м) вместо дефолтной 6 нужна потому, что при 6 ячейка шире
    радиуса покрытия микрофона и флаг становится почти всегда `True`.
    """
    # Детекция в 5 км от обоих микрофонов: слышно, но покрытия там нет.
    start = utcnow() - timedelta(minutes=10)
    seeded.add(
        Detection(
            device_id="dev_a",
            species_slug=SPECIES,
            confidence=0.95,
            window_start_ts=start,
            window_end_ts=start + timedelta(seconds=7.5),
            lat=BASE_LAT + 0.05,
            lon=BASE_LON + 0.05,
            source="model_auto",
        )
    )
    seeded.commit()

    body = client.get(
        f"/api/map/activity_zones?species={SPECIES}&coverage_radius_m=300&precision=8"
    ).json()
    grid = body["layers"]["grid"]
    covered = [f["properties"]["covered"] for f in grid["features"]]
    assert False in covered, "ожидаем ячейки вне покрытия"
    assert True in covered, "ожидаем и ячейки внутри покрытия"
    assert body["layers"]["grid_meta"]["cells_outside_coverage"] == covered.count(False)


def test_coverage_flag_meaningless_at_coarse_precision(client, seeded):
    """
    Ячейка геohash-6 (~1.2 км) шире радиуса покрытия 300 м — сравнение «центр ячейки
    внутри круга» почти всегда даёт `covered=True`, поэтому это должно быть видно
    в метаданных, а не молча вводить в заблуждение.
    """
    body = client.get(
        f"/api/map/activity_zones?species={SPECIES}&coverage_radius_m=300"
    ).json()
    meta = body["layers"]["grid_meta"]
    assert meta["precision"] == 6
    assert meta["cells"] == 2
    assert meta["cells_outside_coverage"] == 0
    assert meta["coverage_check_granular"] is False
    assert "крупнее" in meta["coverage_check_note"]
    assert meta["coverage_cell_m"][0] > 300


def test_activity_zones_fine_precision_is_granular(client, seeded):
    body = client.get(
        f"/api/map/activity_zones?species={SPECIES}&coverage_radius_m=300&precision=8"
    ).json()
    grid_meta = body["layers"]["grid_meta"]
    assert grid_meta["coverage_check_granular"] is True
    assert "coverage_check_note" not in grid_meta


def test_activity_zones_without_coverage(client, seeded):
    body = client.get(f"/api/map/activity_zones?species={SPECIES}&coverage_radius_m=0").json()
    assert all("covered" not in f["properties"] for f in body["layers"]["grid"]["features"])


def test_activity_zones_requires_single_species(client, seeded):
    """Несколько видов в одной «зоне активности» бессмысленны."""
    response = client.get(
        f"/api/map/activity_zones?species={SPECIES},{OTHER_SPECIES}"
    )
    assert response.status_code == 422


def test_coverage_layer_endpoint(client, seeded):
    body = client.get("/api/map/coverage?radius_m=250").json()
    assert body["properties"]["devices_located"] == 2
    assert body["properties"]["radius_m"] == 250
    assert all(f["geometry"]["type"] == "Polygon" for f in body["features"])


def test_period_diff_endpoint(client, seeded):
    today = utcnow().date()
    response = client.get(
        f"/api/map/period_diff?species={SPECIES}"
        f"&a_from={(today - timedelta(days=14)).isoformat()}&a_to={(today - timedelta(days=7)).isoformat()}"
        f"&b_from={(today - timedelta(days=7)).isoformat()}&b_to={today.isoformat()}"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["properties"]["species_slug"] == SPECIES
    assert "summary" in body["properties"]
    for feature in body["features"]:
        assert feature["properties"]["kind"] in {
            "appeared",
            "disappeared",
            "grew",
            "shrunk",
        }


def test_hotspots_endpoint(client, seeded):
    body = client.get(f"/api/map/hotspots?species={SPECIES}&min_samples=3").json()
    assert body["properties"]["clusters"] >= 1
    assert body["features"][0]["properties"]["species_slug"] == SPECIES


def test_summary_endpoint(client, seeded):
    body = client.get("/api/map/summary").json()
    assert body["devices"]["total"] == 2
    assert body["devices"]["active"] == 2
    assert body["devices"]["with_location"] == 2
    assert body["detections"]["species_observed"] == 2
    assert body["generated_at"]
    top = {item["species_slug"]: item for item in body["detections"]["top_species"]}
    assert top[SPECIES]["species_ru"] == "Грач"
    assert top[SPECIES]["detections"] == 85


# ---------------------------------------------------------------------------
# Выгрузка
# ---------------------------------------------------------------------------


def test_export_csv_is_parseable(client, seeded):
    response = client.get(f"/api/map/export.csv?species={SPECIES}")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]

    rows = list(csv.DictReader(io.StringIO(response.text), delimiter=";"))
    assert len(rows) == 85
    assert rows[0]["species_slug"] == SPECIES
    assert rows[0]["species_ru"] == "Грач"
    assert 55.0 < float(rows[0]["lat"]) < 60.0
    assert rows[0]["window_start_ts"].endswith("+00:00")


def test_export_csv_keeps_detections_without_coordinates(client, seeded):
    """«Птица слышна, где именно — неизвестно» не должно теряться при выгрузке."""
    rows = list(
        csv.DictReader(
            io.StringIO(client.get(f"/api/map/export.csv?species={OTHER_SPECIES}").text),
            delimiter=";",
        )
    )
    assert len(rows) == 10
    assert all(row["lat"] == "" and row["lon"] == "" for row in rows)


def test_export_geojson_is_valid_feature_collection(client, seeded):
    response = client.get(f"/api/map/export.geojson?species={SPECIES}")
    assert response.status_code == 200
    assert "geo+json" in response.headers["content-type"]
    body = json.loads(response.text)
    assert body["type"] == "FeatureCollection"
    assert body["properties"]["count"] == 85
    assert body["properties"]["located"] == 85
    assert "не экспертная разметка" in body["properties"]["source"]
    feature = body["features"][0]
    assert feature["geometry"]["type"] == "Point"
    assert len(feature["geometry"]["coordinates"]) == 2


def test_export_geojson_marks_unlocated_features(client, seeded):
    body = json.loads(client.get(f"/api/map/export.geojson?species={OTHER_SPECIES}").text)
    assert body["properties"]["unlocated"] == 10
    assert all(f["geometry"] is None for f in body["features"])


def test_export_respects_period_filter(client, seeded):
    today = utcnow().date().isoformat()
    body = json.loads(
        client.get(
            f"/api/map/export.geojson?species={SPECIES}&from={today}&to={today}"
        ).text
    )
    assert body["properties"]["count"] == 60


# ---------------------------------------------------------------------------
# Ошибки запроса — лучше явный 422, чем молча пустая карта
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "species=not_a_real_bird",
        "from=yesterday",
        "to=2026-13-45",
        "bbox=1,2,3",
        "bbox=200,10,300,20",
        "lat=59.9&radius_m=100",
        "lon=41.1&radius_m=100",
        "lat=59.9&lon=41.1",
        "lat=59.9&lon=41.1&radius_m=0",
    ],
)
def test_bad_filters_return_422(client, seeded, query):
    response = client.get(f"/api/map/detections?{query}")
    assert response.status_code == 422, f"{query} должен отклоняться с 422"
    assert response.json()["detail"]


def test_zones_reject_unknown_species_and_level(client, seeded):
    assert client.get("/api/map/activity_zones?species=nope").status_code == 422
    assert (
        client.get(f"/api/map/activity_zones?species={SPECIES}&level=zzz").status_code
        == 422
    )
    assert client.get("/api/map/activity_zones").status_code == 422


def test_period_diff_requires_four_dates(client, seeded):
    today = utcnow().date().isoformat()
    response = client.get(
        f"/api/map/period_diff?species={SPECIES}&a_from={today}&a_to={today}&b_from={today}"
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Дашборд и авторизация
# ---------------------------------------------------------------------------


def test_dashboard_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "leaflet" in response.text.lower()


def test_dashboard_static_assets_are_served(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200


def test_openapi_lists_map_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path in (
        "/api/map/species",
        "/api/map/devices",
        "/api/map/detections",
        "/api/map/activity_zones",
        "/api/map/coverage",
        "/api/map/period_diff",
        "/api/map/summary",
        "/api/map/export.csv",
        "/api/map/export.geojson",
    ):
        assert path in paths, f"{path} должен быть в OpenAPI"


def test_map_api_requires_auth_when_enabled(monkeypatch, seeded):
    """С `PHOENIX_DASHBOARD_AUTH=true` карта не должна быть открытой всему интернету."""
    from fastapi.testclient import TestClient

    from server import config as config_module

    monkeypatch.setenv("PHOENIX_DASHBOARD_AUTH", "true")
    monkeypatch.setenv("PHOENIX_DASHBOARD_USER", "ecologist")
    monkeypatch.setenv("PHOENIX_DASHBOARD_PASSWORD", "s3cret")
    config_module.reload_settings()
    try:
        with TestClient(app) as protected:
            assert protected.get("/api/map/detections").status_code == 401
            assert protected.get("/").status_code == 401

            ok = protected.get(
                "/api/map/detections", auth=("ecologist", "s3cret")
            )
            assert ok.status_code == 200

            wrong = protected.get("/api/map/detections", auth=("ecologist", "wrong"))
            assert wrong.status_code == 401
            assert "Basic" in wrong.headers.get("www-authenticate", "")
    finally:
        config_module.reload_settings()


def test_device_token_does_not_open_dashboard(seeded):
    """Токен устройства — для загрузки аудио, не для просмотра карты."""
    from fastapi.testclient import TestClient

    from server import config as config_module

    original = config_module.get_settings().dashboard_auth_enabled
    config_module.reload_settings()
    try:
        with TestClient(app) as anonymous:
            assert anonymous.get(
                "/api/map/detections", headers={"Authorization": "Bearer token-dev_a"}
            ).status_code == (200 if not original else 401)
    finally:
        config_module.reload_settings()