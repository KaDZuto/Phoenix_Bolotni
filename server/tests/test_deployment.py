"""
Тесты файлов развёртывания (Задача 7, td3.md раздел 3).

Compose и Dockerfile не запускаются в тестах — Docker в CI может отсутствовать.
Вместо этого проверяется то, что реально ломает развёртывание и об этом обычно
узнают на этапе деплоя: несогласованные имена переменных между `.env.example`
и `config.py`, забытые тома, сервис, который не на кого смотрит.

Особое внимание — расшифровке `${ПЕРЕМЕННАЯ}`: опечатка в имени там даёт не ошибку,
а молчаливую подстановку пустой строки, и сервис стартует с настройками по умолчанию.
Например, незаполненный `PHOENIX_ADMIN_TOKEN` означает открытый на запись
реестр устройств.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parent

ENV_EXAMPLE = SERVER_DIR / ".env.example"
COMPOSE = SERVER_DIR / "docker-compose.yml"
DOCKERFILE = SERVER_DIR / "Dockerfile"


@pytest.fixture(scope="module")
def env_example() -> dict:
    """Карта `ИМЯ -> значение` из `.env.example` (комментарии и пустые строки убраны)."""
    values: dict = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", stripped)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def config_env_vars() -> set:
    """
    Все `PHOENIX_*`, которые реально читает код пакета.

    Список берётся разбором исходников, а не из полей `Settings`: имена переменных и
    полей не совпадают (`PHOENIX_DATABASE_URL` → `database_url`), а сверять нужно
    именно с тем, что читает код, — иначе проверка превратится в сравнение
    «список со списком» и потеряет смысл.

    `alerts.py` читает `PHOENIX_ALERT_WEBHOOK` из окружения напрямую (у вебхука нет
    значения по умолчанию в `Settings`), поэтому сканируется весь пакет.
    """
    names: set = set()
    for module in sorted((SERVER_DIR).glob("*.py")):
        names |= set(re.findall(r'"(PHOENIX_[A-Z0-9_]+)"', module.read_text(encoding="utf-8")))
    return names


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Согласованность конфигурации
# ---------------------------------------------------------------------------


def test_env_example_covers_every_setting(env_example):
    """
    Каждая настройка, которую читает `config.py`, должна быть в `.env.example`.

    Иначе деплойящий человек не знает, что настройка существует, и оставляет
    значение по умолчанию — обычно это тихая неправильная конфигурация.
    """
    missing = sorted(name for name in config_env_vars() if name not in env_example)
    assert not missing, f"Нет в .env.example: {missing}"


def test_env_example_has_no_python_syntax(env_example):
    """Подстановка в compose идёт из `.env`, а не из Python — синтаксис должен быть POSIX."""
    for name, value in env_example.items():
        assert not value.strip().startswith(("{", "[", "(")), f"{name}: похоже на Python-литерал"
        if value:
            assert value.count('"') % 2 == 0, f"{name}: нечётное число кавычек"


def test_secrets_are_placeholders(env_example):
    """
    Секреты в примере обязаны быть заглушками.

    Реальный токен в закоммиченном `.env.example` — это утечка: его скопируют
    в прод и будут считать, что «уже настроено».
    """
    for name in (
        "PHOENIX_ADMIN_TOKEN",
        "PHOENIX_REGISTRATION_TOKEN",
        "PHOENIX_DASHBOARD_PASSWORD",
    ):
        assert name in env_example, f"{name} не упомянут в .env.example"
        value = env_example[name]
        assert "CHANGE_ME" in value, f"{name} не помечен как заменяемый: {value!r}"


def test_dashboard_auth_is_on_in_example_but_off_for_local_stack(env_example, compose):
    """
    В `.env.example` защита включена, в `docker compose` — выключена, и это объяснено.

    Безопасный вариант (`true`) остаётся значением по умолчанию в примере, чтобы
    перенос `.env` в прод не открыл карту случайно. Локальный стек открывает дашборд,
    потому что иначе нельзя посмотреть на карту сразу после `docker compose up`.
    Обе настройки обязаны быть подписаны: без подписи блок с `false` переставят в бой
    и покажут данные мониторинга редких видов любому, кто дотянется до порта.
    """
    text = ENV_EXAMPLE.read_text(encoding="utf-8").lower()
    assert env_example["PHOENIX_DASHBOARD_AUTH"] == "true"
    assert compose["services"]["api"]["environment"]["PHOENIX_DASHBOARD_AUTH"].startswith(
        "${PHOENIX_DASHBOARD_AUTH:-false}"
    )
    assert "в проде это недопустимо" in text
    assert "phoenix_dashboard_auth=false" in text


def test_debug_audio_is_off_by_default(env_example):
    """Отладочное аудио — единственное место, где байты попадают на диск."""
    assert env_example["PHOENIX_DEBUG_KEEP_AUDIO"] == "false"


def test_inline_worker_default_documented(env_example):
    """
    Воркер внутри API — следствие «аудио не сохраняется», и это обязано быть
    объяснено в самом `.env.example`: иначе кто-то добавит второй воркер «для
    нагрузки» и молча потеряет наблюдения.
    """
    assert env_example["PHOENIX_INLINE_WORKER"] == "true"
    text = ENV_EXAMPLE.read_text(encoding="utf-8").lower()
    assert "expired" in text
    assert "второй" in text or "отдельный процесс" in text


# ---------------------------------------------------------------------------
# docker-compose
# ---------------------------------------------------------------------------


def test_compose_has_required_services(compose):
    assert set(compose["services"]) >= {"db", "api"}


def test_db_is_postgis(compose):
    assert "postgis" in compose["services"]["db"]["image"]


def test_db_healthcheck_waits_for_real_readiness(compose):
    """
    `pg_isready` вместо `pg_ctl`/`test -f`: иначе контейнер БД признаётся готовым,
    пока сервер ещё принимает только локальные подключения, и `api` падает на старте.
    """
    healthcheck = compose["services"]["db"].get("healthcheck")
    assert healthcheck, "У БД нет healthcheck — api будет стартовать вслепую"
    assert any("pg_isready" in str(step) for step in healthcheck["test"])


def test_api_waits_for_db(compose):
    depends = compose["services"]["api"]["depends_on"]
    assert depends["db"]["condition"] == "service_healthy"


def test_api_binds_loopback_by_default(compose):
    """
    Локальный стек по умолчанию слушает только loopback.

    Приём загрузок наружу — отдельное решение с TLS и релеем, а не побочный эффект
    `docker compose up`. Значение вынесено в переменную, но её умолчание —
    именно loopback, поэтому проверяется оно, а не факт подстановки.
    """
    port = compose["services"]["api"]["ports"][0]
    assert "127.0.0.1" in port, port
    # Подставлять адрес должен переменной, а не жёстко зашитым значением.
    assert "${PHOENIX_BIND:-127.0.0.1}" in port, port


def test_api_publishes_only_http_port(compose):
    """Наружу открывается только приём данных; БД и отладочные порты не публикуются."""
    api_env = compose["services"]["api"]["environment"]
    assert "PHOENIX_DATABASE_URL" in api_env
    assert "db:5432" in api_env["PHOENIX_DATABASE_URL"]


def test_compose_interpolations_are_known(env_example):
    """
    Все `${ПЕРЕМЕННАЯ}` в compose должны быть из `.env.example` или иметь значение
    по умолчанию.

    Опечатка здесь не даёт ошибку — compose подставляет пустую строку, и сервис
    молча стартует с настройками по умолчанию.
    """
    text = COMPOSE.read_text(encoding="utf-8")
    names = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", text))
    for name in names:
        has_default = bool(re.search(rf"\$\{{{name}:[^}}]", text))
        assert has_default or name in env_example, f"{name}: нет ни в .env.example, ни значения по умолчанию"


def test_compose_db_credentials_match(env_example, compose):
    """Креды БД в `.env.example` и в строке подключения compose должны совпадать."""
    db_env = compose["services"]["db"]["environment"]
    assert db_env["POSTGRES_USER"] == "${POSTGRES_USER:-phoenix}"
    assert "postgresql+psycopg://" in compose["services"]["api"]["environment"]["PHOENIX_DATABASE_URL"]


def test_model_repo_is_mounted_readonly(compose):
    """
    ONNX-модель приходит из репозитория модели монтированием, а не копируется в образ.

    `models/bird_model.onnx` в git хранится указателем git-LFS, поэтому `COPY` в образ
    дал бы 133-байтный файл-заглушку, и воркер падал бы на первой загрузке.
    Монтирование только на чтение не даёт сервису испортить репозиторий модели.
    """
    volumes = compose["services"]["api"]["volumes"]
    model_mounts = [v for v in volumes if "model" in v.lower()]
    assert model_mounts, "Репозиторий модели не смонтирован"
    assert all(v.endswith(":ro") for v in model_mounts), model_mounts


def test_code_is_not_baked_into_a_volume(compose):
    """Код в образе, а не в томе: правка .py не должна требовать перезапуска контейнера."""
    volumes = compose["services"]["api"]["volumes"]
    assert not any("/app/server" in v and not v.startswith("phoenix") for v in volumes), volumes


def test_data_volume_is_persistent(compose):
    """SQLite dev-файл, журнал алертов и отладочное аудио — в именованном томе."""
    volumes = compose["services"]["api"]["volumes"]
    assert any("/app/server/data" in v for v in volumes)
    assert "phoenix-data" in compose["volumes"]


def test_db_data_is_persistent(compose):
    assert "pgdata" in compose["volumes"]
    mounts = compose["services"]["db"]["volumes"]
    assert any("pgdata" in v for v in mounts)


def test_alerts_service_is_separate_from_api(compose):
    """
    Мониторинг разрывов вынесен из процесса API.

    Он не держит модель в памяти, зато нужен постоянно: упавший вместе с API мониторинг
    означал бы, что о пропавших микрофонах никто не узнает.
    """
    alerts = compose["services"]["alerts"]
    assert "server.alerts" in " ".join(alerts["command"])
    assert alerts["depends_on"]["db"]["condition"] == "service_healthy"
    # Токен доступа в мониторинге не нужен: он не обращается к административному API.
    assert "PHOENIX_ADMIN_TOKEN" not in alerts["environment"]


def test_services_restart_unless_stopped(compose):
    """Микрофон сядет и потеряет наблюдения, если сервис не поднимется после сбоя."""
    for name in ("db", "api", "alerts"):
        assert compose["services"][name].get("restart") == "unless-stopped", name


def test_services_have_memory_limits(compose):
    """
    Потолок памяти обязателен: spool растёт до `PHOENIX_SPOOL_MAX_BYTES`, и без лимита
    при перегрузке OOM-киллер убьёт контейнер, а не вернёт устройству честный 503.
    """
    limits = compose["services"]["api"].get("deploy", {}).get("resources", {}).get("limits", {})
    assert "memory" in limits


# ---------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------


def test_dockerfile_builds_from_repo_root():
    """
    Контекст сборки — корень репозитория, а не `server/`.

    Образу нужен и код сервера, и репозиторий модели (`inference.py`,
    `model_config.json`, `models/`, `species/`), который подключается как внешняя
    библиотека. С контекстом `server/` сборка падала бы на первом `COPY`.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY models/ /app/models/" in text
    assert "COPY species/ /app/species/" in text
    assert "COPY server/ /app/server/" in text
    assert "inference.py" in text


