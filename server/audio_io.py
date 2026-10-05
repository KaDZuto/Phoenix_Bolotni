"""
Декодирование аудио чанка **в память** и проверки формата (Задачи 3–4).

Ключевое требование td3.md (Задача 4): сырое аудио не сохраняется — ни в БД, ни на диске.
Поэтому здесь нет ни одного вызова, который пишет во временный файл: байты чанка
декодируются прямо из `bytes` и сразу отдаются в инференс, после чего память освобождается.
"""

from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np
import soundfile as sf

from .confirmation import load_voting_params
from .config import get_settings

logger = logging.getLogger(__name__)


class AudioValidationError(ValueError):
    """Чанк не проходит валидацию формата/длительности/размера."""


@dataclass
class DecodedChunk:
    """Декодированный чанк в оперативной памяти (моно, float32, целевая частота дискретизации)."""

    samples: np.ndarray
    sample_rate: int
    duration_sec: float
    original_sample_rate: int
    original_format: str

    @property
    def sample_count(self) -> int:
        return int(self.samples.shape[0])


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def detect_format(payload: bytes, declared: Optional[str] = None) -> str:
    """Определить формат по сигнатуре (не доверяем заголовку Content-Type)."""
    if payload[:4] == b"RIFF" and payload[8:12] == b"WAVE":
        return "wav"
    if payload[:4] == b"fLaC":
        return "flac"
    if payload[:4] == b"OggS":
        return "ogg"
    if payload[:4] == b"ID3" or payload[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    if payload[4:8] == b"ftyp":
        return "m4a"
    if declared:
        return declared.lower().lstrip(".")
    raise AudioValidationError("Не удалось определить формат аудио чанка ( wav/flac/ogg/mp3/m4a )")


def validate_payload_size(size_bytes: int) -> None:
    limit = get_settings().max_upload_bytes
    if size_bytes <= 0:
        raise AudioValidationError("Пустой чанк аудио")
    if size_bytes > limit:
        raise AudioValidationError(
            f"Размер чанка {size_bytes} Б превышает лимит {limit} Б "
            f"(см. PHOENIX_MAX_UPLOAD_BYTES)"
        )


def decode_chunk(
    payload: bytes,
    declared_format: Optional[str] = None,
    allow_resample: bool = True,
) -> DecodedChunk:
    """
    Декодировать чанк из памяти в mono float32 нужной частоты дискретизации.

    Ничего не пишет на диск: `soundfile` читает из `io.BytesIO`.
    """
    validate_payload_size(len(payload))
    audio_format = detect_format(payload, declared_format)
    try:
        data, sr = sf.read(io.BytesIO(payload), dtype="float32", always_2d=False)
    except Exception as exc:  # pragma: no cover - зависит от битых данных
        raise AudioValidationError(f"Не удалось декодировать аудио ({audio_format}): {exc}") from exc

    if data.ndim > 1:
        # Несколько каналов (в т.ч. dual-mono) усредняем в моно — модель работает с моно.
        data = data.mean(axis=1)
    original_sr = int(sr)
    original_format = audio_format

    target_sr = load_voting_params().target_sr
    if allow_resample and original_sr != target_sr:
        data = resample_to(data, original_sr, target_sr)

    duration = float(data.shape[0]) / float(target_sr) if target_sr else 0.0
    return DecodedChunk(
        samples=np.ascontiguousarray(data, dtype=np.float32),
        sample_rate=target_sr,
        duration_sec=duration,
        original_sample_rate=original_sr,
        original_format=original_format,
    )


def validate_duration(decoded: DecodedChunk) -> None:
    """Проверить длительность чанка по протоколу непрерывной загрузки."""
    settings = get_settings()
    duration = decoded.duration_sec
    if duration < settings.chunk_min_sec:
        raise AudioValidationError(
            f"Длительность чанка {duration:.1f} с меньше минимума {settings.chunk_min_sec:.0f} с. "
            "Устройство должно слать чанки длиннее минимальной длины."
        )
    if duration > settings.chunk_max_sec:
        raise AudioValidationError(
            f"Длительность чанка {duration:.1f} с больше максимума {settings.chunk_max_sec:.0f} с. "
            "Устройство должно дробить поток на короткие чанки."
        )


def resample_to(samples: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Линейный ресемплинг (серверный код использует препроцессинг `audio_utils` модели)."""
    if src_sr == dst_sr:
        return samples
    duration = samples.shape[0] / float(src_sr)
    target_len = max(1, int(round(duration * dst_sr)))
    source_positions = np.linspace(0.0, samples.shape[0] - 1, num=samples.shape[0], dtype=np.float64)
    target_positions = np.linspace(0.0, samples.shape[0] - 1, num=target_len, dtype=np.float64)
    return np.interp(target_positions, source_positions, samples).astype(np.float32)


def chunk_metadata(decoded: DecodedChunk) -> Dict[str, Any]:
    return {
        "sample_rate": decoded.sample_rate,
        "original_sample_rate": decoded.original_sample_rate,
        "audio_format": decoded.original_format,
        "duration_sec": decoded.duration_sec,
        "samples": decoded.sample_count,
    }