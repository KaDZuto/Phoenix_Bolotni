"""
Публичная точка приёма данных — ingestion API (Задача 3, td3.md раздел 2).

Здесь живёт **тонкий публичный релей**: он принимает чанк от микрофона, проверяет
токен и формат, кладёт байты в оперативную очередь и **сразу отвечает `202 Accepted`**.
Инференс и работа с БД остаются на приватной стороне (VPN/туннель), а публичным
остаётся только приём файлов — это и есть требование заказчика «доступ по обычному
HTTPS без VPN».

Протокол непрерывной загрузки:
* устройство шлёт короткие чанки с перекрытием (`seq` + `started_at`/`ended_at`);
* детекции, чьи окна лежат в зоне перекрытия соседних чанков, дедуплицируются
  (см. `dedup.py`), поэтому «разрыв» и «штатная граница чанка» не путаются;
* реальный разрыв связи фиксируется событием `DeviceEvent(gap)` (см. `chunks.py`, `alerts.py`).

Аудио не сохраняется: байты удаляются из памяти сразу после инференса (`spool.py`).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audio_io import AudioValidationError, decode_chunk, sha256_bytes, validate_duration
from .api_devices import router as devices_router
from .api_map import router as map_router
from .auth import authenticate_device, require_dashboard_auth
from .chunks import (
    ChunkValidationError,
    analyze_chunk_timing,
    parse_timestamp,
)
from .config import get_settings
from .db import get_db, init_db
from .dedup import overlap_fraction, to_interval
from .models import (
    EVENT_GAP,
    Device,
    DeviceEvent,
    Chunk,
    Job,
    utcnow,
)
from .spool import SpoolFull, SpoolItemMissing, get_spool

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Старт приложения: схема БД и, по необходимости, воркер в фоне.

    Воркер по умолчанию **внутри процесса API**, и это не выбор удобства, а следствие
    требования «аудио не сохраняется»: байты чанка лежат в оперативной памяти того
    процесса, который принял загрузку. Отдельный контейнер воркера увидит в БД задание,
    но не найдёт байтов и пометит его как `expired` — тихая потеря наблюдения.

    Если воркер всё же запускают отдельным процессом (`python -m server.queue_worker`),
    то очередь обязана быть общей, а это в данной схеме не так.
    """
    init_db()
    worker_task = None
    if get_settings().inline_worker:
        worker_task = asyncio.create_task(_run_inline_worker())
        logger.info("Воркер запущен внутри процесса API")
    try:
        yield
    finally:
        if worker_task is not None:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task


async def _run_inline_worker() -> None:
    """
    Фоновой цикл воркера, живущий в процессе API.

    Реализовано на `asyncio.to_thread`, а не на пуле потоков с busy-wait: `run_forever`
    блокирующий и спит между опросами, поэтому блокировать им event loop нельзя — иначе
    перестаёт отвечать `/ingest`, то есть перестаёт принимать аудио.
    """
    from .queue_worker import ChunkWorker

    settings = get_settings()
    worker = ChunkWorker(worker_id=settings.inline_worker_id)
    while True:
        try:
            done = await asyncio.to_thread(worker.run_once)
            if not done:
                await asyncio.sleep(settings.worker_poll_interval_sec)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - фоновой воркер не должен умирать
            logger.exception("Ошибка воркера; продолжаю")
            await asyncio.sleep(settings.worker_poll_interval_sec)