def test_dockerfile_installs_model_dependencies():
    """Без `libsndfile1` soundfile не сможет декодировать чанк, без ffmpeg — mp3/m4a."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "libsndfile1" in text
    assert "ffmpeg" in text
    assert "requirements.txt" in text
    assert "server/requirements.txt" in text


def test_dockerfile_runs_unprivileged():
    """Процесс не должен иметь прав на запись в код и конфигурацию."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "useradd" in text
    assert "USER phoenix" in text


def test_dockerfile_sets_model_paths():
    text = DOCKERFILE.read_text(encoding="utf-8")
    for var in ("PHOENIX_MODEL", "PHOENIX_CLASSES", "PHOENIX_MODEL_CONFIG", "PHOENIX_WHITELIST"):
        assert f"{var}=/app/" in text, f"{var} не задан в образе"


def test_dockerfile_healthcheck_uses_local_endpoint():
    """
    Healthcheck ходит на `127.0.0.1`, а не на имя сервиса `api`.

    Иначе каждый контейнер в сети проверял бы доступность соседа, а не свой процесс.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "127.0.0.1:8000/health" in text


# ---------------------------------------------------------------------------
# Связь с кодом
# ---------------------------------------------------------------------------


def test_compose_env_vars_are_read_by_settings(compose):
    """
    Каждая переменная `PHOENIX_*` в compose действительно читается `config.py`.

    Опечатка в ключе не даёт ошибки — настройка просто молча не применится.
    """
    known = config_env_vars()
    # `PHOENIX_ALERT_LOG` в compose указывает внутрь контейнера, а не путь с хоста.
    assert known
    for service in compose["services"].values():
        for key in service.get("environment", {}):
            if key.startswith("PHOENIX_"):
                assert key in known, f"{key} не читается ни одним полем Settings"


def test_inline_worker_defaults_to_true(monkeypatch):
    """
    Воркер внутри процесса API — значение по умолчанию.

    Иначе второй контейнер воркера не найдёт байты в своей памяти и пометит
    задания как `expired`, то есть наблюдения будут теряться молча.

    Переменная снята явно: autouse-фикстура conftest выставляет `false`, чтобы тесты
    сами управляли воркером, и без `del` проверялось бы не то значение.
    """
    from server import config as config_module

    monkeypatch.delenv("PHOENIX_INLINE_WORKER", raising=False)
    config_module.reload_settings()
    try:
        assert config_module.get_settings().inline_worker is True
    finally:
        config_module.reload_settings()