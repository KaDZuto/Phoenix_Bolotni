"""
Протокол непрерывной загрузки чанков (Задача 3, п.1): нарезка потока на устройстве,
перекрытия между соседними чанками и распознавание реальных разрывов связи.

Устройство не ждёт «конца записи», а шлёт короткие чанки с перекрытием, поэтому
сервер должен различать три ситуации:

* **штатное перекрытие** — начало нового чанка раньше конца предыдущего (`overlap > 0`);
* **продолжение потока** — стык без перекрытия (`overlap ≈ 0`);
* **разрыв связи** — между концом предыдущего чанка и началом нового прошло заметно
  больше ожидаемого интервала (`gap > 0`), устройство было оффлайн (сеть, батарея).

Логика чистая (без БД и сети), поэтому тестируется на синтетических таймстампах.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import get_settings
from .models import Chunk, ensure_utc, utcnow

#: Отклонение часов устройства, при котором метки времени отбрасываются (задаётся в .env).


class ChunkValidationError(ValueError):
    """Некорректные метаданные чанка (время, порядковый номер, длительность)."""


@dataclass
class ChunkTiming:
    """Результат сверки времени прихода чанка с предыдущим чанком устройства."""

    started_at: datetime
    ended_at: datetime
    duration_sec: float
    overlap_with_prev_sec: float
    gap_before_sec: float
    out_of_order: bool = False
    duplicated: bool = False
    gap_event: bool = False

    def to_dict(self) -> dict:
        return {
            "started_at": ensure_utc(self.started_at).isoformat(),
            "ended_at": ensure_utc(self.ended_at).isoformat(),
            "duration_sec": round(self.duration_sec, 3),
            "overlap_with_prev_sec": round(self.overlap_with_prev_sec, 3),
            "gap_before_sec": round(self.gap_before_sec, 3),
            "out_of_order": self.out_of_order,
            "duplicated": self.duplicated,
            "gap_event": self.gap_event,
        }


def expected_chunk_interval(device_expected_sec: Optional[float], settings=None) -> float:
    """
    Ожидаемый интервал между началами чанков устройства.

    Приоритет: индивидуальная настройка устройства → дефолт сервиса.
    """
    cfg = settings or get_settings()
    if device_expected_sec and device_expected_sec > 0:
        return float(device_expected_sec)
    return float(cfg.default_expected_chunk_sec)


def parse_timestamp(value: object, field: str) -> datetime:
    """
    Разобрать метку времени чанка: Unix-секунды (float) или ISO-8601 строка.

    Время всегда приводится к UTC: устройства в поле живут в разных часовых поясах.
    """
    if value is None:
        raise ChunkValidationError(f"Не задано поле {field}")
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise ChunkValidationError(f"Некорректный Unix-таймстамп в {field}: {value!r}") from exc
    elif isinstance(value, str):
        text = value.strip()
        # Устройства присылают метку времени как строку: это либо ISO-8601, либо Unix-секунды.
        try:
            parsed = datetime.fromtimestamp(float(text), tz=timezone.utc)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ChunkValidationError(
                    f"Некорректная метка времени в {field}: {value!r} "
                    "(ожидается ISO-8601 или Unix-секунды)"
                ) from exc
    else:
        raise ChunkValidationError(f"Неподдерживаемый тип метки времени в {field}: {type(value).__name__}")

    parsed = ensure_utc(parsed)
    if parsed is None:  # pragma: no cover - защита
        raise ChunkValidationError(f"Не удалось разобрать {field}")
    return parsed


def analyze_chunk_timing(
    started_at: datetime,
    ended_at: datetime,
    previous: Optional[Chunk] = None,
    device_expected_sec: Optional[float] = None,
    settings=None,
    now: Optional[datetime] = None,
) -> ChunkTiming:
    """
    Сверить границы чанка с предыдущим чанком того же устройства.

    Возвращает длительность, перекрытие и величину разрыва; `gap_event=True`, если
    разрыв превышает ожидаемый интервал — это повод записать `DeviceEvent(gap)`.
    """
    cfg = settings or get_settings()
    started = ensure_utc(started_at)
    ended = ensure_utc(ended_at)
    duration = (ended - started).total_seconds()

    settings_min = cfg.chunk_min_sec
    settings_max = cfg.chunk_max_sec
    if duration <= 0:
        raise ChunkValidationError(
            f"Некорректный интервал чанка: начало {started.isoformat()} не раньше конца {ended.isoformat()}"
        )
    if duration < settings_min or duration > settings_max:
        raise ChunkValidationError(
            f"Длительность чанка {duration:.1f} с вне диапазона "
            f"[{settings_min:.0f}, {settings_max:.0f}] с (см. td3.md, Задача 3)"
        )

    current_now = ensure_utc(now) if now else utcnow()
    if ended < current_now - timedelta(seconds=cfg.max_clock_skew_past_sec):
        raise ChunkValidationError(
            "Метка времени чанка слишком далеко в прошлом — вероятно, неверны часы устройства "
            f"(допустимое отставание {cfg.max_clock_skew_past_sec:.0f} с)"
        )
    if started > current_now + timedelta(seconds=cfg.max_clock_ahead_sec):
        raise ChunkValidationError(
            "Метка времени чанка опережает серверное время более чем на "
            f"{cfg.max_clock_ahead_sec / 60:.0f} мин — проверьте часы устройства"
        )

    if previous is None:
        return ChunkTiming(
            started_at=started,
            ended_at=ended,
            duration_sec=duration,
            overlap_with_prev_sec=0.0,
            gap_before_sec=0.0,
        )

    prev_start = ensure_utc(previous.started_at)
    prev_end = ensure_utc(previous.ended_at)
    if started < prev_start:
        # Устройство дослало более ранний чанк (перезагрузка/повтор): это не разрыв, но порядок нарушен.
        return ChunkTiming(
            started_at=started,
            ended_at=ended,
            duration_sec=duration,
            overlap_with_prev_sec=0.0,
            gap_before_sec=0.0,
            out_of_order=True,
        )

    overlap = (prev_end - started).total_seconds()
    gap = max(0.0, (started - prev_end).total_seconds())
    device_interval = expected_chunk_interval(device_expected_sec, cfg)
    gap_flag = gap_event_required(gap, device_interval, cfg.gap_alert_sec)

    return ChunkTiming(
        started_at=started,
        ended_at=ended,
        duration_sec=duration,
        overlap_with_prev_sec=max(0.0, overlap),
        gap_before_sec=gap,
        gap_event=gap_flag,
    )


def gap_event_required(gap_sec: float, device_interval_sec: float, gap_alert_sec: float) -> bool:
    """
    Нужен ли алерт о разрыве связи.

    Порог: разрыв больше полутора интервалов отправки **и** больше `gap_alert_sec`,
    чтобы обычная сетевая просадка не превращалась в ложную тревогу.
    """
    return gap_sec > max(1.5 * device_interval_sec, gap_alert_sec)


def overlap_zone(chunk: Chunk) -> tuple[float, float]:
    """
    Интервал перекрытия чанка с предыдущим — зона, где возможны задвоения детекций.

    Возвращает (начало, конец) в Unix-секундах; если перекрытия не было — пустой интервал.
    """
    overlap = float(chunk.overlap_with_prev_sec or 0.0)
    if overlap <= 0:
        return (0.0, 0.0)
    start, _ = chunk.interval
    return (start, start + min(overlap, float(chunk.duration_sec)))