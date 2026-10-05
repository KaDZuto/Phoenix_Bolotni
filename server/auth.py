"""
Авторизация серверного распознавателя.

Два независимых механизма:

1. **Токены устройств** — выдаются при регистрации (`POST /api/devices` или CLI),
   передаются в `Authorization: Bearer <token>`. В БД хранится только SHA-256-хеш,
   поэтому украденная копия БД не даёт возможности заливать данные от чужого имени.
2. **Доступ к дашборду и административным эндпоинтам** — базовая аутентификация
   логин/пароль (`PHOENIX_DASHBOARD_USER` / `PHOENIX_DASHBOARD_PASSWORD`) и/или
   админ-токен `PHOENIX_ADMIN_TOKEN`. Внутренний инструмент для заказчика и
   приглашённых экологов, не публичный сервис.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import Device, hash_token

logger = logging.getLogger(__name__)

basic_scheme = HTTPBasic(auto_error=False)

CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Некорректные учётные данные",
    headers={"WWW-Authenticate": "Basic"},
)


def _bearer(header: Optional[str]) -> Optional[str]:
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def _token_matches(token: Optional[str], expected: Optional[str]) -> bool:
    if not token or not expected:
        return False
    return hmac.compare_digest(token, expected)


def register_device_token(device: Device) -> str:
    """Сгенерировать и сохранить новый токен устройства (возвращается только здесь)."""
    from .models import new_device_token, token_prefix

    token = new_device_token()
    device.token_hash = hash_token(token)
    device.token_prefix = token_prefix(token)
    return token


def find_device_by_token(token: str, session: Session) -> Optional[Device]:
    """Найти устройство по токену (сравнение по хешу)."""
    return session.query(Device).filter(Device.token_hash == hash_token(token)).one_or_none()


def authenticate_device(
    authorization: Optional[str] = Header(default=None),
    x_device_token: Optional[str] = Header(default=None),
    session: Session = Depends(get_db),
) -> Device:
    """
    Зависимость FastAPI: аутентифицировать устройство по API-токену.

    Поддерживаются заголовки `Authorization: Bearer <token>` и `X-Device-Token`.
    """
    token = _bearer(authorization) or x_device_token
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Отсутствует токен устройства (Authorization: Bearer <token>)",
            headers={"WWW-Authenticate": "Bearer"},
        )
    device = find_device_by_token(token, session)
    if device is None:
        logger.warning("Попытка загрузки с неизвестным/чужим токеном")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неизвестный токен устройства",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not device.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Устройство деактивировано оператором"
        )
    return device


def require_admin(
    credentials: Optional[HTTPBasicCredentials] = Depends(basic_scheme),
    authorization: Optional[str] = Header(default=None),
) -> None:
    """
    Зависимость FastAPI: доступ к административным эндпоинтам (регистрация и пр.).

    Управляется `PHOENIX_DASHBOARD_AUTH`: `false` — режим локальной разработки без авторизации
    (в проде так оставлять нельзя), `true` — админ-токен `PHOENIX_ADMIN_TOKEN` или базовые
    учётные данные дашборда.
    """
    settings = get_settings()
    if not settings.dashboard_auth_enabled:
        return
    if _token_matches(_bearer(authorization), settings.admin_token):
        return
    if credentials is not None and hmac.compare_digest(
        credentials.username.encode(), settings.dashboard_user.encode()
    ) and hmac.compare_digest(credentials.password.encode(), settings.dashboard_password.encode()):
        return
    raise CREDENTIALS_ERROR


def require_dashboard_auth(
    credentials: Optional[HTTPBasicCredentials] = Depends(basic_scheme),
    authorization: Optional[str] = Header(default=None),
) -> None:
    """
    Зависимость FastAPI: доступ к API карты и статике дашборда.

    По умолчанию включена базовая аутентификация; если `PHOENIX_DASHBOARD_AUTH=false`
    (локальная разработка) — доступ открыт.
    """
    settings = get_settings()
    if not settings.dashboard_auth_enabled:
        return
    if _token_matches(_bearer(authorization), settings.admin_token):
        return
    if credentials is not None and hmac.compare_digest(
        credentials.username.encode(), settings.dashboard_user.encode()
    ) and hmac.compare_digest(credentials.password.encode(), settings.dashboard_password.encode()):
        return
    raise CREDENTIALS_ERROR


def new_registration_nonce() -> str:
    """Случайный nonce для CLI-регистрации устройства (подтверждение, что запрос наш)."""
    return secrets.token_urlsafe(8)