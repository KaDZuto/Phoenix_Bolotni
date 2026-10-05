"""
Тесты административного CLI (`server/cli.py`).

CLI — единственная точка, где микрофон регистрируют без HTTP, поэтому ошибка здесь
(например, потерянный токен или молчаливое «устройство не найдено») приводит к
часу потерянного наблюдения. Поэтому проверяем именно то, что опасно: коды выхода,
однократность токена и отсутствие побочных эффектов при ошибке.
"""

from __future__ import annotations

import json

import pytest

from server.cli import main
from server.devices import get_device
from server.models import Device
from server.tests.conftest import open_session


def run(capsys, *argv: str) -> tuple[int, str, str]:
    """Запустить команду CLI и вернуть (код возврата, stdout, stderr)."""
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# help и разбор аргументов
# ---------------------------------------------------------------------------


def test_help_lists_all_command_groups(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for group in ("device", "gaps", "sweep", "worker", "status", "summary", "species", "db"):
        assert group in out


@pytest.mark.parametrize(
    "subargv",
    [
        ["device", "--help"],
        ["device", "add", "--help"],
        ["db", "--help"],
        ["worker", "--help"],
    ],
)
def test_subcommand_help_is_parsable(capsys, subargv):
    with pytest.raises(SystemExit) as exit_info:
        main(subargv)
    assert exit_info.value.code == 0


# ---------------------------------------------------------------------------
# Регистрация устройств
# ---------------------------------------------------------------------------


def test_device_add_prints_token_and_creates_row(capsys):
    code, out, _ = run(
        capsys,
        "device",
        "add",
        "--name",
        "Пойма у моста",
        "--lat",
        "59.90",
        "--lon",
        "41.10",
        "--owner",
        "Иван",
    )
    assert code == 0
    assert "Токен показывается один раз" in out
    assert "Authorization: Bearer pbk_" in out

    session = open_session()
    devices = session.query(Device).all()
    assert len(devices) == 1
    assert devices[0].name == "Пойма у моста"
    assert devices[0].owner == "Иван"
    assert abs(devices[0].lat - 59.90) < 1e-6
    # Токен в БД не хранится — только хеш.
    assert devices[0].token_hash and devices[0].token_hash not in out


def test_device_add_without_coordinates_for_stationary_fails(capsys):
    """Стационарный микрофон без точки на карте — бессмысленная запись, отказ ожидаем."""
    code, _, err = run(capsys, "device", "add", "--name", "Без координат")
    assert code == 2
    assert "lat/lon" in err

    session = open_session()
    assert session.query(Device).count() == 0


def test_device_add_mobile_allows_missing_coordinates(capsys):
    """У телефона координаты приходят с каждым чанком — настройка без точки допустима."""
    code, _, _ = run(capsys, "device", "add", "--name", "Телефон", "--type", "mobile")
    assert code == 0
    session = open_session()
    assert session.query(Device).one().device_type == "mobile"


def test_device_add_invalid_coordinates_fails(capsys):
    code, _, err = run(
        capsys, "device", "add", "--name", "Ошибка", "--lat", "999", "--lon", "41.10"
    )
    assert code == 2
    assert err.strip()


def test_device_list_is_readable_table(capsys):
    run(capsys, "device", "add", "--name", "Первый", "--lat", "59.9", "--lon", "41.1")
    run(capsys, "device", "add", "--name", "Второй", "--type", "mobile")
    code, out, _ = run(capsys, "device", "list")

    assert code == 0
    lines = [line for line in out.splitlines() if line.strip()]
    # Заголовок + разделитель + два устройства + итоговая строка.
    assert len(lines) == 5
    assert set(lines[1]) == {"-"}
    assert lines[2].startswith("dev_") and lines[3].startswith("dev_")
    assert "Всего устройств: 2" in out
    # Колонки не слипаются: `device_id` шириной ровно 16 символов, и без запаса
    # id склеился бы с именем (так и было в первой версии CLI).
    assert lines[2][16] == " " and lines[2][:16].startswith("dev_")


def test_device_list_json_is_parseable(capsys):
    run(capsys, "device", "add", "--name", "Первый", "--lat", "59.9", "--lon", "41.1")
    code, out, _ = run(capsys, "device", "list", "--json")
    assert code == 0
    data = json.loads(out)
    assert data[0]["name"] == "Первый"
    # Полный токен утекать не должен: в БД лежит только хеш и 12-символьный префикс
    # (по нему оператор опознаёт устройство в логах, не раскрывая сам секрет).
    assert "token_hash" not in data[0]
    assert len(data[0]["token_prefix"]) == 12


def test_device_list_empty_is_friendly(capsys):
    code, out, _ = run(capsys, "device", "list")
    assert code == 0
    assert "Устройств нет" in out


def test_device_show_includes_detection_count(capsys):
    _, out, _ = run(capsys, "device", "add", "--name", "Первый", "--lat", "59.9", "--lon", "41.1")
    device_id = [line for line in out.splitlines() if "device_id" in line][0].split(":")[1].strip()

    code, out, _ = run(capsys, "device", "show", device_id)
    assert code == 0
    data = json.loads(out)
    assert data["id"] == device_id
    assert data["detections_total"] == 0


def test_device_show_unknown_id_fails(capsys):
    code, _, err = run(capsys, "device", "show", "dev_ffffffffffff")
    assert code == 2
    assert "не найдено" in err


def test_device_update_rejects_half_coordinates(capsys):
    _, out, _ = run(capsys, "device", "add", "--name", "Первый", "--lat", "59.9", "--lon", "41.1")
    device_id = [line for line in out.splitlines() if "device_id" in line][0].split(":")[1].strip()

    code, _, err = run(capsys, "device", "update", device_id, "--lat", "60.0")
    assert code == 2
    assert "парой" in err
    # Точка не должна была поменяться «наполовину».
    session = open_session()
    assert abs(get_device(session, device_id).lat - 59.9) < 1e-6


def test_device_rotate_token_invalidates_previous(capsys):
    _, out, _ = run(capsys, "device", "add", "--name", "Первый", "--lat", "59.9", "--lon", "41.1")
    device_id = [line for line in out.splitlines() if "device_id" in line][0].split(":")[1].strip()
    session = open_session()
    old_hash = get_device(session, device_id).token_hash

    code, out, _ = run(capsys, "device", "update", device_id, "--rotate-token")
    assert code == 0
    assert "Новый токен" in out
    assert get_device(session, device_id).token_hash != old_hash


def test_device_state_off_then_on_logs_events(capsys):
    _, out, _ = run(capsys, "device", "add", "--name", "Первый", "--lat", "59.9", "--lon", "41.1")
    device_id = [line for line in out.splitlines() if "device_id" in line][0].split(":")[1].strip()

    assert run(capsys, "device", "state", device_id, "--off")[0] == 0
    session = open_session()
    assert get_device(session, device_id).is_active is False

    assert run(capsys, "device", "state", device_id, "--on")[0] == 0
    assert get_device(session, device_id).is_active is True


# ---------------------------------------------------------------------------
# Разрывы, очередь, сводка
# ---------------------------------------------------------------------------


def test_gaps_on_fresh_network_is_quiet(capsys):
    run(capsys, "device", "add", "--name", "Первый", "--lat", "59.9", "--lon", "41.1")
    code, out, _ = run(capsys, "gaps")
    assert code == 0
    assert "Разрывов нет" in out


def test_gaps_json_has_expected_keys(capsys):
    run(capsys, "device", "add", "--name", "Первый", "--lat", "59.9", "--lon", "41.1")
    code, out, _ = run(capsys, "gaps", "--json")
    assert code == 0
    data = json.loads(out)
    assert set(data) == {"generated_at", "devices_checked", "stale_devices", "gap_events"}


def test_sweep_ignores_never_seen_device(capsys):
    """
    Устройство, которое ещё ни разу не прислало чанк, разрывом не считается.

    Иначе каждое только что зарегистрированное устройство немедленно попадало бы в
    алерты. Для такого случая есть отдельный статус `never_seen` в `/api/devices`.
    """
    run(capsys, "device", "add", "--name", "Ещё не вышел на связь", "--lat", "59.9", "--lon", "41.1")
    code, out, _ = run(capsys, "sweep", "--no-notify")
    assert code == 0
    assert "Все активные устройства на связи" in out


def test_sweep_records_gap_for_silent_device(capsys):
    """Устройство, которое замолчало после первой загрузки, обязано попасть в разрыв."""
    _, out, _ = run(capsys, "device", "add", "--name", "Молчун", "--lat", "59.9", "--lon", "41.1")
    device_id = [line for line in out.splitlines() if "device_id" in line][0].split(":")[1].strip()

    # Отмечаем последнюю активность в прошлом: имитируем поток, который встал.
    from datetime import timedelta

    from server.models import utcnow

    session = open_session()
    device = session.get(Device, device_id)
    device.last_seen_at = utcnow() - timedelta(seconds=900)
    session.commit()
    session.close()

    code, out, _ = run(capsys, "sweep", "--no-notify")
    assert code == 0
    assert "Молчающих устройств: 1" in out

    # Второй запуск не должен плодить дубликаты событий.
    run(capsys, "sweep", "--no-notify")
    code, out, _ = run(capsys, "gaps", "--json")
    assert len(json.loads(out)["gap_events"]) == 1


def test_status_reports_queue_and_memory(capsys):
    code, out, _ = run(capsys, "status")
    assert code == 0
    assert "Устройств:" in out
    assert "В очереди на разбор:" in out
    assert "Аудио в памяти:" in out


def test_summary_on_empty_db_explains_itself(capsys):
    code, out, _ = run(capsys, "summary", "--days", "1")
    assert code == 0
    assert "Детекций нет" in out


def test_species_catalog_marks_model_classes(capsys):
    """
    Справочник — весь whitelist (136 видов), но флаг `in_model` отделяет 16 классов модели.

    Эколог смотрит на карту и должен видеть потенциальную фауну площадки, а не только
    то, что модель уже умеет: иначе пустые виды в UI выглядят как «не встречается».
    """
    code, out, _ = run(capsys, "species", "--json")
    assert code == 0
    catalog = json.loads(out)
    assert len(catalog) == 136
    assert len([item for item in catalog if item["in_model"]]) == 16
    assert all(item["ru"] and item["slug"] and item["habitat"] for item in catalog)
    # Каталог обязан совпадать с тем, что отдаёт API карты, иначе легенда и справочник
    # разойдутся по числу видов.
    from server.species import get_species_registry

    assert catalog == get_species_registry().catalog()


def test_species_text_output_notes_star(capsys):
    code, out, _ = run(capsys, "species")
    assert code == 0
    assert "Распознаёт модель:              16 классов" in out
    assert "распознаётся моделью" in out


# ---------------------------------------------------------------------------
# Миграции
# ---------------------------------------------------------------------------


def test_db_upgrade_then_check(capsys):
    """Главный сценарий релиза: накатить миграции и убедиться, что модели совпадают."""
    assert run(capsys, "db", "upgrade")[0] == 0
    code, out, _ = run(capsys, "db", "check")
    assert code == 0
    assert "Миграции соответствуют моделям" in out


def test_db_upgrade_is_idempotent(capsys):
    run(capsys, "db", "upgrade")
    code, out, _ = run(capsys, "db", "upgrade")
    assert code == 0
    assert "Миграции применены" in out


def test_migrations_do_not_silence_application_logs(capsys):
    """
    Запуск миграций не должен глушить логи приложения.

    `logging.fileConfig` в `alembic/env.py` по умолчанию выключает все логгеры,
    которых нет в `alembic.ini`. Из-за этого после `db upgrade` логгер `server.*`
    замолкал навсегда, и вместе с ним — алерты о разрывах связи, то есть
    оператор переставал получать самое важное предупреждение.
    """
    import logging

    assert logging.getLogger("server.alerts").disabled is False
    assert run(capsys, "db", "upgrade")[0] == 0
    # `logging` не умеет включить логгер обратно, поэтому проверка однозначна:
    # после миграций он обязан остаться рабочим.
    logger = logging.getLogger("server.alerts")
    assert logger.disabled is False
    # Алерт о разрыве связи пишется на уровне WARNING — он обязан доходить.
    assert logger.getEffectiveLevel() <= logging.WARNING


def test_db_current_reports_revision(tmp_path):
    """
    `db current` печатает ревизию в stdout через собственный вывод alembic.

    Поток перехватывается вручную: `capsys.readouterr()` закрывает свой буфер, а
    alembic схватил ссылку на тот же объект и падает с «I/O operation on closed file».
    """
    import io
    import os
    from contextlib import redirect_stdout

    from server import config as config_module
    from server import db as db_module

    os.environ["PHOENIX_DATABASE_URL"] = f"sqlite:///{tmp_path / 'cli.db'}"
    config_module.reload_settings()
    db_module.reset_engine()
    try:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            main(["db", "upgrade"])
            code = main(["db", "current"])
        assert code == 0
        assert "Current revision" in buffer.getvalue()
    finally:
        db_module.reset_engine()
        config_module.reload_settings()


def test_worker_once_on_empty_queue_succeeds(capsys):
    code, out, _ = run(capsys, "worker", "--once")
    assert code == 0
    assert "Обработано чанков: 0" in out