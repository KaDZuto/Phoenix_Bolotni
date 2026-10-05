"""
Сервис реестра устройств (Задача 2) — логика, общая для HTTP-эндпоинтов и CLI.

Устройство получает `device_id` (генерируется сервером) и собственный API-токен.
Для стационарных микрофонов `lat/lon` фиксируются один раз при настройке,
для мобильных — приходят с каждым чанком.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .auth import register_device_token
from .geo import validate_coordinates
from .models import (
    DEVICE_TYPE_STATIONARY,
    DEVICE_TYPES,
    EVENT_REGISTERED,
    Device,
    DeviceEvent,
    ensure_utc,
    utcnow,
)

logger = logging.getLogger(__name__)


class DeviceValidationError(ValueError):
    """Некорректные данные регистрации устройства."""


@dataclass
class RegisteredDevice:
    """Результат регистрации: устройство + токен (токен больше нигде не сохраняется)."""

    device: Device
    token: str


def register_device(
    session: Session,
    name: str,
    owner: str = "",
    contact: Optional[str] = None,
    device_type: str = DEVICE_TYPE_STATIONARY,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    altitude_m: Optional[float] = None,
    place_note: Optional[str] = None,
    expected_chunk_sec: Optional[float] = None,
    note: Optional[str] = None,
) -> RegisteredDevice:
    """
    Зарегистрировать новое устройство и выдать ему API-токен.

    Для `stationary` координаты обязательны (точка микрофона фиксируется при настройке),
    для `mobile` — не обязательны (приходят с каждым чанком).
    """
    device_type = (device_type or DEVICE_TYPE_STATIONARY).strip().lower()
    if device_type not in DEVICE_TYPES:
        raise DeviceValidationError(
            f"Неизвестный тип устройства: {device_type!r}. Допустимо: {', '.join(DEVICE_TYPES)}"
        )
    if not name or not name.strip():
        raise DeviceValidationError("Не задано имя устройства")

    if device_type == DEVICE_TYPE_STATIONARY:
        if lat is None or lon is None:
            raise DeviceValidationError(
                "Для стационарного устройства обязательны фиксированные lat/lon"
            )
    if lat is not None and lon is not None:
        try:
            lat, lon = validate_coordinates(lat, lon)
        except ValueError as exc:
            raise DeviceValidationError(str(exc)) from exc
    if expected_chunk_sec is not None and expected_chunk_sec <= 0:
        raise DeviceValidationError("expected_chunk_sec должен быть положительным")

    device = Device(
        id=Device.new_id(),
        name=name.strip()[:120],
        owner=(owner or "").strip()[:120],
        contact=contact,
        device_type=device_type,
        lat=lat,
        lon=lon,
        altitude_m=altitude_m,
        place_note=place_note,
        expected_chunk_sec=expected_chunk_sec,
        note=note,
        is_active=True,
    )
    token = register_device_token(device)
    session.add(device)
    session.add(
        DeviceEvent(
            device_id=device.id,
            kind=EVENT_REGISTERED,
            started_at=utcnow(),
            detail=f"Регистрация устройства «{device.name}» ({device_type})",
        )
    )
    session.flush()
    logger.info(
        "Зарегистрировано устройство %s (%s, %s)", device.id, device.name, device_type
    )
    return RegisteredDevice(device=device, token=token)


def get_device(session: Session, device_id: str) -> Optional[Device]:
    return session.get(Device, device_id)


def list_devices(
    session: Session,
    active_only: bool = False,
    include_inactive_status: bool = True,
) -> list[Device]:
    query = session.query(Device).order_by(Device.name)
    if active_only:
        query = query.filter(Device.is_active.is_(True))
    devices = query.all()
    return devices


def update_device(
    session: Session,
    device: Device,
    name: Optional[str] = None,
    owner: Optional[str] = None,
    contact: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    altitude_m: Optional[float] = None,
    place_note: Optional[str] = None,
    expected_chunk_sec: Optional[float] = None,
    note: Optional[str] = None,
    rotate_token: bool = False,
) -> Optional[str]:
    """Изменить параметры устройства; при `rotate_token` вернуть новый токен."""
    if name is not None:
        if not name.strip():
            raise DeviceValidationError("Имя устройства не может быть пустым")
        device.name = name.strip()[:120]
    if owner is not None:
        device.owner = owner.strip()[:120]
    if contact is not None:
        device.contact = contact
    if lat is not None and lon is not None:
        try:
            device.lat, device.lon = validate_coordinates(lat, lon)
        except ValueError as exc:
            raise DeviceValidationError(str(exc)) from exc
    if altitude_m is not None:
        device.altitude_m = altitude_m
    if place_note is not None:
        device.place_note = place_note
    if expected_chunk_sec is not None:
        if expected_chunk_sec <= 0:
            raise DeviceValidationError("expected_chunk_sec должен быть положительным")
        device.expected_chunk_sec = expected_chunk_sec
    if note is not None:
        device.note = note

    device.updated_at = utcnow()
    new_token = register_device_token(device) if rotate_token else None
    session.flush()
    if new_token:
        logger.info("Выпущен новый токен устройства %s", device.id)
    return new_token


def set_device_active(
    session: Session, device: Device, active: bool, reason: str = ""
) -> Device:
    """Деактивировать/переактивировать устройство (используется и для алертов о пропаже связи)."""
    if device.is_active == active:
        return device
    device.is_active = active
    device.updated_at = utcnow()
    session.add(
        DeviceEvent(
            device_id=device.id,
            kind="deactivated" if not active else "reactivated",
            started_at=utcnow(),
            detail=reason or ("Деактивация оператором" if not active else "Реактивация оператором"),
        )
    )
    session.flush()
    logger.info("Устройство %s: %s", device.id, "включено" if active else "деактивировано")
    return device


def touch_device_upload(
    session: Session,
    device: Device,
    *,
    chunk_started_at: Optional[datetime] = None,
    seq: Optional[int] = None,
    remote_addr: Optional[str] = None,
) -> None:
    """Обновить heartbeat устройства (последняя активность для маркеров карты и алертов)."""
    now = utcnow()
    device.last_seen_at = now
    device.last_upload_at = now
    if chunk_started_at is not None:
        started = ensure_utc(chunk_started_at)
        if device.last_chunk_started_at is None or started > ensure_utc(device.last_chunk_started_at):
            device.last_chunk_started_at = started
    if seq is not None and (device.last_seq is None or seq > device.last_seq):
        device.last_seq = seq
    if remote_addr:
        device.last_remote_addr = remote_addr[:64]


def stale_after_sec(device: Device, now: Optional[datetime] = None, threshold_sec: float = 180.0) -> Optional[float]:
    """Сколько секунд прошло с последней активности (None, если активность была)."""
    reference = ensure_utc(device.last_seen_at) if device.last_seen_at else None
    if reference is None:
        return None
    current = ensure_utc(now) if now else datetime.now(timezone.utc)
    return max(0.0, (current - reference).total_seconds())


def device_status(
    device: Device,
    gap_alert_sec: float,
    now: Optional[datetime] = None,
) -> dict:
    """Статус активности устройства для списка/карты: `online` / `silent` / `offline`."""
    silence = stale_after_sec(device, now=now)
    if silence is None:
        state = "never_seen"
    elif silence <= gap_alert_sec:
        state = "online"
    elif silence <= gap_alert_sec * 3:
        state = "silent"
    else:
        state = "offline"
    data = device.to_dict()
    data.update(
        {
            "status": state,
            "seconds_since_last_seen": round(silence, 1) if silence is not None else None,
            "gap_alert_sec": gap_alert_sec,
            "has_fixed_location": device.has_fixed_location,
        }
    )
    return data