app = FastAPI(
    title="Phoenix_Bolotni — серверный распознаватель (ingestion API)",
    description=(
        "Приём непрерывного потока аудиочанков от микрофонов/телефонов и обработка "
        "многократной проверкой (window voting). Хранится только статистика, без аудио."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

#: Административный API реестра устройств (живёт на приватной стороне, не в публичном релее).
app.include_router(devices_router)

#: Карта активности + дашборд (Задачи 5–6, td3.md разделы 5 и 6).
app.include_router(map_router)


# ---------------------------------------------------------------------------
# Дашборд (Leaflet) — статика из `server/webapp_map/`
# ---------------------------------------------------------------------------

WEBAPP_DIR = Path(__file__).resolve().parent / "webapp_map"

if WEBAPP_DIR.is_dir():  # pragma: no cover - в тестах статика может отсутствовать
    app.mount(
        "/static",
        StaticFiles(directory=str(WEBAPP_DIR)),
        name="static",
    )


@app.get("/", include_in_schema=False)
def dashboard(_: None = Depends(require_dashboard_auth)) -> FileResponse:
    """
    Дашборд карты активности (Задача 5).

    Отдаётся под той же базовой аутентификацией, что и API карты: это внутренний
    инструмент для заказчика и приглашённых экологов, а не публичный сервис.
    """
    index = WEBAPP_DIR / "index.html"
    if not index.is_file():  # pragma: no cover - защита от неполной установки
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Дашборд не установлен: отсутствует server/webapp_map/index.html",
        )
    return FileResponse(str(index))


# ---------------------------------------------------------------------------
# Служебные эндпоинты
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    """Проверка живости сервиса и состояния оперативной очереди."""
    spool = get_spool()
    settings = get_settings()
    return {
        "status": "ok",
        "queue": spool.stats(),
        "expected_overlap_sec": settings.expected_overlap_sec,
        "chunk_duration_sec": [settings.chunk_min_sec, settings.chunk_max_sec],
    }


# ---------------------------------------------------------------------------
# Приём чанков
# ---------------------------------------------------------------------------


@app.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest_chunk(
    request: Request,
    audio: UploadFile = File(..., description="Аудиочанк (wav/flac/ogg/mp3/m4a)"),
    seq: int = Form(..., description="Порядковый номер чанка от устройства (нарастающий)"),
    started_at: str = Form(..., description="Время начала записи чанка: ISO-8601 или Unix-секунды"),
    ended_at: Optional[str] = Form(None, description="Время конца записи чанка (по умолчанию выводится из длительности)"),
    lat: Optional[float] = Form(None, description="Координаты — для мобильных устройств"),
    lon: Optional[float] = Form(None),
    device_id: Optional[str] = Form(None, description="Идентификатор устройства (необязателен: есть в токене)"),
    device: Device = Depends(authenticate_device),
    session: Session = Depends(get_db),
) -> JSONResponse:
    """
    Принять чанк непрерывного потока и поставить его в обработку.

    Ответ `202 Accepted` означает: чанк принят и поставлен в очередь, детекции появятся
    позже (асинхронно). `503` означает перегрузку — устройство должно повторить отправку.
    """
    settings = get_settings()

    if device_id and device_id != device.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="device_id в форме не совпадает с устройством токена",
        )

    payload = await _read_limited(audio)
    digest = sha256_bytes(payload)

    existing = _find_chunk_by_seq(session, device.id, seq)
    if existing is not None:
        if existing.sha256 and existing.sha256 != digest:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Чанк seq={seq} уже принят с другим содержимым — возможен сбой на устройстве",
            )
        logger.info(
            "Повторная отправка чанка %s (seq=%s, %s) — вернём прежний job_id",
            existing.id,
            seq,
            device.id,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "status": "already_accepted",
                "chunk_id": existing.id,
                "seq": seq,
                "device_id": device.id,
                "accepted_at": (existing.received_at or utcnow()).isoformat(),
            },
        )

    try:
        decoded = decode_chunk(payload, declared_format=(audio.content_type or "").split("/")[-1])
        validate_duration(decoded)
        started = parse_timestamp(started_at, "started_at")
        ended = (
            parse_timestamp(ended_at, "ended_at")
            if ended_at
            else datetime.fromtimestamp(started.timestamp() + decoded.duration_sec, tz=started.tzinfo)
        )
    except (AudioValidationError, ChunkValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    previous = _latest_chunk(session, device.id)
    try:
        timing = analyze_chunk_timing(
            started,
            ended,
            previous=previous,
            device_expected_sec=device.expected_chunk_sec,
        )
    except ChunkValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    det_lat, det_lon = device.location_for_detection(lat, lon)

    chunk = Chunk(
        device_id=device.id,
        seq=seq,
        started_at=timing.started_at,
        ended_at=timing.ended_at,
        duration_sec=timing.duration_sec,
        overlap_with_prev_sec=timing.overlap_with_prev_sec,
        gap_before_sec=timing.gap_before_sec,
        lat=det_lat,
        lon=det_lon,
        size_bytes=len(payload),
        sha256=digest,
        audio_format=decoded.original_format,
        sample_rate=decoded.original_sample_rate,
        received_at=utcnow(),
        status="queued",
    )
    job = Job(id=Job.new_id(), device_id=device.id, status="queued")
    session.add(chunk)
    session.flush()
    job.chunk_id = chunk.id
    session.add(job)

    if timing.gap_event and previous is not None:
        _record_gap_event(session, device, previous, timing, seq)
    if timing.out_of_order:
        logger.warning(
            "Устройство %s прислало чанк seq=%s с временем раньше предыдущего (%s < %s) — порядок нарушен",
            device.id,
            seq,
            timing.started_at.isoformat(),
            previous.started_at.isoformat() if previous else "?",
        )

    from .devices import touch_device_upload

    touch_device_upload(
        session,
        device,
        chunk_started_at=timing.started_at,
        seq=seq,
        remote_addr=request.client.host if request.client else None,
    )

    spool = get_spool()
    try:
        spool.put(job.id, device.id, chunk.id, payload)
    except SpoolFull as exc:
        session.rollback()
        logger.warning("Отказ в приёме чанка %s (seq=%s): %s", device.id, seq, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
            headers={"Retry-After": "10"},
        ) from exc

    session.commit()
    logger.info(
        "Принят чанк %s устройства %s (seq=%s, %.1f с, перекрытие %.1f с, разрыв %.1f с)",
        chunk.id,
        device.id,
        seq,
        timing.duration_sec,
        timing.overlap_with_prev_sec,
        timing.gap_before_sec,
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "status": "accepted",
            "job_id": job.id,
            "chunk_id": chunk.id,
            "seq": seq,
            "device_id": device.id,
            "duration_sec": round(decoded.duration_sec, 3),
            "expected_overlap_sec": settings.expected_overlap_sec,
            "overlap_with_prev_sec": round(timing.overlap_with_prev_sec, 3),
            "gap_before_sec": round(timing.gap_before_sec, 3),
            "accepted_at": utcnow().isoformat(),
        },
    )


