"""
Воркер очереди обработки чанков (Задача 3, п.3–4).

Воркер забирает задание, достаёт байты чанка **из оперативной памяти**, прогоняет их
через многократную проверку (`confirmation.ChunkAnalyzer`) и сохраняет только
подтверждённые детекции. Сырое аудио нигде не остаётся: байты освобождаются сразу после
инференса, в БД попадают только числа.

Дедупликация: соседние чанки перекрываются, поэтому детекция на стыке проверяется по
пересечению временных интервалов и не записывается дважды (`dedup.py`).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .audio_io import decode_chunk
from .chunks import overlap_zone
from .confirmation import ChunkAnalyzer, ConfirmationResult, get_chunk_analyzer
from .config import SERVER_DIR, get_settings
from .db import init_db, session_scope
from .dedup import Interval, dedup_detections, find_duplicate, to_interval
from .models import (
    EVENT_ERROR,
    JOB_DONE,
    JOB_EXPIRED,
    JOB_FAILED,
    JOB_PROCESSING,
    JOB_QUEUED,
    Chunk,
    Detection,
    DeviceEvent,
    Device,
    Job,
    RawWindow,
    ensure_utc,
    utcnow,
)
from .spool import SpoolItemMissing, get_spool

logger = logging.getLogger(__name__)

DEBUG_AUDIO_DIR = SERVER_DIR / "debug_audio"


@dataclass
class ProcessingResult:
    """Итог обработки одного чанка."""

    job_id: str
    chunk_id: Optional[int]
    device_id: str
    status: str
    windows_total: int = 0
    detections_saved: int = 0
    duplicates_skipped: int = 0
    species: List[str] = field(default_factory=list)
    error: Optional[str] = None
    audio_kept: bool = False

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "chunk_id": self.chunk_id,
            "device_id": self.device_id,
            "status": self.status,
            "windows_total": self.windows_total,
            "detections_saved": self.detections_saved,
            "duplicates_skipped": self.duplicates_skipped,
            "species": self.species,
            "error": self.error,
            "audio_kept": self.audio_kept,
        }


class ChunkWorker:
    """Воркер обработки очереди чанков (можно запускать несколько процессов)."""

    def __init__(self, worker_id: Optional[str] = None, analyzer: Optional[ChunkAnalyzer] = None):
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self._analyzer = analyzer
        self.processed_chunks = 0
        self.saved_detections = 0

    @property
    def analyzer(self) -> ChunkAnalyzer:
        if self._analyzer is None:
            self._analyzer = get_chunk_analyzer()
        return self._analyzer

    # -- обработка одного задания ----------------------------------------
    def process_job(self, session: Session, job: Job) -> ProcessingResult:
        """Обработать задание: инференс → дедупликация → запись подтверждённых детекций."""
        chunk = session.get(Chunk, job.chunk_id) if job.chunk_id else None
        result = ProcessingResult(
            job_id=job.id, chunk_id=job.chunk_id, device_id=job.device_id, status=JOB_DONE
        )
        if chunk is None:
            job.status = JOB_FAILED
            job.error = "Чанк не найден в БД"
            job.finished_at = utcnow()
            result.status = JOB_FAILED
            result.error = job.error
            return result

        spool = get_spool()
        try:
            payload = spool.pop(job.id)
        except SpoolItemMissing:
            job.status = JOB_EXPIRED
            job.error = "Аудио чанка уже не в оперативной памяти (истёк TTL или перезапуск)"
            job.finished_at = utcnow()
            chunk.status = JOB_EXPIRED
            chunk.error = job.error
            result.status = JOB_EXPIRED
            result.error = job.error
            logger.warning("Чанк %s: %s", chunk.id, job.error)
            return result

        try:
            decoded = decode_chunk(payload, declared_format=chunk.audio_format)
            if get_settings().debug_keep_audio:
                result.audio_kept = self._keep_debug_audio(job.id, chunk.device_id, payload)

            analysis = self.analyzer.analyze(decoded.samples, decoded.sample_rate)
            offset = chunk.started_at.timestamp()
            detections = self._persist_detections(session, chunk, analysis.with_time_offset(offset))

            result.windows_total = analysis.total_windows
            result.detections_saved = len(detections)
            result.duplicates_skipped = max(
                0, sum(1 for d in detections if d.duplicate_of_id is not None)
            )
            result.species = sorted({d.species_slug for d in detections if d.duplicate_of_id is None})

            self._store_raw_windows(session, chunk, analysis, offset)
            self._mark_chunk_done(session, chunk, job, analysis, detections)
            self.processed_chunks += 1
            self.saved_detections += len(detections)
            logger.info(
                "Чанк %s (%s seq=%s): окон %d, сохранено детекций %d, дублей отброшено %d%s",
                chunk.id,
                chunk.device_id,
                chunk.seq,
                analysis.total_windows,
                len(detections),
                result.duplicates_skipped,
                f" (подтверждено: {', '.join(result.species)})" if result.species else "",
            )
        except Exception as exc:  # noqa: BLE001 - воркер не должен падать на одном чанке
            session.rollback()
            logger.exception("Ошибка обработки чанка %s (job %s): %s", chunk.id, job.id, exc)
            chunk = session.get(Chunk, job.chunk_id) if job.chunk_id else None
            job = session.get(Job, job.id) or job
            job.status = JOB_FAILED
            job.error = str(exc)[:2000]
            job.finished_at = utcnow()
            if chunk is not None:
                chunk.status = JOB_FAILED
                chunk.error = job.error
            self._record_error_event(session, job, str(exc))
            result.status = JOB_FAILED
            result.error = job.error
        finally:
            # Байты чанка больше нигде не нужны: память освобождена (td3.md, Задача 4).
            spool.discard(job.id)

        return result

    # -- запись результатов ----------------------------------------------
    def _persist_detections(
        self, session: Session, chunk: Chunk, analysis: ConfirmationResult
    ) -> List[Detection]:
        """Создать записи `detections` с дедупликацией на стыках перекрывающихся чанков."""
        if not analysis.confirmed_detections:
            return []

        settings = get_settings()
        device = session.get(Device, chunk.device_id)
        chunk_start_ts = ensure_utc(chunk.started_at)
        chunk_end_ts = ensure_utc(chunk.ended_at)
        overlap_start, overlap_end = overlap_zone(chunk)
        model_version = self._model_version()

        staged: List[Detection] = []
        for confirmed in analysis.confirmed_detections:
            interval = Interval(confirmed.start_sec, confirmed.end_sec)
            lat, lon = (chunk.lat, chunk.lon) if device is None else device.location_for_detection(
                chunk.lat, chunk.lon
            )

            # Детекция целиком в теле чанка дублем быть не может — проверяем только стык.
            duplicate_of = None
            if overlap_end > overlap_start:
                seam = Interval(overlap_start - 1.0, overlap_end + 1.0)
                if _interval_overlap(interval, seam) > 0:
                    duplicate_of = find_duplicate(
                        session,
                        device_id=chunk.device_id,
                        species_slug=confirmed.species,
                        interval=interval,
                        chunk_start_ts=chunk_start_ts,
                        chunk_end_ts=chunk_end_ts,
                        overlap_frac=settings.dedup_overlap_frac,
                    )

            detection = Detection(
                device_id=chunk.device_id,
                chunk_id=chunk.id,
                species_slug=confirmed.species,
                confidence=confirmed.confidence,
                mean_confidence=confirmed.mean_confidence,
                votes=confirmed.votes,
                windows_evaluated=confirmed.windows_evaluated,
                window_start_ts=datetime.fromtimestamp(confirmed.start_sec, tz=timezone.utc),
                window_end_ts=datetime.fromtimestamp(confirmed.end_sec, tz=timezone.utc),
                lat=lat,
                lon=lon,
                source="model_auto",
                model_version=model_version,
                created_at=utcnow(),
            )
            session.add(detection)
            session.flush()
            if duplicate_of is not None:
                detection.duplicate_of_id = duplicate_of.id
            staged.append(detection)

        unique, duplicates = dedup_detections(
            session, [d for d in staged if d.duplicate_of_id is None], settings.dedup_overlap_frac
        )
        for duplicate in duplicates:
            duplicate.duplicate_of_id = unique[0].id if unique else None
        session.flush()
        return staged

    def _store_raw_windows(
        self, session: Session, chunk: Chunk, analysis: ConfirmationResult, offset: float
    ) -> None:
        """Сохранить per-window предсказания (только если явно включено `PHOENIX_STORE_RAW_WINDOWS`)."""
        if not get_settings().store_raw_windows:
            return
        for window in analysis.raw_windows:
            session.add(
                RawWindow(
                    chunk_id=chunk.id,
                    device_id=chunk.device_id,
                    window_index=window.index,
                    window_start_ts=datetime.fromtimestamp(window.start_sec + offset, tz=timezone.utc),
                    window_end_ts=datetime.fromtimestamp(window.end_sec + offset, tz=timezone.utc),
                    top_species=window.top_species,
                    top_probability=window.top_probability,
                    status=window.status,
                    top_k_json=json.dumps(window.probabilities, ensure_ascii=False),
                )
            )

    def _mark_chunk_done(
        self,
        session: Session,
        chunk: Chunk,
        job: Job,
        analysis: ConfirmationResult,
        detections: List[Detection],
    ) -> None:
        chunk.status = JOB_DONE
        chunk.processed_at = utcnow()
        chunk.windows_total = analysis.total_windows
        chunk.windows_stored = len(analysis.raw_windows) if get_settings().store_raw_windows else 0
        chunk.error = None
        job.status = JOB_DONE
        job.finished_at = utcnow()
        job.detections_count = sum(1 for d in detections if d.duplicate_of_id is None)
        job.duplicates_count = sum(1 for d in detections if d.duplicate_of_id is not None)
        session.flush()

    def _record_error_event(self, session: Session, job: Job, message: str) -> None:
        """После повторных ошибок — событие для оператора."""
        if job.attempts < 2:
            return
        session.add(
            DeviceEvent(
                device_id=job.device_id,
                kind=EVENT_ERROR,
                started_at=utcnow(),
                detail=f"Ошибка обработки чанка (job {job.id}, попыток {job.attempts}): {message[:300]}",
            )
        )

    def _model_version(self) -> Optional[str]:
        settings = get_settings()
        try:
            stat = settings.model_path.stat()
        except OSError:
            return None
        return f"{settings.model_path.name}:{stat.st_size}"

    def _keep_debug_audio(self, job_id: str, device_id: str, payload: bytes) -> bool:
        """
        Явная P2-опция «послушать проблемный чанк»: пишет только последние N чанков
        с коротким TTL (по умолчанию выключено — см. `PHOENIX_DEBUG_KEEP_AUDIO`).
        """
        settings = get_settings()
        try:
            DEBUG_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            target = DEBUG_AUDIO_DIR / f"{job_id}_{device_id}.bin"
            target.write_bytes(payload)
        except OSError as exc:  # pragma: no cover - зависит от прав ФС
            logger.error("Не удалось сохранить отладочный чанк: %s", exc)
            return False
        cleanup_debug_audio()
        return True

    # -- очередь ---------------------------------------------------------
    def claim_next_job(self, session: Session) -> Optional[Job]:
        """Атомарно забрать очередное задание (оптимистичная блокировка по статусу)."""
        job_ids = session.execute(
            select(Job.id).where(Job.status == JOB_QUEUED).order_by(Job.created_at).limit(20)
        ).scalars().all()
        for job_id in job_ids:
            claimed = session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JOB_QUEUED)
                .values(
                    status=JOB_PROCESSING,
                    started_at=utcnow(),
                    worker_id=self.worker_id,
                    attempts=Job.attempts + 1,
                )
            )
            session.commit()
            if claimed.rowcount == 1:
                return session.get(Job, job_id)
        return None

    def run_once(self, limit: Optional[int] = None) -> List[ProcessingResult]:
        """Обработать до `limit` заданий (по умолчанию — пачку из настроек)."""
        settings = get_settings()
        limit = limit if limit is not None else settings.worker_batch_size
        results: List[ProcessingResult] = []
        with session_scope() as session:
            job = self.claim_next_job(session)
            if job is None:
                return results
            results.append(self.process_job(session, job))
        return results

    def run_forever(self, poll_interval: Optional[float] = None) -> None:  # pragma: no cover - служба
        """Основной цикл воркера (`python -m server.queue_worker`)."""
        settings = get_settings()
        interval = poll_interval if poll_interval is not None else settings.worker_poll_interval_sec
        logger.info("Воркер %s запущен (интервал опроса %.2f с)", self.worker_id, interval)
        while True:
            try:
                results = self.run_once()
            except Exception as exc:  # noqa: BLE001 - цикл не должен умирать
                logger.exception("Ошибка цикла воркера: %s", exc)
                time.sleep(max(1.0, interval))
                continue
            if not results:
                time.sleep(interval)


def _interval_overlap(a: Interval, b: Interval) -> float:
    return max(0.0, min(a.end, b.end) - max(a.start, b.start))


def cleanup_debug_audio(ttl_sec: Optional[float] = None, keep_last: Optional[int] = None) -> int:
    """
    Удалить отладочные записи чанков старше TTL (P2-опция, по умолчанию выключена).

    Это осознанно отдельная, явно включаемая политика с коротким TTL, а не общее
    хранилище аудио: см. td3.md, Задача 4.
    """
    settings = get_settings()
    ttl = float(ttl_sec if ttl_sec is not None else settings.debug_keep_ttl_sec)
    keep = int(keep_last if keep_last is not None else settings.debug_keep_last_chunks)
    if not DEBUG_AUDIO_DIR.exists():
        return 0
    files = sorted(DEBUG_AUDIO_DIR.glob("*.bin"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    deadline = time.time() - ttl
    for index, path in enumerate(files):
        if index < keep and path.stat().st_mtime >= deadline:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:  # pragma: no cover
            pass
    return removed


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Воркер обработки аудиочанков Phoenix_Bolotni (инференс без записи аудио)"
    )
    parser.add_argument("--once", action="store_true", help="Обработать текущую очередь и выйти")
    parser.add_argument("--limit", type=int, default=None, help="Сколько чанков обработать в режиме --once")
    parser.add_argument("--worker-id", default=None, help="Идентификатор воркера для логов и БД")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    init_db()
    worker = ChunkWorker(worker_id=args.worker_id)

    if args.once:
        total = 0
        while True:
            results = worker.run_once(limit=1)
            if not results:
                break
            for result in results:
                print(json.dumps(result.to_dict(), ensure_ascii=False))
            total += len(results)
            if args.limit is not None and total >= args.limit:
                break
        print(f"Обработано чанков: {total}, сохранено детекций: {worker.saved_detections}", flush=True)
        return 0

    worker.run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())