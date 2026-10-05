"""
Тесты Задачи 2 [P0] — реестр устройств: регистрация, координаты, токены.

Критерии приёмки td3.md: можно зарегистрировать устройство, получить токен, увидеть его
в списке с координатами; загрузка с неверным/чужим токеном отклоняется.
"""

from __future__ import annotations

import pytest

from server.auth import find_device_by_token
from server.db import session_scope
from server.devices import (
    DeviceValidationError,
    device_status,
    get_device,
    list_devices,
    register_device,
    set_device_active,
    touch_device_upload,
    update_device,
)
from server.models import DEVICE_TYPE_MOBILE, DEVICE_TYPE_STATIONARY, hash_token


# ---------------------------------------------------------------------------
# Регистрация
# ---------------------------------------------------------------------------


def test_register_stationary_device(db_session):
    registered = register_device(
        db_session,
        name="Пойма, микрофон 1",
        owner="Иванова",
        contact="ivanova@example.org",
        device_type=DEVICE_TYPE_STATIONARY,
        lat=55.7558,
        lon=37.6173,
        expected_chunk_sec=60,
    )
    device = registered.device

    assert device.id.startswith("dev_")
    assert device.lat == pytest.approx(55.7558)
    assert device.lon == pytest.approx(37.6173)
    assert device.has_fixed_location is True
    assert device.is_active is True
    # Токен выдаётся один раз, в БД лежит только хеш.
    assert registered.token.startswith("pbk_")
    assert device.token_hash == hash_token(registered.token)
    assert device.token_prefix == registered.token[:12]
    assert registered.token not in device.token_hash

    found = find_device_by_token(registered.token, db_session)
    assert found is not None and found.id == device.id


def test_register_mobile_device_without_fixed_coordinates(db_session):
    registered = register_device(
        db_session, name="Телефон оператора", device_type=DEVICE_TYPE_MOBILE
    )
    device = registered.device
    assert device.lat is None and device.lon is None
    assert device.has_fixed_location is False
    # Для мобильных координаты приходят с каждым чанком.
    assert device.location_for_detection(54.1, 38.2) == (54.1, 38.2)
    assert device.location_for_detection(None, None) == (None, None)


def test_stationary_device_requires_coordinates(db_session):
    with pytest.raises(DeviceValidationError, match="lat/lon"):
        register_device(db_session, name="Без координат", device_type=DEVICE_TYPE_STATIONARY)


def test_invalid_coordinates_rejected(db_session):
    with pytest.raises(DeviceValidationError):
        register_device(
            db_session, name="Плохие координаты", lat=999.0, lon=10.0, device_type="stationary"
        )


def test_invalid_device_type_rejected(db_session):
    with pytest.raises(DeviceValidationError, match="Неизвестный тип устройства"):
        register_device(db_session, name="Что-то", device_type="drone")


def test_device_event_registered_written(db_session):
    from server.models import EVENT_REGISTERED, DeviceEvent

    register_device(db_session, name="Событие", lat=50.0, lon=30.0)
    events = db_session.query(DeviceEvent).filter(DeviceEvent.kind == EVENT_REGISTERED).all()
    assert len(events) == 1


# ---------------------------------------------------------------------------
# Список, статусы, деактивация
# ---------------------------------------------------------------------------


def test_list_devices_with_coordinates(db_session):
    register_device(db_session, name="Стационар", lat=55.0, lon=37.0)
    register_device(db_session, name="Мобильный", device_type=DEVICE_TYPE_MOBILE)
    devices = list_devices(db_session)
    assert {d.name for d in devices} == {"Стационар", "Мобильный"}
    statuses = [device_status(d, gap_alert_sec=180.0) for d in devices]
    stationary = next(s for s in statuses if s["name"] == "Стационар")
    assert stationary["lat"] == pytest.approx(55.0)
    assert stationary["has_fixed_location"] is True
    assert stationary["status"] == "never_seen"


def test_device_status_progresses_to_offline(db_session):
    from datetime import timedelta

    from server.models import utcnow

    registered = register_device(db_session, name="Молчун", lat=55.0, lon=37.0)
    device = registered.device
    touch_device_upload(db_session, device, seq=1)
    assert device_status(device, 180.0)["status"] == "online"
    assert device.last_seq == 1

    device.last_seen_at = utcnow() - timedelta(seconds=200)
    assert device_status(device, 180.0)["status"] == "silent"
    device.last_seen_at = utcnow() - timedelta(seconds=1000)
    assert device_status(device, 180.0)["status"] == "offline"


