#!/usr/bin/env python3
"""
Скрипт валидации классов датасета против Whitelist видов фауны Российской Федерации.
Источник списка: «Список птиц Российской Федерации» (Коблик Е.А., Редькин Я.А., Архипов В.Ю. / Мензбировское орнитологическое общество, eBird/Avibase).
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Any

# Служебные имена папок, не являющиеся видами птиц
SPECIAL_CLASSES = {"_noise"}


def load_whitelist(whitelist_path: Path) -> Dict[str, Any]:
    if not whitelist_path.exists():
        raise FileNotFoundError(f"Whitelist не найден по пути: {whitelist_path}")
    with open(whitelist_path, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_dataset(
    data_dir: str = "./dataset",
    whitelist_path: str = "./species/ru_birds_whitelist.json",
    report_path: str = "./artifacts/validation_report.json",
) -> Dict[str, Any]:
    data_path = Path(data_dir)
    wl_path = Path(whitelist_path)
    rep_path = Path(report_path)

    whitelist = load_whitelist(wl_path)

    if not data_path.exists():
        print(f"⚠️ Папка датасета {data_path} не существует.")
        subdirs = []
    else:
        subdirs = sorted([p.name for p in data_path.iterdir() if p.is_dir()])

    valid_classes: List[str] = []
    invalid_classes: List[str] = []
    special_found: List[str] = []
    warnings: List[str] = []

    for name in subdirs:
        if name in SPECIAL_CLASSES:
            special_found.append(name)
            continue
        if name in whitelist:
            valid_classes.append(name)
        else:
            msg = f'⚠️ вид "{name}" отсутствует в whitelist фауны РФ — проверьте вручную'
            print(msg)
            warnings.append(msg)
            invalid_classes.append(name)

    report = {
        "data_dir": str(data_path),
        "total_classes": len(subdirs),
        "valid_classes": valid_classes,
        "invalid_classes": invalid_classes,
        "special_classes": special_found,
        "warnings": warnings,
        "status": "warning" if invalid_classes else "ok",
    }

    rep_path.parent.mkdir(parents=True, exist_ok=True)
    with open(rep_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(
        f"✅ Валидация завершена: {len(valid_classes)} видов из РФ, "
        f"{len(special_found)} служебных классов, "
        f"{len(invalid_classes)} неизвестных видов. Отчёт: {rep_path}"
    )

    return report


def main():
    parser = argparse.ArgumentParser(
        description="Валидация классов датасета на соответствие whitelist фауны РФ"
    )
    parser.add_argument(
        "--data-dir",
        default="./dataset",
        help="Путь к папке датасета (по умолчанию: ./dataset)",
    )
    parser.add_argument(
        "--whitelist",
        default="./species/ru_birds_whitelist.json",
        help="Путь к whitelist JSON (по умолчанию: ./species/ru_birds_whitelist.json)",
    )
    parser.add_argument(
        "--report-path",
        default="./artifacts/validation_report.json",
        help="Путь к сохраняемому отчету (по умолчанию: ./artifacts/validation_report.json)",
    )
    args = parser.parse_args()

    validate_dataset(
        data_dir=args.data_dir,
        whitelist_path=args.whitelist,
        report_path=args.report_path,
    )


if __name__ == "__main__":
    main()
