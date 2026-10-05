"""
Алерты оператору о разрывах связи (Задача 3, п.5).

Идеально непрерывный поток гарантировать нельзя: сеть, разряд батареи, ветер, обслуживание.
Поэтому система не делает вид, что наблюдение было сплошным, а честно фиксирует пробелы
и сообщает о них: устройство X не на связи с ЧЧ:ММ.

Каналы доставки (включаются независимо):
* логгер (всегда);
* файл `PHOENIX_ALERT_LOG` (если задан);
* webhook `PHOENIX_ALERT_WEBHOOK` (если задан) — без внешних зависимостей, `urllib`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope
from .models import (
    EVENT_GAP,
    Device,
    DeviceEvent,
    ensure_utc,
    utcnow,
)

logger = logging.getLogger(__name__)

WebhookSender = Callable[[str], None]

_sent_lock = threading.Lock()
_sent_recent: List[str] = []


def _webhook(message: str) -> None:
    """Отправить сообщение на webhook оператора (best effort, без блокировки приёмa)."""
    url = os.environ.get("PHOENIX_ALERT_WEBHOOK", "").strip()
    if not url:
        return
    payload = json.dumps({"text": message, "source": "phoenix_bolotni_server"}, ensure_ascii=False).encode()
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - URL из конфига
            logger.debug("Алерт отправлен, webhook ответил %s", response.status)
    except (urllib.error.URLError, OSError) as exc:
        logger.error("Не удалось отправить алерт на webhook: %s", exc)


def send_alert(message: str) -> None:
    """Отправить алерт во все настроенные каналы."""
    logger.warning("АЛЕРТ: %s", message)
    settings = get_settings()
    if settings.alert_log_path:
        try:
            path = settings.alert_log_path
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{utcnow().isoformat()} {message}\n")
        except OSError as exc:  # pragma: no cover - зависит от прав ФС
            logger.error("Не удалось записать алерт в файл %s: %s", settings.alert_log_path, exc)
    if os.environ.get("PHOENIX_ALERT_WEBHOOK", "").strip():
        thread = threading.Thread(target=_webhook, args=(message,), daemon=True)
        thread.start()


@dataclass
class GapReport:
    """Сводный отчёт о разрывах по сети устройств."""

    generated_at: datetime
    devices_checked: int
    stale: List[dict]
    gap_events: List[dict]

    def to_dict(self) -> dict:
        return {
            "generated_at": ensure_utc(self.generated_at).isoformat(),
            "devices_checked": self.devices_checked,
            "stale_devices": self.stale,
            "gap_events": self.gap_events,
        }


def expected_interval(device: Device) -> float:
    settings = get_settings()
    return float(device.expected_chunk_sec or settings.default_expected_chunk_sec)


def find_stale_devices(
    session: Session,
    threshold_sec: Optional[float] = None,
    now: Optional[datetime] = None,
    active_only: bool = True,
) -> List[dict]:
    """
    Устройства, от которых давно нет данных.

    Порог по умолчанию: `1.5 × ожидаемый интервал` и не меньше `gap_alert_sec`,
    чтобы короткая сетевая просадка не считалась разрывом наблюдения.
    """
    settings = get_settings()
    threshold = threshold_sec if threshold_sec is not None else settings.gap_alert_sec
    current = ensure_utc(now) if now else utcnow()

    query = select(Device)
    if active_only:
        query = query.where(Device.is_active.is_(True))

    stale: List[dict] = []
    for device in session.execute(query).scalars():
        interval = expected_interval(device)
        limit = max(threshold, 1.5 * interval)
        last_seen = ensure_utc(device.last_seen_at)
        silence = (current - last_seen).total_seconds() if last_seen else None
        if silence is None or silence <= limit:
            continue
        stale.append(
            {
                "device_id": device.id,
                "name": device.name,
                "device_type": device.device_type,
                "lat": device.lat,
                "lon": device.lon,
                "last_seen_at": last_seen.isoformat() if last_seen else None,
                "seconds_since_last_seen": round(silence, 1) if silence is not None else None,
                "expected_chunk_sec": interval,
                "threshold_sec": round(limit, 1),
                "message": (
                    f"устройство «{device.name}» ({device.id}) не на связи с "
                    f"{last_seen.strftime('%H:%M') if last_seen else '—'} UTC — "
                    f"нет данных {(silence or 0) / 60:.0f} мин"
                ),
            }
        )
    return stale


def record_stale_devices(
    session: Session,
    stale: List[dict],
    now: Optional[datetime] = None,
    notify: bool = True,
) -> List[DeviceEvent]:
    """
    Записать события `gap` по «молчащим» устройствам (без дублей за последний час)
    и отправить алерты оператору.
    """
    current = ensure_utc(now) if now else utcnow()
    created: List[DeviceEvent] = []
    for item in stale:
        device_id = item["device_id"]
        already = session.execute(
            select(DeviceEvent)
            .where(
                DeviceEvent.device_id == device_id,
                DeviceEvent.kind == EVENT_GAP,
                DeviceEvent.started_at >= current.timestamp() - 3600,
            )
            .order_by(DeviceEvent.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if already is not None:
            continue
        last_seen = item.get("last_seen_at")
        started_at = datetime.fromisoformat(last_seen) if last_seen else current
        event = DeviceEvent(
            device_id=device_id,
            kind=EVENT_GAP,
            started_at=ensure_utc(started_at) or current,
            ended_at=current,
            duration_sec=item.get("seconds_since_last_seen"),
            detail=f"Нет данных от устройства более {item['threshold_sec']:.0f} с",
        )
        session.add(event)
        created.append(event)
        if notify:
            send_alert(item["message"])
    session.flush()
    return created


def recent_gap_events(session: Session, limit: int = 50, since_hours: int = 24) -> List[dict]:
    """Последние события разрывов — для дашборда и отчёта по покрытию наблюдений."""
    current = utcnow().timestamp()
    events = (
        session.execute(
            select(DeviceEvent)
            .where(
                DeviceEvent.kind == EVENT_GAP,
                DeviceEvent.created_at >= _from_timestamp(current - since_hours * 3600),
            )
            .order_by(DeviceEvent.created_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [event.to_dict() for event in events]


def _from_timestamp(value: float):
    from datetime import timezone

    return datetime.fromtimestamp(value, tz=timezone.utc)


def build_gap_report(
    session: Session,
    threshold_sec: Optional[float] = None,
    notify: bool = False,
) -> GapReport:
    """Сводка: какие устройства молчат + какие разрывы уже зафиксированы."""
    stale = find_stale_devices(session, threshold_sec=threshold_sec)
    if notify and stale:
        record_stale_devices(session, stale, notify=True)
    return GapReport(
        generated_at=utcnow(),
        devices_checked=len(session.execute(select(Device)).scalars().all()),
        stale=stale,
        gap_events=recent_gap_events(session),
    )


def watch_loop(interval_sec: Optional[float] = None, notify: bool = True) -> None:  # pragma: no cover - фоновая служба
    """
    Постоянный мониторинг разрывов (отдельный процесс `python -m server.alerts --watch`).

    Не выключает устройства автоматически: молчание — это сигнал оператору, а не приговор.
    """
    import time

    settings = get_settings()
    interval = interval_sec if interval_sec is not None else settings.gap_monitor_interval_sec
    logger.info("Мониторинг разрывов запущен (интервал %.0f с)", interval)
    while True:
        try:
            with session_scope() as session:
                build_gap_report(session, notify=notify)
        except Exception as exc:  # noqa: BLE001 - фоновая петля не должна падать
            logger.exception("Ошибка мониторинга разрывов: %s", exc)
        time.sleep(interval)


def main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover - точка входа
    """Точка входа сервиса мониторинга: `python -m server.alerts --watch`."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Мониторинг разрывов связи Phoenix_Bolotni (алерты оператору)"
    )
    parser.add_argument("--watch", action="store_true", help="Бесконечный цикл проверок")
    parser.add_argument("--once", action="store_true", help="Одна проверка и выход")
    parser.add_argument("--interval", type=float, default=None, help="Интервал, секунд")
    parser.add_argument("--notify", action="store_true", help="Отправлять алерты (по умолчанию да)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    # Схема готовится до первого запроса: без этого сервис, стартующий на чистой БД,
    # падал бы с «no such table: devices» вместо нормальной работы.
    from .db import init_db

    init_db()

    if args.once:
        with session_scope() as session:
            report = build_gap_report(session, notify=args.notify)
        print(
            f"Устройств: {report.devices_checked}, не на связи: {len(report.stale)}, "
            f"событий за сутки: {len(report.gap_events)}"
        )
        for item in report.stale:
            print(f"  {item['device_id']}: {item['message']}")
        return 0

    watch_loop(interval_sec=args.interval, notify=args.notify)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())