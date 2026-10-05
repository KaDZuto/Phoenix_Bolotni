"""
Административный CLI серверного распознавателя (`server/cli.py`).

Нужен там, где HTTP-API неудобен или недоступен: первичная регистрация микрофона
прямо на сервере без открытых административных портов, разбор разрывов связи,
разовая обработка очереди, миграции.

    python -m server.cli device add --name "Пойма №1" --lat 59.90 --lon 41.10
    python -m server.cli device list
    python -m server.cli gaps
    python -m server.cli worker --once
    python -m server.cli db upgrade
    python -m server.cli summary --days 7

Команды работают напрямую с БД (без HTTP), поэтому должны выполняться на приватной
стороне — на той же машине, где живёт воркер. В БД лежат только SHA-256-хеши
токенов, поэтому выданный токен здесь же и печатается: повторно его не увидеть.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import timedelta
from typing import List, Optional

from .alerts import build_gap_report, find_stale_devices, record_stale_devices
from .analytics import DetectionFilter, hourly_histogram, species_summary
from .db import session_scope
from .devices import (
    DeviceValidationError,
    get_device,
    list_devices,
    register_device,
    set_device_active,
    update_device,
)
from .models import JOB_FAILED, JOB_QUEUED, Detection, Device, Job, utcnow

logger = logging.getLogger("phoenix.cli")


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------------------
# Устройства
# ---------------------------------------------------------------------------


def _cmd_device_add(args: argparse.Namespace) -> int:
    """Зарегистрировать устройство и показать выданный токен (показывается один раз)."""
    with session_scope() as session:
        try:
            registered = register_device(
                session,
                name=args.name,
                owner=args.owner,
                contact=args.contact,
                device_type=args.type,
                lat=args.lat,
                lon=args.lon,
                altitude_m=args.altitude_m,
                place_note=args.place_note,
                expected_chunk_sec=args.expected_chunk_sec,
                note=args.note,
            )
        except DeviceValidationError as exc:
            print(f"Ошибка регистрации: {exc}", file=sys.stderr)
            return 2

        device_id = registered.device.id

    # Токен напечатан после выхода из контекста: если транзакция не прошла,
    # «выданный» токен не должен остаться в выводе оператора.
    print("Устройство зарегистрировано:")
    print(f"  device_id: {device_id}")
    print(f"  токен:     {registered.token}")
    print()
    print("Токен показывается один раз — в БД хранится только его SHA-256-хеш.")
    print(f"Заголовок при загрузке:  Authorization: Bearer {registered.token}")
    if registered.device.lat is not None:
        print(f"Точка на карте: {registered.device.lat}, {registered.device.lon}")
    return 0


def _cmd_device_list(args: argparse.Namespace) -> int:
    with session_scope() as session:
        devices = list_devices(session, active_only=args.active_only)
        if args.json:
            _print_json([device.to_dict() for device in devices])
            return 0
        if not devices:
            print("Устройств нет. Зарегистрируйте: python -m server.cli device add --name ...")
            return 0

        # Ширины подобраны по фактической длине полей: `device_id` вида `dev_4270af4f4314`
    # занимает ровно 16 символов, и без запаса колонки слипались бы.
    id_col, name_col, type_col, coord_col, state_col = 18, 26, 11, 22, 14
    header = (
        f"{'device_id':<{id_col}}{'имя':<{name_col}}{'тип':<{type_col}}"
        f"{'координаты':<{coord_col}}{'состояние':<{state_col}}последние данные"
    )
    print(header)
    print("-" * len(header))
    for device in devices:
        coords = (
            f"{device.lat:.5f}, {device.lon:.5f}"
            if device.lat is not None and device.lon is not None
            else "—"
        )
        last_seen = (
            device.last_seen_at.isoformat(timespec="seconds") if device.last_seen_at else "—"
        )
        state = "на связи" if device.is_active else "отключено"
        print(
            f"{device.id:<{id_col}}{device.name[: name_col - 1]:<{name_col}}"
            f"{device.device_type:<{type_col}}{coords:<{coord_col}}"
            f"{state:<{state_col}}{last_seen}"
        )
    print(f"\nВсего устройств: {len(devices)}")
    return 0


def _cmd_device_show(args: argparse.Namespace) -> int:
    with session_scope() as session:
        device = get_device(session, args.device_id)
        if device is None:
            print(f"Устройство {args.device_id} не найдено", file=sys.stderr)
            return 2
        data = device.to_dict()
        data["detections_total"] = (
            session.query(Detection).filter(Detection.device_id == device.id).count()
        )
        _print_json(data)
    return 0


def _cmd_device_update(args: argparse.Namespace) -> int:
    if (args.lat is None) != (args.lon is None):
        print("Координаты меняются парой: укажите --lat и --lon одновременно.", file=sys.stderr)
        return 2

    with session_scope() as session:
        device = get_device(session, args.device_id)
        if device is None:
            print(f"Устройство {args.device_id} не найдено", file=sys.stderr)
            return 2
        try:
            token = update_device(
                session,
                device,
                name=args.name,
                owner=args.owner,
                contact=args.contact,
                lat=args.lat,
                lon=args.lon,
                altitude_m=args.altitude_m,
                place_note=args.place_note,
                expected_chunk_sec=args.expected_chunk_sec,
                note=args.note,
                rotate_token=args.rotate_token,
            )
        except DeviceValidationError as exc:
            print(f"Ошибка обновления: {exc}", file=sys.stderr)
            return 2

    print(f"Устройство {args.device_id} обновлено.")
    if token:
        print(f"Новый токен (показывается один раз): {token}")
    return 0


def _cmd_device_state(args: argparse.Namespace) -> int:
    with session_scope() as session:
        device = get_device(session, args.device_id)
        if device is None:
            print(f"Устройство {args.device_id} не найдено", file=sys.stderr)
            return 2
        set_device_active(session, device, active=args.state, reason=args.reason or "")
        state = "включено" if device.is_active else "деактивировано"
        print(f"Устройство {args.device_id}: {state}.")
        if not device.is_active:
            print("Загрузки от него будут отклоняться (401).")
    return 0


# ---------------------------------------------------------------------------
# Разрывы связи
# ---------------------------------------------------------------------------


def _cmd_gaps(args: argparse.Namespace) -> int:
    with session_scope() as session:
        report = build_gap_report(session, notify=args.notify)

    if args.json:
        _print_json(report.to_dict())
        return 0

    print(f"Проверено устройств: {report.devices_checked}")
    if not report.stale and not report.gap_events:
        print("Разрывов нет — сеть отдаёт непрерывный поток.")
        return 0
    if report.stale:
        print(f"\nНе на связи ({len(report.stale)}):")
        for item in report.stale:
            print(f"  {item['device_id']}: {item['message']}")
    if report.gap_events:
        print(f"\nЗафиксированные разрывы за сутки ({len(report.gap_events)}):")
        for event in report.gap_events:
            detail = event.get("detail") or event.get("kind")
            print(f"  {event['created_at']} {event['device_id']}: {detail}")
    return 0


def _cmd_sweep(args: argparse.Namespace) -> int:
    """Разово зафиксировать «молчащие» устройства (для cron/systemd timer)."""
    with session_scope() as session:
        stale = find_stale_devices(session)
        events = record_stale_devices(session, stale, notify=not args.no_notify)

    if not stale:
        print("Все активные устройства на связи.")
        return 0
    print(f"Молчающих устройств: {len(stale)}, записано событий: {len(events)}")
    for item in stale:
        print(f"  {item['device_id']}: {item['message']}")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """Фоновый мониторинг разрывов."""
    from .alerts import watch_loop

    interval = args.interval
    print(
        "Мониторинг разрывов запущен"
        + (f" (интервал {interval:.0f} с)" if interval else "")
        + ", Ctrl+C для остановки."
    )
    try:
        watch_loop(interval_sec=interval, notify=not args.no_notify)
    except KeyboardInterrupt:  # pragma: no cover - интерактивно
        print("\nОстановлено.")
    return 0


# ---------------------------------------------------------------------------
# Очередь и состояние
# ---------------------------------------------------------------------------


def _cmd_worker(args: argparse.Namespace) -> int:
    from .queue_worker import main as worker_main

    argv: List[str] = []
    if args.once:
        argv.append("--once")
    if args.limit is not None:
        argv += ["--limit", str(args.limit)]
    if args.worker_id:
        argv += ["--worker-id", args.worker_id]
    argv += ["--log-level", args.log_level]
    return worker_main(argv)


def _cmd_status(args: argparse.Namespace) -> int:
    """Состояние очереди и памяти — первое, что стоит проверить при «что-то сломалось»."""
    from .spool import get_spool

    with session_scope() as session:
        pending = session.query(Job).filter(Job.status == JOB_QUEUED).count()
        failed = session.query(Job).filter(Job.status == JOB_FAILED).count()
        detections = session.query(Detection).count()
        devices_total = session.query(Device).count()
        devices_active = session.query(Device).filter(Device.is_active.is_(True)).count()

    stats = get_spool().stats()
    print(f"Устройств:            {devices_total} (активных {devices_active})")
    print(f"В очереди на разбор:  {pending}")
    print(f"Заданий с ошибкой:    {failed}")
    print(f"Детекций в БД:        {detections}")
    print(
        f"Аудио в памяти:        {stats['chunks_in_memory']} чанков, "
        f"{stats['bytes_in_memory'] / 1e6:.1f} МБ из {stats['max_bytes'] / 1e6:.0f} МБ"
    )
    if stats["chunks_in_memory"] and pending == 0:
        print(
            "Внимание: байты лежат в памяти, но заданий в очереди нет. "
            "Похоже, воркер остановлен — задания протухнут по TTL "
            "и будут помечены как expired."
        )
    return 0


# ---------------------------------------------------------------------------
# Аналитика
# ---------------------------------------------------------------------------


def _cmd_summary(args: argparse.Namespace) -> int:
    from .species import get_species_registry

    registry = get_species_registry()
    since = utcnow() - timedelta(days=args.days)
    with session_scope() as session:
        flt = DetectionFilter(date_from=since, require_location=False, limit=1_000_000)
        summary = species_summary(session, flt)
        hourly = hourly_histogram(session, flt)
        gaps = build_gap_report(session, notify=False)

    print(f"Период: последние {args.days} дн.")
    print(f"Устройств: {gaps.devices_checked}, не на связи: {len(gaps.stale)}")
    print(f"Видов с наблюдениями: {len(summary)}")
    print(f"Всего детекций: {sum(item['detections'] for item in summary)}\n")

    if not summary:
        print("Детекций нет — либо тишина, либо поток ещё не идёт (см. `status`).")
        return 0

    print(f"{'вид':<30}{'слаг':<26}{'детекций':>9}{'ср. уверенность':>16}  пик (UTC)")
    print("-" * 92)
    for item in summary[: args.top]:
        counts = hourly.get(item["species_slug"], [0] * 24)
        peak = max(range(24), key=lambda hour: counts[hour])
        print(
            f"{registry.name_ru(item['species_slug']):<30}"
            f"{item['species_slug']:<26}"
            f"{item['detections']:>9}"
            f"{item['mean_confidence']:>16.3f}"
            f"  {peak:02d}:00 ({counts[peak]})"
        )
    return 0


def _cmd_species(args: argparse.Namespace) -> int:
    from .species import get_species_registry

    registry = get_species_registry()
    if args.json:
        _print_json(registry.catalog())
        return 0

    print(f"В whitelist репозитория модели: {len(registry.slugs)} видов")
    print(f"Распознаёт модель:              {len(registry.model_classes)} классов\n")
    for habitat, items in registry.group_by_habitat(registry.slugs).items():
        print(f"{habitat} ({len(items)}):")
        for item in items:
            marker = "*" if item["slug"] in registry.model_classes else " "
            print(f"  {marker} {item['ru']:<32}{item['latin']}")
        print()
    print("  * — вид распознаётся моделью; остальные в whitelist, но в 16 классах модели не входят")
    return 0


# ---------------------------------------------------------------------------
# Миграции
# ---------------------------------------------------------------------------


def _alembic_config():
    from alembic.config import Config

    from .config import SERVER_DIR

    # `stdout` передаётся явно: в Alembic это значение аргумента функции со значением
    # по умолчанию, то есть `sys.stdout` подставляется один раз при импорте модуля.
    # Из-за этого вывод миграций уходил бы в тот поток, который был актуален на старте
    # процесса, и не попадал ни в перенаправление, ни в буфер тестов.
    return Config(str(SERVER_DIR / "alembic.ini"), stdout=sys.stdout)


def _cmd_db_upgrade(args: argparse.Namespace) -> int:
    from alembic import command

    command.upgrade(_alembic_config(), args.revision or "head")
    print(f"Миграции применены ({args.revision or 'head'}).")
    return 0


def _cmd_db_downgrade(args: argparse.Namespace) -> int:
    from alembic import command

    command.downgrade(_alembic_config(), args.revision)
    print(f"Откатили до {args.revision}.")
    return 0


def _cmd_db_current(args: argparse.Namespace) -> int:
    from alembic import command

    command.current(_alembic_config(), verbose=True)
    return 0


def _cmd_db_check(args: argparse.Namespace) -> int:
    """
    Проверить, что миграции не разошлись с моделями.

    Полезно в CI: расхождение означает, что на следующем `alembic upgrade head`
    в проде что-то не совпадёт с кодом.
    """
    from alembic import command

    try:
        command.check(_alembic_config())
    except Exception as exc:  # noqa: BLE001 - alembic бросает разные исключения
        # Отдельно разбираем самый частый случай в разработке: база создана через
        # `create_all` (тесты, sqlite-файл в репозитории) и поэтому не проstampлена.
        # Формально это тоже «не up to date», но лечится не кодом, а `db upgrade`.
        if "not up to date" in str(exc):
            print(
                "База не отмечена как мигрированная: нет таблицы alembic_version.\n"
                "Вероятно, она создана через create_all (тесты или sqlite dev-файл).\n"
                "Наложите миграции: python -m server.cli db upgrade",
                file=sys.stderr,
            )
            return 1
        print(f"Миграции разошлись с моделями:\n{exc}", file=sys.stderr)
        return 1
    print("Миграции соответствуют моделям.")
    return 0


# ---------------------------------------------------------------------------
# Разбор аргументов
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m server.cli",
        description="Администрирование серверного распознавателя Phoenix_Bolotni",
    )
    parser.add_argument(
        "--log-level", default="INFO", help="Уровень логирования (DEBUG/INFO/WARNING)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- устройства ---
    device = sub.add_parser("device", help="Реестр микрофонов/устройств")
    device_sub = device.add_subparsers(dest="device_command", required=True)

    add = device_sub.add_parser("add", help="Зарегистрировать устройство и получить токен")
    add.add_argument("--name", required=True, help="Название микрофона, например «Пойма у моста»")
    add.add_argument("--owner", default="", help="Ответственный (опционально)")
    add.add_argument("--contact", default=None, help="Телефон или почта (опционально)")
    add.add_argument(
        "--type",
        default="stationary",
        choices=("stationary", "mobile"),
        help="stationary — фиксированная точка, mobile — координаты приходят с чанком",
    )
    add.add_argument("--lat", type=float, default=None, help="Широта (обязательна для stationary)")
    add.add_argument("--lon", type=float, default=None, help="Долгота (обязательна для stationary)")
    add.add_argument("--altitude-m", type=float, default=None, help="Высота над уровнем моря")
    add.add_argument("--place-note", default=None, help="Примечание к точке («у дороги, шум трафика»)")
    add.add_argument(
        "--expected-chunk-sec", type=float, default=None, help="Ожидаемая пауза между чанками"
    )
    add.add_argument("--note", default=None, help="Произвольная заметка")
    add.set_defaults(func=_cmd_device_add)

    listing = device_sub.add_parser("list", help="Список устройств и их состояние")
    listing.add_argument("--active-only", action="store_true", help="Только активные")
    listing.add_argument("--json", action="store_true", help="Вывод в JSON")
    listing.set_defaults(func=_cmd_device_list)

    show = device_sub.add_parser("show", help="Карточка одного устройства (JSON)")
    show.add_argument("device_id")
    show.set_defaults(func=_cmd_device_show)

    update = device_sub.add_parser("update", help="Изменить параметры устройства")
    update.add_argument("device_id")
    update.add_argument("--name", default=None)
    update.add_argument("--owner", default=None)
    update.add_argument("--contact", default=None)
    update.add_argument("--lat", type=float, default=None)
    update.add_argument("--lon", type=float, default=None)
    update.add_argument("--altitude-m", type=float, default=None)
    update.add_argument("--place-note", default=None)
    update.add_argument("--expected-chunk-sec", type=float, default=None)
    update.add_argument("--note", default=None)
    update.add_argument(
        "--rotate-token",
        action="store_true",
        help="Выпустить новый токен (старый станет недействителен)",
    )
    update.set_defaults(func=_cmd_device_update)

    state = device_sub.add_parser("state", help="Включить или деактивировать устройство")
    state.add_argument("device_id")
    state.add_argument("--on", dest="state", action="store_true", default=True, help="Включить")
    state.add_argument("--off", dest="state", action="store_false", help="Деактивировать")
    state.add_argument("--reason", default=None, help="Причина (пишется в события устройства)")
    state.set_defaults(func=_cmd_device_state)

    # --- разрывы ---
    gaps = sub.add_parser("gaps", help="Отчёт о разрывах связи")
    gaps.add_argument("--notify", action="store_true", help="Отправить алерт оператору")
    gaps.add_argument("--json", action="store_true", help="Вывод в JSON")
    gaps.set_defaults(func=_cmd_gaps)

    sweep = sub.add_parser("sweep", help="Разово отметить молчащие устройства (для cron)")
    sweep.add_argument(
        "--no-notify", action="store_true", help="Не слать алерты, только записать события"
    )
    sweep.set_defaults(func=_cmd_sweep)

    watch = sub.add_parser("watch", help="Постоянный мониторинг разрывов")
    watch.add_argument("--interval", type=float, default=None, help="Интервал проверки, секунд")
    watch.add_argument("--no-notify", action="store_true")
    watch.set_defaults(func=_cmd_watch)

    # --- очередь ---
    worker = sub.add_parser("worker", help="Воркер обработки чанков")
    worker.add_argument("--once", action="store_true", help="Обработать текущую очередь и выйти")
    worker.add_argument(
        "--limit", type=int, default=None, help="Сколько чанков обработать в режиме --once"
    )
    worker.add_argument("--worker-id", default=None, help="Идентификатор воркера")
    worker.add_argument("--log-level", default="INFO")
    worker.set_defaults(func=_cmd_worker)

    status = sub.add_parser("status", help="Состояние очереди, памяти и устройств")
    status.set_defaults(func=_cmd_status)

    # --- аналитика ---
    summary = sub.add_parser("summary", help="Сводка по видам за период")
    summary.add_argument("--days", type=int, default=7, help="За сколько дней (по умолчанию 7)")
    summary.add_argument("--top", type=int, default=20, help="Сколько видов показать")
    summary.set_defaults(func=_cmd_summary)

    species = sub.add_parser("species", help="Справочник видов из репозитория модели")
    species.add_argument("--json", action="store_true", help="Вывод в JSON")
    species.set_defaults(func=_cmd_species)

    # --- БД ---
    db = sub.add_parser("db", help="Миграции Alembic")
    db_sub = db.add_subparsers(dest="db_command", required=True)

    up = db_sub.add_parser("upgrade", help="Применить миграции")
    up.add_argument("--revision", default=None, help="До какой ревизии (по умолчанию head)")
    up.set_defaults(func=_cmd_db_upgrade)

    down = db_sub.add_parser("downgrade", help="Откатить миграции")
    down.add_argument("revision", nargs="?", default="-1", help="Целевая ревизия (по умолчанию -1)")
    down.set_defaults(func=_cmd_db_downgrade)

    current = db_sub.add_parser("current", help="Текущая ревизия схемы")
    current.set_defaults(func=_cmd_db_current)

    check = db_sub.add_parser("check", help="Совпадают ли миграции с моделями")
    check.set_defaults(func=_cmd_db_check)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    # Для `db ...` схему создавать не нужно: `alembic` и сам решает, что делать, а
    # `init_db()` на проде создал бы таблицы из моделей в обход миграций.
    if getattr(args, "command", None) != "db":
        from .db import init_db

        init_db()

    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())