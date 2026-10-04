#!/usr/bin/env python3
"""
Скрипт подготовки метаданных датасета для обучения модели Phoenix_Bolotni.
Выполняет валидацию классов против whitelist РФ, групповое разбиение по recording_id
без утечки данных между сплитами, и сохраняет train.jsonl, val.jsonl, test.jsonl,
classes.json и label2idx.json.
"""

import argparse
import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from scripts.validate_dataset import validate_dataset


def extract_recording_id(file_path: Path, regex_pattern: Optional[str] = None) -> str:
    """
    Извлечение recording_id из имени файла.
    По умолчанию отсекает суффиксы нарезки вида _partNN, _clipNN или числовые индексы.
    Если совпадений нет, используется stem имени файла.
    """
    stem = file_path.stem
    if regex_pattern:
        match = re.search(regex_pattern, stem)
        if match:
            return match.group(1)
    
    # Стандартный шаблон: отсекаем _part\d+, -clip\d+, _chunk\d+
    match = re.match(r"^(.*?)(?:[_\-]part\d+|[_\-]clip\d+|[_\-]chunk\d+.*)$", stem, re.IGNORECASE)
    if match:
        return match.group(1)
    return stem


def group_split_records(
    class_records: List[Dict[str, Any]],
    val_ratio: float = 0.1,
    test_ratio: float = 0.1,
    seed: int = 42,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """
    Разбиение записей одного класса по группам recording_id без утечки.
    Возвращает train_items, val_items, test_items, warnings.
    """
    rng = np.random.RandomState(seed)
    
    # Группировка файлов по recording_id
    rec_groups = defaultdict(list)
    for item in class_records:
        rec_groups[item["recording_id"]].append(item)

    unique_recs = sorted(list(rec_groups.keys()))
    rng.shuffle(unique_recs)
    num_recs = len(unique_recs)

    warnings = []
    cls_name = class_records[0]["class_name"]

    if num_recs == 1:
        msg = f"⚠️ Класс '{cls_name}' имеет только 1 запись ({unique_recs[0]}) — направлен только в train"
        warnings.append(msg)
        train_recs = unique_recs
        val_recs = []
        test_recs = []
    elif num_recs == 2:
        msg = f"⚠️ Класс '{cls_name}' имеет только 2 записи ({unique_recs}) — направлены в train и val (test пуст)"
        warnings.append(msg)
        train_recs = [unique_recs[0]]
        val_recs = [unique_recs[1]]
        test_recs = []
    else:
        # При 3 и более записях гарантируем хотя бы по 1 в val и test
        n_test = max(1, int(round(num_recs * test_ratio)))
        n_val = max(1, int(round(num_recs * val_ratio)))

        if n_test + n_val >= num_recs:
            n_test = 1
            n_val = 1

        test_recs = unique_recs[:n_test]
        val_recs = unique_recs[n_test : n_test + n_val]
        train_recs = unique_recs[n_test + n_val :]

    train_items = [it for r in train_recs for it in rec_groups[r]]
    val_items = [it for r in val_recs for it in rec_groups[r]]
    test_items = [it for r in test_recs for it in rec_groups[r]]

    return train_items, val_items, test_items, warnings


def prepare_dataset(
    data_dir: str = "./dataset",
    artifacts_dir: str = "./artifacts",
    skip_validation: bool = False,
    recording_regex: Optional[str] = None,
    seed: int = 42,
    val_ratio: float = 0.1,
    test_ratio: float = 0.1,
) -> Dict[str, Any]:
    data_path = Path(data_dir)
    art_path = Path(artifacts_dir)
    art_path.mkdir(parents=True, exist_ok=True)

    validation_report_file = art_path / "validation_report.json"
    if not skip_validation:
        print("🔍 Валидация датасета против whitelist РФ...")
        validate_dataset(data_dir=data_dir, report_path=str(validation_report_file))

    print(f"🔍 Сканирование датасета в: {data_path.resolve()}")
    if not data_path.exists():
        print(f"❌ Папка датасета {data_path} не существует!")
        return {"status": "error", "message": "data_dir not found"}

    # Поиск всех папок с классами (включая _noise)
    classes = sorted([p.name for p in data_path.iterdir() if p.is_dir()])
    if not classes:
        print("❌ Классы не найдены! Убедитесь, что в папке dataset есть подпапки с названиями птиц.")
        return {"status": "error", "message": "no classes found"}

    label2idx = {c: i for i, c in enumerate(classes)}

    train_all: List[Dict[str, Any]] = []
    val_all: List[Dict[str, Any]] = []
    test_all: List[Dict[str, Any]] = []
    all_warnings: List[str] = []

    print("\nСтатистика классов и файлов:")
    for cls in classes:
        cls_dir = data_path / cls
        wavs = sorted(list(cls_dir.rglob("*.wav")))
        print(f"  • {cls:25s} : {len(wavs)} файлов")
        if not wavs:
            continue

        class_items = []
        for p in wavs:
            rec_id = extract_recording_id(p, regex_pattern=recording_regex)
            class_items.append({
                "path": str(p),
                "label": label2idx[cls],
                "recording_id": rec_id,
                "class_name": cls,
            })

        tr, vl, ts, warns = group_split_records(
            class_items, val_ratio=val_ratio, test_ratio=test_ratio, seed=seed
        )
        train_all.extend(tr)
        val_all.extend(vl)
        test_all.extend(ts)
        all_warnings.extend(warns)

    for w in all_warnings:
        print(w)

    # Обновление отчета валидации информацией о редких классах и сплитах
    if validation_report_file.exists():
        try:
            with open(validation_report_file, "r", encoding="utf-8") as f:
                rep_data = json.load(f)
        except Exception:
            rep_data = {}
    else:
        rep_data = {}

    rep_data["split_warnings"] = all_warnings
    rep_data["split_stats"] = {
        "train_samples": len(train_all),
        "val_samples": len(val_all),
        "test_samples": len(test_all),
    }
    with open(validation_report_file, "w", encoding="utf-8") as f:
        json.dump(rep_data, f, ensure_ascii=False, indent=2)

    # Сохранение .jsonl файлов
    def save_jsonl(filename: str, items: List[Dict[str, Any]]):
        with open(art_path / filename, "w", encoding="utf-8") as f:
            for item in items:
                record = {
                    "path": item["path"],
                    "label": int(item["label"]),
                    "recording_id": item["recording_id"],
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    save_jsonl("train.jsonl", train_all)
    save_jsonl("val.jsonl", val_all)
    save_jsonl("test.jsonl", test_all)

    with open(art_path / "classes.json", "w", encoding="utf-8") as f:
        json.dump(classes, f, ensure_ascii=False, indent=2)

    with open(art_path / "label2idx.json", "w", encoding="utf-8") as f:
        json.dump(label2idx, f, ensure_ascii=False, indent=2)

    print(f"\n📂 Метаданные сохранены в {art_path.resolve()}")
    print(f"Train: {len(train_all)}, Val: {len(val_all)}, Test: {len(test_all)}")
    return {
        "classes": classes,
        "train_count": len(train_all),
        "val_count": len(val_all),
        "test_count": len(test_all),
        "warnings": all_warnings,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Подготовка метаданных датасета для обучения модели Phoenix_Bolotni"
    )
    parser.add_argument(
        "--data-dir",
        default="./dataset",
        help="Путь к папке датасета (по умолчанию: ./dataset)",
    )
    parser.add_argument(
        "--artifacts-dir",
        default="./artifacts",
        help="Путь к папке сохранения метаданных (по умолчанию: ./artifacts)",
    )
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Пропустить валидацию классов против whitelist РФ",
    )
    parser.add_argument(
        "--recording-regex",
        default=None,
        help="Регулярное выражение для извлечения recording_id из имени файла",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Случайное зерно генератора (по умолчанию: 42)",
    )
    parser.add_argument(
        "--val-ratio",
        type=float,
        default=0.1,
        help="Доля валидационной выборки (по умолчанию: 0.1)",
    )
    parser.add_argument(
        "--test-ratio",
        type=float,
        default=0.1,
        help="Доля тестовой выборки (по умолчанию: 0.1)",
    )
    args = parser.parse_args()

    prepare_dataset(
        data_dir=args.data_dir,
        artifacts_dir=args.artifacts_dir,
        skip_validation=args.skip_validation,
        recording_regex=args.recording_regex,
        seed=args.seed,
        val_ratio=args.val_ratio,
        test_ratio=args.test_ratio,
    )


if __name__ == "__main__":
    main()