@app.get("/ingest/status")
def ingest_status(
    device: Device = Depends(authenticate_device),
    session: Session = Depends(get_db),
) -> dict:
    """
    Состояние потока устройства: последний принятый чанк, очередь, ожидаемый интервал.

    Устройство может сверяться с сервером после перезагрузки и догружать пропущенное.
    """
    spool = get_spool()
    last_chunk = _latest_chunk(session, device.id)
    return {
        "device_id": device.id,
        "last_seq": device.last_seq,
        "last_chunk": last_chunk.to_dict() if last_chunk else None,
        "expected_overlap_sec": get_settings().expected_overlap_sec,
        "chunk_duration_sec": [
            get_settings().chunk_min_sec,
            get_settings().chunk_max_sec,
        ],
        "queue": spool.stats(),
        "server_time": utcnow().isoformat(),
    }


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


async def _read_limited(upload: UploadFile) -> bytes:
    """Прочитать загруженный файл, не выходя за лимит размера чанка."""
    limit = get_settings().max_upload_bytes
    buffer = bytearray()
    while True:
        block = await upload.read(1024 * 256)
        if not block:
            break
        buffer.extend(block)
        if len(buffer) > limit:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Размер чанка превышает лимит {limit} Б (см. PHOENIX_MAX_UPLOAD_BYTES)",
            )
    if not buffer:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Пустой чанк аудио")
    return bytes(buffer)


def _latest_chunk(session: Session, device_id: str) -> Optional[Chunk]:
    return session.execute(
        select(Chunk)
        .where(Chunk.device_id == device_id)
        .order_by(Chunk.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _find_chunk_by_seq(session: Session, device_id: str, seq: int) -> Optional[Chunk]:
    return session.execute(
        select(Chunk).where(Chunk.device_id == device_id, Chunk.seq == seq)
    ).scalar_one_or_none()


def _record_gap_event(session: Session, device: Device, previous: Chunk, timing, seq: int) -> None:
    """Зафиксировать реальный разрыв в потоке (устройство было оффлайн)."""
    gap_end = timing.started_at
    gap_start = previous.ended_at
    duration = timing.gap_before_sec
    session.add(
        DeviceEvent(
            device_id=device.id,
            kind=EVENT_GAP,
            started_at=gap_start,
            ended_at=gap_end,
            duration_sec=duration,
            detail=(
                f"Разрыв связи {duration:.0f} с между чанками seq={previous.seq} и seq={seq} "
                f"(перекрытие {timing.overlap_with_prev_sec:.0f} с)"
            ),
        )
    )
    alert_message(
        f"Устройство {device.name} ({device.id}) не на связи "
        f"с {gap_start.strftime('%H:%M')} до {gap_end.strftime('%H:%M')} UTC — разрыв {duration:.0f} с"
    )
    logger.warning(
        "Устройство %s не на связи %.0f с (пробел наблюдений %s..%s)",
        device.id,
        duration,
        gap_start.isoformat(),
        gap_end.isoformat(),
    )


def alert_message(message: str) -> None:
    """Отправить алерт оператору (по умолчанию — в лог и, если настроено, в файл)."""
    from .alerts import send_alert

    send_alert(message)


def overlap_with_previous(session: Session, device_id: str, chunk: Chunk) -> float:
    """Доля перекрытия чанка с предыдущим (для диагностики протокола)."""
    previous = _latest_chunk_before(session, device_id, chunk)
    if previous is None:
        return 0.0
    return overlap_fraction(
        to_interval(previous.started_at, previous.ended_at),
        to_interval(chunk.started_at, chunk.ended_at),
    )


def _latest_chunk_before(session: Session, device_id: str, chunk: Chunk) -> Optional[Chunk]:
    return session.execute(
        select(Chunk)
        .where(Chunk.device_id == device_id, Chunk.started_at < chunk.started_at)
        .order_by(Chunk.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


# `SpoolItemMissing` импортируется для API-совместимости воркера и тестов.
__all__ = ["app", "ingest_chunk", "ingest_status", "health", "SpoolItemMissing"]