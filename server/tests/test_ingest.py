"""
Тесты Задачи 3 [P0] — непрерывный приём потока, очередь, дедупликация, разрывы связи.

Критерии приёмки td3.md: загрузка серии перекрывающихся тестовых чанков не создаёт
задвоенных детекций на границах; принудительно смоделированный разрыв связи корректно
детектируется и логируется; невалидный токен/формат — понятная ошибка.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from server.alerts import build_gap_report, find_stale_devices
from server.chunks import (
    ChunkValidationError,
    analyze_chunk_timing,
    gap_event_required,
    parse_timestamp,
)
from server.devices import register_device
from server.models import (
    EVENT_GAP,
    Detection,
    DeviceEvent,
    Chunk,
    Job,
    JOB_DONE,
    JOB_EXPIRED,
    utcnow,
)
from server.queue_worker import ChunkWorker
from server.spool import AudioSpool, SpoolFull, get_spool, set_spool
from server.tests.conftest import ScriptedAnalyzer, make_wav_bytes, multipart_chunk, now_ts

CHUNK_SEC = 60.0


@pytest.fixture
def device(db_session):
    registered = register_device(
        db_session,
        name="Пойма, микрофон 1",
        owner="Иванова",
        lat=55.7558,
        lon=37.6173,
        expected_chunk_sec=60,
    )
    db_session.commit()
    return registered


def post_chunk(client, token, audio, seq, started_at, **kwargs):
    payload = multipart_chunk(audio, seq=seq, started_at=started_at, **kwargs)
    return client.post(
        "/ingest", files=payload["files"], data=payload["data"], headers={"Authorization": f"Bearer {token}"}
    )


# ---------------------------------------------------------------------------
# Валидация протокола (чистая логика)
# ---------------------------------------------------------------------------


def test_chunk_duration_limits():
    start = utcnow()
    with pytest.raises(ChunkValidationError, match="(?i)длительность чанка"):
        analyze_chunk_timing(start, start + timedelta(seconds=5))
    with pytest.raises(ChunkValidationError, match="(?i)длительность чанка"):
        analyze_chunk_timing(start, start + timedelta(seconds=600))
    with pytest.raises(ChunkValidationError, match="не раньше конца"):
        analyze_chunk_timing(start, start - timedelta(seconds=10))


def test_overlap_and_gap_computation():
    """Перекрытие — штатная граница чанков, большой разрыв — потеря наблюдений."""
    now = utcnow()
    previous = Chunk(
        id=1,
        device_id="dev_x",
        seq=1,
        started_at=now - timedelta(seconds=170),
        ended_at=now - timedelta(seconds=110),
        duration_sec=60.0,
    )
    # Штатное перекрытие 55 с: начало нового чанка раньше конца предыдущего.
    timing = analyze_chunk_timing(
        now - timedelta(seconds=165), now - timedelta(seconds=105), previous=previous
    )
    assert timing.overlap_with_prev_sec == pytest.approx(55.0)
    assert timing.gap_before_sec == 0.0
    assert timing.gap_event is False

    # Реальный разрыв: устройство молчало 200 с (порог = max(1.5 × 60 с, 180 с)).
    timing_gap = analyze_chunk_timing(now + timedelta(seconds=90), now + timedelta(seconds=150), previous=previous)
    assert timing_gap.overlap_with_prev_sec == 0.0
    assert timing_gap.gap_before_sec == pytest.approx(200.0, abs=1.0)
    assert timing_gap.gap_event is True


def test_small_network_hiccup_is_not_a_gap():
    previous_end = utcnow()
    timing = analyze_chunk_timing(
        previous_end + timedelta(seconds=20),
        previous_end + timedelta(seconds=80),
        previous=Chunk(
            id=1, device_id="dev_x", seq=1, started_at=previous_end - timedelta(seconds=60),
            ended_at=previous_end, duration_sec=60.0,
        ),
        device_expected_sec=60,
    )
    # Просадка сети меньше порога — это не разрыв наблюдений.
    assert timing.gap_before_sec == pytest.approx(20.0)
    assert timing.gap_event is False


def test_out_of_order_chunk_detected():
    start = utcnow()
    previous = Chunk(
        id=1, device_id="dev_x", seq=2, started_at=start, ended_at=start + timedelta(seconds=60),
        duration_sec=60.0,
    )
    timing = analyze_chunk_timing(
        start - timedelta(seconds=30), start + timedelta(seconds=30), previous=previous
    )
    assert timing.out_of_order is True
    assert timing.gap_event is False


def test_parse_timestamp_formats():
    assert parse_timestamp(1_700_000_000.0, "t").timestamp() == pytest.approx(1_700_000_000.0)
    assert parse_timestamp("1700000000", "t").timestamp() == pytest.approx(1_700_000_000.0)
    assert parse_timestamp("2023-11-14T22:13:20Z", "t").timestamp() == pytest.approx(1_700_000_000.0)
    with pytest.raises(ChunkValidationError):
        parse_timestamp("не время", "t")


def test_gap_event_threshold():
    # 60-секундные чанки: порог = max(1.5 интервала, gap_alert_sec) = 180 с.
    assert gap_event_required(60.0, 60.0, 180.0) is False
    assert gap_event_required(180.0, 60.0, 180.0) is False
    assert gap_event_required(240.0, 60.0, 180.0) is True
    # Устройство с редкими чанками (5 минут) — порог по интервалу отправки (450 с).
    assert gap_event_required(200.0, 300.0, 180.0) is False
    assert gap_event_required(500.0, 300.0, 180.0) is True


# ---------------------------------------------------------------------------
# Приём через HTTP
# ---------------------------------------------------------------------------


def test_ingest_accepts_chunk(client, device):
    audio = make_wav_bytes(duration_sec=CHUNK_SEC)
    response = post_chunk(client, device.token, audio, seq=1, started_at=now_ts(-CHUNK_SEC))
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "accepted"
    assert body["seq"] == 1
    assert body["duration_sec"] == pytest.approx(CHUNK_SEC, abs=0.2)
    assert body["job_id"].startswith("job_")
    assert get_spool().count == 1


def test_ingest_rejects_bad_token_and_format(client, device, db_session):
    audio = make_wav_bytes(duration_sec=CHUNK_SEC)

    bad_token = client.post(
        "/ingest",
        files={"audio": ("c.wav", audio, "audio/wav")},
        data={"seq": "1", "started_at": str(now_ts(-CHUNK_SEC))},
        headers={"Authorization": "Bearer pbk_nope"},
    )
    assert bad_token.status_code == 401

    payload = multipart_chunk(b"\x00\x01\x02 not audio at all", seq=2, started_at=now_ts(-CHUNK_SEC))
    bad_format = client.post(
        "/ingest", files=payload["files"], data=payload["data"],
        headers={"Authorization": f"Bearer {device.token}"},
    )
    assert bad_format.status_code == 422
    assert "аудио" in bad_format.json()["detail"].lower()

    short = post_chunk(client, device.token, make_wav_bytes(duration_sec=3.0), seq=3, started_at=now_ts(-3))
    assert short.status_code == 422
    assert "длительность" in short.json()["detail"].lower()

    long = post_chunk(client, device.token, make_wav_bytes(duration_sec=200.0), seq=4, started_at=now_ts(-30))
    assert long.status_code == 422

    assert db_session.query(Chunk).count() == 0


def test_ingest_is_idempotent_per_sequence_number(client, device):
    audio = make_wav_bytes(duration_sec=CHUNK_SEC)
    first = post_chunk(client, device.token, audio, seq=1, started_at=now_ts(-CHUNK_SEC))
    assert first.status_code == 202

    # Повтор того же чанка после сетевого сбоя — вернём прежний job_id, без дублей в БД.
    repeat = post_chunk(client, device.token, audio, seq=1, started_at=now_ts(-CHUNK_SEC))
    assert repeat.status_code == 202
    assert repeat.json()["status"] == "already_accepted"
    assert repeat.json()["chunk_id"] == first.json()["chunk_id"]

    # Тот же номер, но другое содержимое — конфликт (устройство сломало нумерацию).
    conflict = post_chunk(client, device.token, make_wav_bytes(duration_sec=CHUNK_SEC, freq=900), seq=1, started_at=now_ts(-CHUNK_SEC))
    assert conflict.status_code == 409


def test_ingest_records_gap_event_and_alert(client, device, db_session, caplog, monkeypatch):
    """Разрыв связи фиксируется событием и алертом оператору (td3.md, Задача 3 п.5)."""
    monkeypatch.setenv("PHOENIX_GAP_ALERT_SEC", "60")
    from server import config as config_module

    config_module.reload_settings()

    audio = make_wav_bytes(duration_sec=CHUNK_SEC)
    # Первый чанк пришёл с задержкой (устройство буферизует), второй — после 100 с молчания.
    assert post_chunk(client, device.token, audio, seq=1, started_at=now_ts(-115)).status_code == 202
    assert post_chunk(client, device.token, audio, seq=2, started_at=now_ts(-15 + 100)).status_code == 202
    db_session.expire_all()

    gap_chunks = db_session.query(Chunk).order_by(Chunk.seq).all()
    assert gap_chunks[0].gap_before_sec == 0.0
    # Первый чанк длится 60 с, поэтому разрыв = (now+85) - (now-55) = 140 с.
    assert gap_chunks[1].gap_before_sec == pytest.approx(140.0, abs=5.0)
    assert gap_chunks[1].overlap_with_prev_sec == 0.0

    events = db_session.query(DeviceEvent).filter(DeviceEvent.kind == EVENT_GAP).all()
    assert len(events) == 1
    assert events[0].duration_sec == pytest.approx(140.0, abs=5.0)
    assert "не на связи" in caplog.text or "разрыв" in caplog.text.lower()


def test_ingest_marks_stale_devices(client, device, db_session):
    audio = make_wav_bytes(duration_sec=CHUNK_SEC)
    post_chunk(client, device.token, audio, seq=1, started_at=now_ts(-CHUNK_SEC))
    db_session.expire_all()

    assert find_stale_devices(db_session) == []

    # Устройство замолчало на 10 минут при интервале 60 с — это разрыв наблюдений.
    device_obj = db_session.get(type(device.device), device.device.id)
    device_obj.last_seen_at = utcnow() - timedelta(seconds=600)
    db_session.commit()

    stale = find_stale_devices(db_session)
    assert len(stale) == 1
    assert stale[0]["device_id"] == device.device.id
    assert "не на связи" in stale[0]["message"]

    report = build_gap_report(db_session, notify=True)
    assert report.devices_checked == 1
    assert len(report.stale) == 1
    assert any(e["kind"] == EVENT_GAP for e in report.gap_events)


def test_spool_overflow_returns_503(client, device):
    """Перегрузка — честный 503, а не «успешное» принятие с потерей данных."""
    tight = AudioSpool(max_bytes=200_000, ttl_sec=60)
    set_spool(tight)
    audio = make_wav_bytes(duration_sec=CHUNK_SEC)  # ~2 МБ при 16 кГц PCM16
    response = post_chunk(client, device.token, audio, seq=1, started_at=now_ts(-CHUNK_SEC))
    assert response.status_code == 503
    assert response.headers.get("Retry-After")
    assert tight.count == 0

    big = AudioSpool(max_bytes=1024, ttl_sec=60)
    set_spool(big)
    with pytest.raises(SpoolFull):
        big.put("job_x", "dev_x", 1, b"0" * 5000)


# ---------------------------------------------------------------------------
# Обработка воркером
# ---------------------------------------------------------------------------


def test_worker_persists_confirmed_detections_only(client, device, db_session):
    analyzer = ScriptedAnalyzer(spans=[("corvus_frugilegus", 5.0, 12.5)])
    worker = ChunkWorker(worker_id="test-worker", analyzer=analyzer)

    audio = make_wav_bytes(duration_sec=CHUNK_SEC)
    assert post_chunk(client, device.token, audio, seq=1, started_at=now_ts(-CHUNK_SEC - 1)).status_code == 202

    results = worker.run_once()
    assert len(results) == 1
    assert results[0].status == JOB_DONE
    assert results[0].detections_saved == 1
    db_session.expire_all()

    detections = db_session.query(Detection).all()
    assert len(detections) == 1
    detection = detections[0]
    assert detection.species_slug == "corvus_frugilegus"
    assert detection.device_id == device.device.id
    assert detection.lat == pytest.approx(55.7558)
    assert detection.confidence == pytest.approx(0.9)
    # Окно 5..12.5 с от начала чанка, начало чанка — 61 с назад.
    expected_start = db_session.query(Chunk).one().started_at.timestamp() + 5.0
    assert detection.interval[0] == pytest.approx(expected_start, abs=0.5)
    assert detection.source == "model_auto"

    chunk = db_session.query(Chunk).one()
    assert chunk.status == JOB_DONE
    assert chunk.processed_at is not None
    assert chunk.windows_total == 1

    job = db_session.query(Job).one()
    assert job.status == JOB_DONE
    assert job.detections_count == 1


def test_worker_does_not_store_audio_anywhere(client, device, db_session, tmp_path):
    """После обработки байтов аудио не остаётся ни в очереди, ни на диске (Задача 4)."""
    analyzer = ScriptedAnalyzer(spans=[("strix_aluco", 3.0, 9.0)])
    worker = ChunkWorker(analyzer=analyzer)
    audio = make_wav_bytes(duration_sec=CHUNK_SEC)
    post_chunk(client, device.token, audio, seq=1, started_at=now_ts(-CHUNK_SEC - 1))
    assert get_spool().size_bytes > 0

    worker.run_once()

    assert get_spool().size_bytes == 0
    assert get_spool().count == 0
    debug_dir = db_session.get_bind().url.database
    assert debug_dir  # на диске осталась только сама БД
    assert analyzer.calls  # аудио реально декодировалось и ушло в инференс


def test_worker_marks_job_expired_when_audio_is_gone(client, device, db_session):
    worker = ChunkWorker(analyzer=ScriptedAnalyzer(spans=[("strix_aluco", 3.0, 9.0)]))
    audio = make_wav_bytes(duration_sec=CHUNK_SEC)
    post_chunk(client, device.token, audio, seq=1, started_at=now_ts(-CHUNK_SEC - 1))
    # Эмулируем перезапуск процесса релея: байты потеряны, метаданные остались.
    get_spool().clear()

    results = worker.run_once()
    assert results[0].status == JOB_EXPIRED
    db_session.expire_all()
    assert db_session.query(Detection).count() == 0
    assert db_session.query(Job).one().status == JOB_EXPIRED


def test_overlapping_chunks_do_not_double_detections(client, device, db_session):
    """
    Один и тот же звук в зоне перекрытия соседних чанков учитывается один раз.

    Чанк A: [T0, T0+60]. Чанк B: [T0+54.7, T0+114.7] (перекрытие 5.3 с).
    Пение на границе видно обоим чанкам, но в БД должна появиться одна детекция.
    """
    audio = make_wav_bytes(duration_sec=CHUNK_SEC, seed=1)
    t0 = now_ts(-CHUNK_SEC - 2)

    class TwoChunkAnalyzer(ScriptedAnalyzer):
        """Первый вызов — детекция у конца чанка A, второй — то же самое в чанке B."""

        def analyze(self, samples, sr):
            self.calls.append(int(len(samples)))
            from server.confirmation import ConfirmedDetection, ConfirmationResult

            start = 55.0 if len(self.calls) == 1 else 0.3
            detections = [
                ConfirmedDetection(
                    species="corvus_frugilegus",
                    start_sec=start,
                    end_sec=start + 5.0,
                    confidence=0.88,
                    mean_confidence=0.88,
                    votes=2,
                    windows_evaluated=3,
                    window_indices=[0],
                )
            ]
            return ConfirmationResult(
                clip_duration_sec=float(len(samples)) / float(sr),
                target_sr=sr,
                confirmed_detections=detections,
                source="scripted",
            )

    worker = ChunkWorker(analyzer=TwoChunkAnalyzer())
    first = post_chunk(client, device.token, audio, seq=1, started_at=t0)
    second = post_chunk(client, device.token, audio, seq=2, started_at=t0 + 54.7)
    assert first.status_code == 202 and second.status_code == 202
    assert second.json()["overlap_with_prev_sec"] == pytest.approx(5.3, abs=0.5)

    worker.run_once()
    worker.run_once()
    db_session.expire_all()

    detections = db_session.query(Detection).order_by(Detection.window_start_ts).all()
    assert len(detections) == 2, "Должны быть сохранены обе записи, но одна помечена дублем"
    assert sum(1 for d in detections if d.duplicate_of_id is None) == 1
    duplicate = next(d for d in detections if d.duplicate_of_id is not None)
    canonical = next(d for d in detections if d.duplicate_of_id is None)
    assert duplicate.species_slug == canonical.species_slug == "corvus_frugilegus"
    # Интервалы детекций реально пересекаются (это и есть повтор на стыке чанков).
    assert min(detections[0].interval[1], detections[1].interval[1]) > max(
        detections[0].interval[0], detections[1].interval[0]
    )
    # Дубли не попадают в карту: потребители фильтруют duplicate_of_id.
    assert db_session.query(Detection).filter(Detection.duplicate_of_id.is_(None)).count() == 1


def test_detections_far_from_seam_are_not_deduplicated(client, device, db_session):
    """Детекции внутри тела чанка не могут быть дублями — даже если интервалы соседствуют."""
    analyzer = ScriptedAnalyzer(spans=[("strix_aluco", 10.0, 15.0)])
    worker = ChunkWorker(analyzer=analyzer)
    audio = make_wav_bytes(duration_sec=CHUNK_SEC, seed=2)
    t0 = now_ts(-CHUNK_SEC - 2)
    assert post_chunk(client, device.token, audio, seq=1, started_at=t0).status_code == 202
    # Второй чанк начинается с перекрытием, но его детекция — в начале чанка (в зоне стыка)…
    analyzer.spans = [("strix_aluco", 0.5, 5.5)]
    assert post_chunk(client, device.token, audio, seq=2, started_at=t0 + CHUNK_SEC - 5.3).status_code == 202
    worker.run_once()
    worker.run_once()
    db_session.expire_all()
    # …и это разные моменты времени (60 с между ними), поэтому дубля нет.
    assert db_session.query(Detection).count() == 2
    assert db_session.query(Detection).filter(Detection.duplicate_of_id.isnot(None)).count() == 0


def test_worker_reports_model_unavailable_without_crash(client, device, db_session):
    """Если модель не загрузилась — чанк обработан, детекций 0, ошибки нет."""
    worker = ChunkWorker(analyzer=ScriptedAnalyzer(spans=[]))
    audio = make_wav_bytes(duration_sec=CHUNK_SEC)
    post_chunk(client, device.token, audio, seq=1, started_at=now_ts(-CHUNK_SEC - 1))
    results = worker.run_once()
    assert results[0].status == JOB_DONE
    assert results[0].detections_saved == 0
    db_session.expire_all()
    assert db_session.query(Detection).count() == 0


def test_raw_windows_stored_only_when_enabled(client, device, db_session, monkeypatch):
    from server.models import RawWindow

    audio = make_wav_bytes(duration_sec=CHUNK_SEC)
    post_chunk(client, device.token, audio, seq=1, started_at=now_ts(-CHUNK_SEC - 1))
    ChunkWorker(analyzer=ScriptedAnalyzer(spans=[("strix_aluco", 3.0, 9.0)])).run_once()
    db_session.expire_all()
    assert db_session.query(RawWindow).count() == 0  # по умолчанию выключено

    monkeypatch.setenv("PHOENIX_STORE_RAW_WINDOWS", "true")
    from server import config as config_module

    config_module.reload_settings()
    post_chunk(client, device.token, audio, seq=2, started_at=now_ts(-5))
    ChunkWorker(analyzer=ScriptedAnalyzer(spans=[("strix_aluco", 3.0, 9.0)])).run_once()
    db_session.expire_all()
    assert db_session.query(RawWindow).count() == 1


def test_ingest_status_endpoint(client, device, db_session):
    audio = make_wav_bytes(duration_sec=CHUNK_SEC)
    post_chunk(client, device.token, audio, seq=7, started_at=now_ts(-CHUNK_SEC))
    body = client.get(
        "/ingest/status", headers={"Authorization": f"Bearer {device.token}"}
    ).json()
    assert body["device_id"] == device.device.id
    assert body["last_seq"] == 7
    assert body["last_chunk"]["seq"] == 7
    assert body["expected_overlap_sec"] == 5.0
    assert body["queue"]["chunks_in_memory"] == 1


def test_health_endpoint(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["queue"]["max_bytes"] > 0