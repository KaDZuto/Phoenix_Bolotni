"""Общие фикстуры тестов серверного распознавателя (`server/`)."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pytest
import soundfile as sf

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Каждый тест работает на своей SQLite-БД в tmp; PostGIS-специфика не требуется."""
    monkeypatch.setenv("PHOENIX_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("PHOENIX_DASHBOARD_AUTH", "false")
    monkeypatch.setenv("PHOENIX_DEBUG_KEEP_AUDIO", "false")
    monkeypatch.setenv("PHOENIX_STORE_RAW_WINDOWS", "false")
    monkeypatch.setenv("PHOENIX_GAP_ALERT_SEC", "180")
    monkeypatch.setenv("PHOENIX_SPOOL_MAX_BYTES", str(8 * 1024 * 1024))
    # Тесты сами запускают воркер через ChunkWorker.run_once и проверяют его
    # результат. Фоновый воркер приложения забирал бы те же задания первым,
    # и результат зависел бы от порядка потоков, а не от кода теста.
    monkeypatch.setenv("PHOENIX_INLINE_WORKER", "false")

    from server import config as config_module
    from server import db as db_module
    from server import spool as spool_module

    config_module.reload_settings()
    db_module.reset_engine()
    spool_module.set_spool(None)
    yield
    spool_module.get_spool().clear()
    db_module.reset_engine()
    config_module.reload_settings()


def open_session():
    """
    Новая сессия БД для чтения результата, сделанного другим соединением.

    CLI и воркер открывают собственные сессии и коммитят, поэтому держать одну
    долгоживущую сессию в тесте нельзя — она не увидит их изменения. Вызывается
    как `open_session()` и закрывается на чтении, чтобы данные были актуальными.
    """
    from server.db import get_engine
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=get_engine())()


@pytest.fixture(autouse=True)
def _reset_analyzer():
    from server import confirmation

    confirmation.reset_chunk_analyzer()
    yield
    confirmation.reset_chunk_analyzer()


@pytest.fixture
def db_session():
    """Сессия БД с подготовленной схемой."""
    from server.db import init_db, session_scope

    init_db()
    with session_scope() as session:
        yield session


@pytest.fixture
def client():
    """FastAPI-клиент ingestion API (lifespan отключён — схему создаёт фикстура сессии)."""
    from fastapi.testclient import TestClient

    from server.ingest import app

    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Генерация тестового аудио (в памяти, на диск ничего не пишется)
# ---------------------------------------------------------------------------


def now_ts(offset_sec: float = 0.0) -> float:
    """
    Метка времени чанка «сейчас ± смещение» (Unix-секунды).

    Сервер проверяет уход часов устройства, поэтому в тестах нельзя использовать
    фиксированные метки из прошлого.
    """
    import time

    return time.time() + offset_sec


def make_wav_bytes(
    duration_sec: float = 25.0,
    sample_rate: int = 16000,
    freq: float = 1000.0,
    noise: float = 0.01,
    seed: int = 0,
) -> bytes:
    """Сгенерировать WAV-чанк нужной длительности (моно, 16 бит) и вернуть байты."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(duration_sec * sample_rate), dtype=np.float32) / sample_rate
    signal = 0.4 * np.sin(2 * np.pi * freq * t, dtype=np.float32)
    if noise:
        signal = signal + (noise * rng.standard_normal(signal.shape)).astype(np.float32)
    buffer = io.BytesIO()
    sf.write(buffer, signal.astype(np.float32), sample_rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def multipart_chunk(
    audio_bytes: bytes,
    seq: int,
    started_at: float,
    *,
    ended_at: Optional[float] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    device_id: Optional[str] = None,
    filename: str = "chunk.wav",
    content_type: str = "audio/wav",
) -> Dict[str, object]:
    """Собрать payload для `POST /ingest` (multipart)."""
    data: Dict[str, object] = {"seq": str(seq), "started_at": str(started_at)}
    if ended_at is not None:
        data["ended_at"] = str(ended_at)
    if lat is not None:
        data["lat"] = str(lat)
    if lon is not None:
        data["lon"] = str(lon)
    if device_id is not None:
        data["device_id"] = device_id
    return {
        "data": data,
        "files": {"audio": (filename, audio_bytes, content_type)},
    }


# ---------------------------------------------------------------------------
# Заглушка анализатора (реальная модель в тестах не используется)
# ---------------------------------------------------------------------------


class ScriptedAnalyzer:
    """
    Заглушка `ChunkAnalyzer`: возвращает заранее заданные подтверждённые детекции.

    Нужна, чтобы тесты приёма/дедупликации/разрывов были детерминированными и не зависели
    от весов модели (в репозитории `models/bird_model.onnx` — LFS-заглушка).
    """

    def __init__(self, spans: Optional[Sequence[Tuple[str, float, float]]] = None):
        self.spans: List[Tuple[str, float, float]] = list(spans or [])
        self.calls: List[int] = []

    def analyze(self, samples, sr: int):
        from server.confirmation import ConfirmedDetection, ConfirmationResult, WindowPrediction

        self.calls.append(int(len(samples)))
        windows = [
            WindowPrediction(
                index=0,
                start_sec=0.0,
                end_sec=float(len(samples)) / float(sr),
                top_species=self.spans[0][0] if self.spans else "_noise",
                top_probability=0.9,
                status="detected" if self.spans else "unknown/uncertain",
            )
        ]
        detections = [
            ConfirmedDetection(
                species=species,
                start_sec=start,
                end_sec=end,
                confidence=0.9,
                mean_confidence=0.9,
                votes=2,
                windows_evaluated=3,
                window_indices=[0],
            )
            for species, start, end in self.spans
        ]
        return ConfirmationResult(
            clip_duration_sec=float(len(samples)) / float(sr),
            target_sr=sr,
            raw_windows=windows,
            confirmed_detections=detections,
            source="scripted",
        )


@pytest.fixture
def scripted_analyzer():
    """
    Подменить анализатор на заглушку, выдающую одну детекцию `anas_platyrhynchos`.

    Подмена идёт через `confirmation.set_chunk_analyzer_factory`, а не присваиванием
    объекта: анализатор создаётся лениво и кэшируется в модуле, поэтому прямая подмена
    работала бы только до первого обращения к нему.

    Без заглушки тесты упёрлись бы в `models/bird_model.onnx`, который в репозитории
    хранится как git-LFS-указатель (133 байта), то есть настоящая модель недоступна.
    """
    from server import confirmation

    analyzer = ScriptedAnalyzer(spans=[("anas_platyrhynchos", 0.0, 3.0)])
    confirmation.set_chunk_analyzer_factory(lambda: analyzer)
    return analyzer
