"""
REST API реестра устройств (Задача 2): `server/api_devices.py`.

* `POST  /api/devices`       — регистрация (выдаёт `device_id` и API-токен, токен виден один раз);
* `GET   /api/devices`       — список устройств с координатами и статусом активности;
* `GET   /api/devices/{id}`  — карточка устройства;
* `PATCH /api/devices/{id}`  — переименование, координаты, ротация токена;
* `POST  /api/devices/{id}/deactivate` / `activate` — отключение устройства оператором;
* `GET   /api/devices/{id}/events` — события (разрывы связи, деактивации).

Регистрация и изменение — только по админ-токену или учётной записи дашборда.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .auth import require_admin
from .config import get_settings
from .db import get_db
from .devices import (
    DeviceValidationError,
    device_status,
    get_device,
    list_devices,
    register_device,
    set_device_active,
    update_device,
)
from .models import Device, DeviceEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/devices", tags=["devices"])


class DeviceCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120, description="Человекочитаемое имя микрофона")
    owner: str = Field("", max_length=120, description="Владелец площадки/контакты")
    contact: Optional[str] = Field(None, max_length=200)
    device_type: str = Field("stationary", description="stationary — фиксированная точка, mobile — GPS в каждом чанке")
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lon: Optional[float] = Field(None, ge=-180, le=180)
    altitude_m: Optional[float] = None
    place_note: Optional[str] = Field(None, max_length=300, description="Описание места: пойма, опушка, выход...")
    expected_chunk_sec: Optional[float] = Field(
        None, gt=0, description="Ожидаемый интервал между чанками (для алертов о разрывах связи)"
    )
    note: Optional[str] = None


class DeviceUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    owner: Optional[str] = Field(None, max_length=120)
    contact: Optional[str] = Field(None, max_length=200)
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lon: Optional[float] = Field(None, ge=-180, le=180)
    altitude_m: Optional[float] = None
    place_note: Optional[str] = Field(None, max_length=300)
    expected_chunk_sec: Optional[float] = Field(None, gt=0)
    note: Optional[str] = None
    rotate_token: bool = Field(False, description="Выпустить новый API-токен (старый перестаёт работать)")


class DeviceCreatedResponse(BaseModel):
    device: dict
    token: str = Field(..., description="API-токен устройства. Сохраните его: сервер его не хранит.")


@router.post("", response_model=DeviceCreatedResponse, status_code=status.HTTP_201_CREATED)
def create_device(
    payload: DeviceCreateRequest,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> DeviceCreatedResponse:
    """Зарегистрировать устройство и получить `device_id` + API-токен."""
    try:
        registered = register_device(
            session,
            name=payload.name,
            owner=payload.owner,
            contact=payload.contact,
            device_type=payload.device_type,
            lat=payload.lat,
            lon=payload.lon,
            altitude_m=payload.altitude_m,
            place_note=payload.place_note,
            expected_chunk_sec=payload.expected_chunk_sec,
            note=payload.note,
        )
    except DeviceValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    session.commit()
    return DeviceCreatedResponse(device=registered.device.to_dict(), token=registered.token)


@router.get("")
def read_devices(
    active_only: bool = Query(False, description="Только активные устройства"),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict:
    """Список устройств с координатами и статусом активности."""
    settings = get_settings()
    devices = list_devices(session, active_only=active_only)
    return {
        "count": len(devices),
        "gap_alert_sec": settings.gap_alert_sec,
        "devices": [device_status(d, settings.gap_alert_sec) for d in devices],
    }


@router.get("/{device_id}")
def read_device(
    device_id: str,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict:
    device = get_device(session, device_id)
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Устройство не найдено")
    settings = get_settings()
    data = device_status(device, settings.gap_alert_sec)
    data["detections_count"] = _count_detections(session, device_id)
    return data


@router.patch("/{device_id}")
def patch_device(
    device_id: str,
    payload: DeviceUpdateRequest,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict:
    """Переименовать устройство, изменить координаты или выпустить новый токен."""
    device = get_device(session, device_id)
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Устройство не найдено")
    try:
        new_token = update_device(
            session,
            device,
            name=payload.name,
            owner=payload.owner,
            contact=payload.contact,
            lat=payload.lat,
            lon=payload.lon,
            altitude_m=payload.altitude_m,
            place_note=payload.place_note,
            expected_chunk_sec=payload.expected_chunk_sec,
            note=payload.note,
            rotate_token=payload.rotate_token,
        )
    except DeviceValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    session.commit()
    response = device.to_dict()
    if new_token:
        response["token"] = new_token
        response["token_notice"] = "Новый токен показывается один раз, старый больше не работает."
    return response


@router.post("/{device_id}/deactivate")
def deactivate_device(
    device_id: str,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict:
    device = get_device(session, device_id)
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Устройство не найдено")
    set_device_active(session, device, False, reason="Деактивация оператором через API")
    session.commit()
    return device.to_dict()


@router.post("/{device_id}/activate")
def activate_device(
    device_id: str,
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict:
    device = get_device(session, device_id)
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Устройство не найдено")
    set_device_active(session, device, True, reason="Реактивация оператором через API")
    session.commit()
    return device.to_dict()


@router.get("/{device_id}/events")
def read_device_events(
    device_id: str,
    limit: int = Query(50, ge=1, le=500),
    session: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> dict:
    """События устройства: разрывы связи, опоздавшие чанки, деактивации."""
    if get_device(session, device_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Устройство не найдено")
    events = (
        session.query(DeviceEvent)
        .filter(DeviceEvent.device_id == device_id)
        .order_by(DeviceEvent.created_at.desc())
        .limit(limit)
        .all()
    )
    return {"count": len(events), "events": [e.to_dict() for e in events]}


def _count_detections(session: Session, device_id: str) -> int:
    from .models import Detection

    return session.query(Detection).filter(Detection.device_id == device_id).count()