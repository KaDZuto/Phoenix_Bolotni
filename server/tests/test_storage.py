"""
Тесты Задачи 4 [P0] — хранение только статистики, без аудио.

Критерий приёмки td3.md: после обработки чанка ни на диске, ни в БД не остаётся
аудио-байт (эмулируем приём и обработку нескольких чанков и сверяем размер рабочей
 директории/БД до и после).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from sqlalchemy import inspect

from server.db import get_engine
from server.devices import register_device
from server.models import Chunk, Detection
from server.queue_worker import ChunkWorker
from server.spool import get_spool
from server.tests.conftest import ScriptedAnalyzer, make_wav_bytes, multipart_chunk, now_ts

CHUNK_SEC = 25.0
NUM_CHUNKS = 3


def post(client, token: str, audio: bytes, *, seq: int):
    """Отправить чанк на `POST /ingest` (обёртка над `multipart_chunk`)."""
    payload = multipart_chunk(audio, seq=seq, started_at=now_ts(-CHUNK_SEC))
    return client.post(
        "/ingest",
        files=payload["files"],
        data=payload["data"],
        headers={"Authorization": f"Bearer {token}"},
    )


def directory_size(path: Path) -> int:
    """Суммарный размер файлов в каталоге (байт)."""
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


@pytest.fixture
def device(db_session):
    registered = register_device(db_session, name="Микрофон поймы", lat=55.75, lon=37.62)
    db_session.commit()
    return registered


def test_inline_worker_processes_queue(scripted_analyzer, device, db_session, monkeypatch):
    """
    Воркер по умолчанию живёт внутри процесса API.

    Причина не в удобстве, а в требовании «аудио не сохраняется»: байты чанка лежат
    в памяти принявшего их процесса. Отдельный контейнер воркера увидит в БД
    задание, но не найдёт байтов и пометит его `expired` — тихая потеря наблюдения.
    """
    from fastapi.testclient import TestClient

    from server import config as config_module
    from server.ingest import app

    monkeypatch.setenv("PHOENIX_INLINE_WORKER", "true")
    config_module.reload_settings()
    assert config_module.get_settings().inline_worker is True

    audio = make_wav_bytes(duration_sec=CHUNK_SEC)
    # Клиент создаётся здесь, а не фикстурой: фоновый воркер запускается в lifespan,
    # то есть уже после того, как конфигурация перечитана выше.
    with TestClient(app) as client:
        response = post(client, device.token, audio, seq=1)
        assert response.status_code == 202

        # Ожидание результата вместо фиксированного sleep: тест не должен зависеть
        # от того, успел ли воркер проснуться за 0.25 с.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            db_session.expire_all()
            if db_session.query(Detection).count():
                break
            time.sleep(0.05)

    assert db_session.query(Detection).count() >= 1
    # Байты освобождены сразу после инференса — это ядро требования Задачи 4.
    assert get_spool().stats()["chunks_in_memory"] == 0


def test_separate_worker_process_would_expire_jobs(client, device, db_session):
    """
    Отдельный процесс воркера не находит байтов и честно помечает задание `expired`.

    Это поведение фиксируется намеренно: оно объясняет, почему воркер по умолчанию
    встроен в процесс API, и запрещает «просто добавить второй контейнер воркера»
    без выноса очереди в общее хранилище.
    """
    from server.models import JOB_EXPIRED, Job

    audio = make_wav_bytes(duration_sec=CHUNK_SEC)
    assert post(client, device.token, audio, seq=1).status_code == 202

    # Эмулируем перезапуск процесса API: память очищена, а задание в БД осталось.
    get_spool().clear()

    results = ChunkWorker(worker_id="other-process").run_once(limit=1)
    assert [result.status for result in results] == [JOB_EXPIRED]
    db_session.expire_all()
    assert db_session.query(Job).filter(Job.status == JOB_EXPIRED).count() == 1
    assert db_session.query(Detection).count() == 0


def test_no_audio_bytes_remain_after_processing(client, device, db_session, tmp_path):
    """Полный цикл «3 чанка принято и обработано»: аудио не остаётся нигде."""
    analyzer = ScriptedAnalyzer(spans=[("corvus_frugilegus", 5.0, 12.0)])
    worker = ChunkWorker(analyzer=analyzer)

    payloads = [make_wav_bytes(duration_sec=CHUNK_SEC, seed=i) for i in range(NUM_CHUNKS)]
    total_audio_bytes = sum(len(p) for p in payloads)
    assert total_audio_bytes > 2 * 1024 * 1024, "тестовые чанки должны быть заметного размера"

    db_path = Path(str(get_engine().url.database))
    size_before = db_path.stat().st_size if db_path.exists() else 0
    dir_before = directory_size(tmp_path)

    for index, payload in enumerate(payloads, start=1):
        started = now_ts(-(CHUNK_SEC * (NUM_CHUNKS - index + 1)) - 1)
        request = multipart_chunk(payload, seq=index, started_at=started)
        response = client.post(
            "/ingest",
            files=request["files"],
            data=request["data"],
            headers={"Authorization": f"Bearer {device.token}"},
        )
        assert response.status_code == 202, response.text

    # Пока воркер не работал, байты лежали только в оперативной памяти.
    assert get_spool().size_bytes == total_audio_bytes
    assert directory_size(tmp_path) == size_before, "приём не должен создавать файлов на диске"

    for _ in payloads:
        worker.run_once()

    # 1) Очередь памяти очищена.
    assert get_spool().size_bytes == 0
    assert get_spool().count == 0

    # 2) На диске не появилось ни одного нового файла, кроме самой БД.
    db_session.expire_all()
    assert db_path.exists()
    assert db_session.query(Detection).count() == NUM_CHUNKS
    assert db_session.query(Chunk).count() == NUM_CHUNKS
    new_files = [
        p
        for p in tmp_path.rglob("*")
        if p.is_file() and p.name not in {db_path.name}
    ]
    assert new_files == [], f"Ожидалось отсутствие аудио на диске, найдено: {new_files}"

    # 3) Прирост БД кратный размеру аудио (только метаданные и числа).
    size_after = db_path.stat().st_size
    growth = size_after - size_before
    assert growth < total_audio_bytes / 5, (
        f"БД выросла на {growth} байт при {total_audio_bytes} байтах аудио — "
        "похоже, аудио сохраняется"
    )
    assert directory_size(tmp_path) - dir_before == growth

    # 4) В таблицах нет ни одного BLOB-столбца.
    inspector = inspect(get_engine())
    for table in inspector.get_table_names():
        for column in inspector.get_columns(table):
            type_name = type(column["type"]).__name__.upper()
            assert "BLOB" not in type_name and "BYTEA" not in type_name, (
                f"Таблица {table} содержит бинарный столбец {column['name']} — аудио не должно храниться"
            )


def test_detections_store_only_numbers(client, device, db_session):
    """В detections лежат только числа, координаты, времена и вид — никаких байт аудио."""
    analyzer = ScriptedAnalyzer(spans=[("strix_aluco", 3.0, 9.0)])
    worker = ChunkWorker(analyzer=analyzer)
    payload = make_wav_bytes(duration_sec=CHUNK_SEC)
    request = multipart_chunk(payload, seq=1, started_at=now_ts(-CHUNK_SEC - 1))
    client.post(
        "/ingest",
        files=request["files"],
        data=request["data"],
        headers={"Authorization": f"Bearer {device.token}"},
    )
    worker.run_once()
    db_session.expire_all()

    detection = db_session.query(Detection).one()
    row = detection.to_dict()
    assert set(row) == {
        "id",
        "device_id",
        "chunk_id",
        "species_slug",
        "species_ru",
        "confidence",
        "mean_confidence",
        "votes",
        "windows_evaluated",
        "window_start_ts",
        "window_end_ts",
        "lat",
        "lon",
        "source",
        "model_version",
        "duplicate_of_id",
        "created_at",
    }
    assert row["source"] == "model_auto", "Детекции помечаются как автопредсказание модели"
    # Чанк хранит только метаданные: хеш и размер вместо самого звука.
    chunk = db_session.query(Chunk).one()
    assert chunk.sha256 and len(chunk.sha256) == 64
    assert chunk.size_bytes == len(payload)
    assert chunk.audio_format == "wav"
    for value in vars(chunk).values():
        assert not isinstance(value, (bytes, bytearray, memoryview))


def test_debug_audio_option_is_explicit_and_expires(client, device, db_session, monkeypatch):
    """P2-опция «послушать проблемный чанк»: только последние N файлов и с TTL."""
    from server.queue_worker import DEBUG_AUDIO_DIR, cleanup_debug_audio

    monkeypatch.setenv("PHOENIX_DEBUG_KEEP_AUDIO", "true")
    monkeypatch.setenv("PHOENIX_DEBUG_KEEP_LAST_CHUNKS", "1")
    from server import config as config_module

    config_module.reload_settings()

    worker = ChunkWorker(analyzer=ScriptedAnalyzer(spans=[]))
    for index in (1, 2):
        request = multipart_chunk(
            make_wav_bytes(duration_sec=CHUNK_SEC, seed=index),
            seq=index,
            started_at=now_ts(-CHUNK_SEC * (3 - index)),
        )
        client.post(
            "/ingest",
            files=request["files"],
            data=request["data"],
            headers={"Authorization": f"Bearer {device.token}"},
        )
        results = worker.run_once()
        assert results[0].audio_kept is True

    assert DEBUG_AUDIO_DIR.exists()
    assert len(list(DEBUG_AUDIO_DIR.glob("*.bin"))) == 1, "Хранится только последний чанк"

    # Файлы старше TTL удаляются при следующей чистке.
    for path in DEBUG_AUDIO_DIR.glob("*.bin"):
        os.utime(path, (0, 0))
    assert cleanup_debug_audio(ttl_sec=1.0, keep_last=0) >= 1
    assert list(DEBUG_AUDIO_DIR.glob("*.bin")) == []

    # По умолчанию опция выключена — на диск не пишется ничего.
    monkeypatch.setenv("PHOENIX_DEBUG_KEEP_AUDIO", "false")
    config_module.reload_settings()
    request = multipart_chunk(make_wav_bytes(duration_sec=CHUNK_SEC), seq=3, started_at=now_ts(-CHUNK_SEC))
    client.post(
        "/ingest",
        files=request["files"],
        data=request["data"],
        headers={"Authorization": f"Bearer {device.token}"},
    )
    results = ChunkWorker(analyzer=ScriptedAnalyzer(spans=[])).run_once()
    assert results[0].audio_kept is False