def test_deactivate_and_reactivate_device(db_session):
    from server.models import DeviceEvent

    registered = register_device(db_session, name="Отключаемое", lat=55.0, lon=37.0)
    device = registered.device
    set_device_active(db_session, device, False, reason="Ремонт площадки")
    assert device.is_active is False
    set_device_active(db_session, device, True)
    assert device.is_active is True
    kinds = [e.kind for e in db_session.query(DeviceEvent).filter(DeviceEvent.device_id == device.id)]
    assert "deactivated" in kinds and "reactivated" in kinds


def test_rename_and_rotate_token(db_session):
    registered = register_device(db_session, name="Старое имя", lat=55.0, lon=37.0)
    device = registered.device
    old_token = registered.token

    new_token = update_device(db_session, device, name="Новое имя", rotate_token=True)

    assert device.name == "Новое имя"
    assert new_token and new_token != old_token
    assert find_device_by_token(old_token, db_session) is None
    assert find_device_by_token(new_token, db_session) is not None
    with pytest.raises(DeviceValidationError):
        update_device(db_session, device, name="  ")


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


def test_http_device_lifecycle(client, monkeypatch):
    monkeypatch.setenv("PHOENIX_DASHBOARD_AUTH", "true")
    monkeypatch.setenv("PHOENIX_DASHBOARD_USER", "ecologist")
    monkeypatch.setenv("PHOENIX_DASHBOARD_PASSWORD", "secret")
    from server import config as config_module

    config_module.reload_settings()

    auth = ("ecologist", "secret")
    # Без авторизации регистрация не проходит.
    assert client.post("/api/devices", json={"name": "X", "lat": 1, "lon": 2}).status_code == 401

    created = client.post(
        "/api/devices",
        json={
            "name": "Пойма СБ",
            "owner": "Отдел охраны",
            "device_type": "stationary",
            "lat": 55.75,
            "lon": 37.62,
            "expected_chunk_sec": 60,
        },
        auth=auth,
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    device_id = payload["device"]["id"]
    token = payload["token"]
    assert token.startswith("pbk_")
    assert payload["device"]["lat"] == pytest.approx(55.75)

    listing = client.get("/api/devices", auth=auth).json()
    assert listing["count"] == 1
    assert listing["devices"][0]["id"] == device_id
    assert listing["devices"][0]["status"] == "never_seen"

    patched = client.patch(f"/api/devices/{device_id}", json={"name": "Пойма СБ (нов.)"}, auth=auth)
    assert patched.status_code == 200
    assert patched.json()["name"] == "Пойма СБ (нов.)"

    rotated = client.patch(
        f"/api/devices/{device_id}", json={"rotate_token": True}, auth=auth
    ).json()
    new_token = rotated["token"]
    assert new_token != token

    assert client.post(f"/api/devices/{device_id}/deactivate", auth=auth).status_code == 200
    assert client.get(f"/api/devices/{device_id}", auth=auth).json()["is_active"] is False
    assert client.post(f"/api/devices/{device_id}/activate", auth=auth).status_code == 200
    assert client.get("/api/devices/does-not-exist", auth=auth).status_code == 404


def test_http_registration_validation_error(client):
    response = client.post("/api/devices", json={"name": "Без координат", "device_type": "stationary"})
    assert response.status_code == 422
    assert "lat/lon" in response.json()["detail"]


def test_device_token_used_for_upload(client, db_session):
    """Токен устройства даёт доступ к /ingest, чужой токен — нет (Задача 2 + 3)."""
    registered = register_device(db_session, name="Устройство", lat=55.0, lon=37.0)
    db_session.commit()

    from server.tests.conftest import make_wav_bytes, multipart_chunk, now_ts

    audio = make_wav_bytes(duration_sec=22.0)
    payload = multipart_chunk(audio, seq=1, started_at=now_ts(-25))
    ok = client.post("/ingest", files=payload["files"], data=payload["data"], headers={
        "Authorization": f"Bearer {registered.token}"
    })
    assert ok.status_code == 202, ok.text

    wrong = client.post("/ingest", files=payload["files"], data=payload["data"], headers={
        "Authorization": "Bearer pbk_wrong-token-000000"
    })
    assert wrong.status_code == 401
    assert "токен" in wrong.json()["detail"].lower()

    no_token = client.post("/ingest", files=payload["files"], data=payload["data"])
    assert no_token.status_code == 401


def test_deactivated_device_cannot_upload(client, db_session):
    registered = register_device(db_session, name="Отключённое", lat=55.0, lon=37.0)
    device = registered.device
    set_device_active(db_session, device, False, reason="Тест")
    db_session.commit()

    from server.tests.conftest import make_wav_bytes, multipart_chunk, now_ts

    payload = multipart_chunk(make_wav_bytes(duration_sec=22.0), seq=1, started_at=now_ts(-25))
    response = client.post(
        "/ingest",
        files=payload["files"],
        data=payload["data"],
        headers={"Authorization": f"Bearer {registered.token}"},
    )
    assert response.status_code == 403
    assert get_device(db_session, device.id) is not None