"""
Очередь аудиочанков в оперативной памяти (Задачи 3–4).

Требование td3.md (Задача 4): «сырое аудио не сохраняется вообще, ни временно на
постоянном диске, ни в БД». Поэтому байты чанка лежат в RAM под тем же идентификатором
задания и удаляются сразу после инференса; в БД остаются только метаданные.

Очередь ограничена по объёму (`spool_max_bytes`) и времени жизни (`spool_ttl_sec`):
при переполнении `/ingest` честно отвечает `503` (backpressure), чтобы устройство
повторила отправку, а не «успешно» потеряла запись. Просроченные байты освобождаются
по TTL — это аварийный клапан, а не политика хранения.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional, Tuple

from .config import get_settings

logger = logging.getLogger(__name__)


class SpoolFull(RuntimeError):
    """Очередь аудио переполнена (нет свободной памяти под чанк)."""


class SpoolItemMissing(RuntimeError):
    """Байты чанка уже не в памяти (истёк TTL или процесс перезапустился)."""


@dataclass
class SpoolEntry:
    job_id: str
    device_id: str
    chunk_id: int
    payload: bytes
    enqueued_at: float


class AudioSpool:
    """Потокобезопасная ограниченная очередь байтов аудио (RAM, без записи на диск)."""

    def __init__(self, max_bytes: Optional[int] = None, ttl_sec: Optional[float] = None):
        settings = get_settings()
        self._max_bytes = int(max_bytes if max_bytes is not None else settings.spool_max_bytes)
        self._ttl_sec = float(ttl_sec if ttl_sec is not None else settings.spool_ttl_sec)
        self._items: "OrderedDict[str, SpoolEntry]" = OrderedDict()
        self._lock = threading.Lock()
        self._bytes_used = 0

    # -- свойства --------------------------------------------------------
    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def size_bytes(self) -> int:
        with self._lock:
            return self._bytes_used

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def stats(self) -> dict:
        with self._lock:
            return {
                "chunks_in_memory": len(self._items),
                "bytes_in_memory": self._bytes_used,
                "max_bytes": self._max_bytes,
                "ttl_sec": self._ttl_sec,
                "free_bytes": max(0, self._max_bytes - self._bytes_used),
            }

    # -- операции --------------------------------------------------------
    def put(self, job_id: str, device_id: str, chunk_id: int, payload: bytes) -> None:
        """Положить байты чанка в очередь или выбросить `SpoolFull`."""
        self._expire_locked()
        with self._lock:
            if len(payload) > self._max_bytes:
                raise SpoolFull(
                    f"Чанк {len(payload)} Б больше лимита памяти очереди {self._max_bytes} Б"
                )
            if self._bytes_used + len(payload) > self._max_bytes:
                raise SpoolFull(
                    "Очередь аудио переполнена — сервер не успевает обрабатывать поток, "
                    "повторите отправку этого чанка"
                )
            self._items[job_id] = SpoolEntry(
                job_id=job_id,
                device_id=device_id,
                chunk_id=chunk_id,
                payload=payload,
                enqueued_at=_now(),
            )
            self._bytes_used += len(payload)

    def pop(self, job_id: str) -> bytes:
        """Забрать байты чанка для обработки (после успеха/ошибки они удаляются из памяти)."""
        with self._lock:
            entry = self._items.pop(job_id, None)
            if entry is None:
                raise SpoolItemMissing(f"Чанк {job_id} отсутствует в оперативной очереди")
            self._bytes_used -= len(entry.payload)
            return entry.payload

    def peek_meta(self, job_id: str) -> Optional[Tuple[str, int]]:
        with self._lock:
            entry = self._items.get(job_id)
            return (entry.device_id, entry.chunk_id) if entry else None

    def discard(self, job_id: str) -> bool:
        """Удалить чанк из памяти, не обрабатывая его."""
        with self._lock:
            entry = self._items.pop(job_id, None)
            if entry is None:
                return False
            self._bytes_used -= len(entry.payload)
            return True

    def expire(self) -> int:
        """Освободить просроченные чанки (TTL)."""
        with self._lock:
            return self._expire_locked()

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes_used = 0

    def _expire_locked(self) -> int:
        deadline = _now() - self._ttl_sec
        expired = [job_id for job_id, entry in self._items.items() if entry.enqueued_at < deadline]
        for job_id in expired:
            entry = self._items.pop(job_id)
            self._bytes_used -= len(entry.payload)
            logger.warning(
                "Чанк %s (%s) удалён из оперативной очереди по TTL %.0f с — данные не обработаны",
                job_id,
                entry.device_id,
                self._ttl_sec,
            )
        return len(expired)


def _now() -> float:
    import time

    return time.monotonic()


_spool: Optional[AudioSpool] = None
_spool_lock = threading.Lock()


def get_spool() -> AudioSpool:
    """Общая очередь аудио процесса (в тестах подменяется через `set_spool`)."""
    global _spool
    if _spool is None:
        with _spool_lock:
            if _spool is None:
                _spool = AudioSpool()
    return _spool


def set_spool(spool: Optional[AudioSpool]) -> None:
    """Подменить очередь аудио (используется в тестах)."""
    global _spool
    with _spool_lock:
        _spool = spool