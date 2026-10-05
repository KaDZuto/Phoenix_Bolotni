"""
Конфигурация серверного распознавателя Phoenix_Bolotni.

Все параметры берутся из переменных окружения (префикс `PHOENIX_`), поэтому
docker-compose и systemd могут переопределять их без правки кода.
Значения по умолчанию подобраны так, чтобы стек поднимался локально одной
командой `docker compose up` (см. `server/docker-compose.yml`).

Параметры препроцессинга и многократной проверки НЕ дублируются здесь — они
читаются из единого `model_config.json` репозитория модели (см. `confirmation.py`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

SERVER_DIR = Path(__file__).resolve().parent
REPO_ROOT = SERVER_DIR.parent


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_opt(name: str) -> Optional[str]:
    value = os.environ.get(name)
    return value if value not in (None, "") else None


def _env_int(name: str, default: int) -> int:
    value = _env_opt(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:  # pragma: no cover - защита от мусора в .env
        raise ValueError(f"Переменная {name} должна быть целым числом, получено: {value!r}") from exc


def _env_float(name: str, default: float) -> float:
    value = _env_opt(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:  # pragma: no cover
        raise ValueError(f"Переменная {name} должна быть числом, получено: {value!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    value = _env_opt(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "да"}


@dataclass(frozen=True)
class Settings:
    """Снимок конфигурации сервиса."""

    # --- БД и geo ---
    # Прод: postgresql+psycopg://user:pass@host:5432/db (+ PostGIS, см. alembic).
    # Dev/тесты по умолчанию: локальный SQLite-файл без PostGIS.
    database_url: str
    database_echo: bool

    # --- Модель и препроцессинг (пути до репозитория модели) ---
    model_config_path: Path
    classes_path: Path
    whitelist_path: Path
    model_path: Path
    use_tta: bool
    store_raw_windows: bool

    # --- Регистрация устройств (Задача 2) ---
    admin_token: Optional[str]
    registration_token: Optional[str]
    default_expected_chunk_sec: float

    # --- Протокол непрерывной загрузки чанков (Задача 3) ---
    chunk_min_sec: float
    chunk_max_sec: float
    expected_overlap_sec: float
    max_upload_bytes: int
    accepted_audio_formats: tuple

    # --- Очередь и spool аудио в оперативной памяти (без записи на диск) ---
    spool_max_bytes: int
    spool_ttl_sec: float
    worker_poll_interval_sec: float
    worker_batch_size: int
    #: Гонять воркер внутри процесса API. По умолчанию включено: байты чанка лежат
    #: в памяти принявшего их процесса, поэтому отдельный процесс воркера не сможет
    #: их забрать и пометит задание как `expired` (тихая потеря наблюдения).
    #: Выключать только если очередь вынесена в общее хранилище.
    inline_worker: bool
    inline_worker_id: str

    # --- Дедупликация детекций на границах перекрывающихся чанков ---
    dedup_overlap_frac: float

    # --- Мониторинг разрывов связи ---
    gap_alert_sec: float
    gap_monitor_interval_sec: float
    alert_log_path: Optional[Path]
    max_clock_skew_past_sec: float
    max_clock_ahead_sec: float

    # --- Отладочное временное хранение аудио (P2, выключено по умолчанию) ---
    debug_keep_audio: bool
    debug_keep_last_chunks: int
    debug_keep_ttl_sec: float

    # --- Доступ к дашборду карты (Задача 5) ---
    dashboard_auth_enabled: bool
    dashboard_user: str
    dashboard_password: str
    cors_origins: tuple

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Кэшированная конфигурация (тесты вызывают `reload_settings`)."""
    formats = tuple(
        f.strip().lstrip(".")
        for f in _env_str("PHOENIX_AUDIO_FORMATS", "wav,flac,ogg,mp3,m4a").split(",")
        if f.strip()
    )
    return Settings(
        database_url=_env_str("PHOENIX_DATABASE_URL", f"sqlite:///{SERVER_DIR / 'data' / 'phoenix.db'}"),
        database_echo=_env_bool("PHOENIX_DATABASE_ECHO", False),
        model_config_path=Path(_env_str("PHOENIX_MODEL_CONFIG", REPO_ROOT / "model_config.json")),
        classes_path=Path(_env_str("PHOENIX_CLASSES", REPO_ROOT / "models" / "classes.json")),
        whitelist_path=Path(
            _env_str("PHOENIX_WHITELIST", REPO_ROOT / "species" / "ru_birds_whitelist.json")
        ),
        model_path=Path(_env_str("PHOENIX_MODEL", REPO_ROOT / "models" / "bird_model.onnx")),
        use_tta=_env_bool("PHOENIX_USE_TTA", True),
        store_raw_windows=_env_bool("PHOENIX_STORE_RAW_WINDOWS", False),
        admin_token=_env_opt("PHOENIX_ADMIN_TOKEN"),
        registration_token=_env_opt("PHOENIX_REGISTRATION_TOKEN") or _env_opt("PHOENIX_ADMIN_TOKEN"),
        default_expected_chunk_sec=_env_float("PHOENIX_DEFAULT_EXPECTED_CHUNK_SEC", 60.0),
        chunk_min_sec=_env_float("PHOENIX_CHUNK_MIN_SEC", 20.0),
        chunk_max_sec=_env_float("PHOENIX_CHUNK_MAX_SEC", 90.0),
        expected_overlap_sec=_env_float("PHOENIX_EXPECTED_OVERLAP_SEC", 5.0),
        max_upload_bytes=_env_int("PHOENIX_MAX_UPLOAD_BYTES", 32 * 1024 * 1024),
        accepted_audio_formats=formats,
        spool_max_bytes=_env_int("PHOENIX_SPOOL_MAX_BYTES", 256 * 1024 * 1024),
        spool_ttl_sec=_env_float("PHOENIX_SPOOL_TTL_SEC", 300.0),
        worker_poll_interval_sec=_env_float("PHOENIX_WORKER_POLL_INTERVAL_SEC", 0.25),
        worker_batch_size=_env_int("PHOENIX_WORKER_BATCH_SIZE", 1),
        inline_worker=_env_bool("PHOENIX_INLINE_WORKER", True),
        inline_worker_id=_env_str("PHOENIX_INLINE_WORKER_ID", "inline-1"),
        dedup_overlap_frac=_env_float("PHOENIX_DEDUP_OVERLAP_FRAC", 0.5),
        gap_alert_sec=_env_float("PHOENIX_GAP_ALERT_SEC", 180.0),
        gap_monitor_interval_sec=_env_float("PHOENIX_GAP_MONITOR_INTERVAL_SEC", 60.0),
        alert_log_path=(
            Path(_env_str("PHOENIX_ALERT_LOG", "")) if _env_opt("PHOENIX_ALERT_LOG") else None
        ),
        max_clock_skew_past_sec=_env_float("PHOENIX_MAX_CLOCK_SKEW_PAST_SEC", 120.0),
        max_clock_ahead_sec=_env_float("PHOENIX_MAX_CLOCK_AHEAD_SEC", 300.0),
        debug_keep_audio=_env_bool("PHOENIX_DEBUG_KEEP_AUDIO", False),
        debug_keep_last_chunks=_env_int("PHOENIX_DEBUG_KEEP_LAST_CHUNKS", 5),
        debug_keep_ttl_sec=_env_float("PHOENIX_DEBUG_KEEP_TTL_SEC", 300.0),
        dashboard_auth_enabled=_env_bool("PHOENIX_DASHBOARD_AUTH", True),
        dashboard_user=_env_str("PHOENIX_DASHBOARD_USER", "ecologist"),
        dashboard_password=_env_str("PHOENIX_DASHBOARD_PASSWORD", "phoenix"),
        cors_origins=tuple(
            o.strip() for o in _env_str("PHOENIX_CORS_ORIGINS", "").split(",") if o.strip()
        ),
    )


def reload_settings() -> Settings:
    """Сбросить кэш конфигурации (используется в тестах)."""
    get_settings.cache_clear()
    return get_settings()