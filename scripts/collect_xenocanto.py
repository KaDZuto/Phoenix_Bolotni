#!/usr/bin/env python3
"""
Скрипт для поиска и загрузки аудиозаписей птиц из базы Xeno-canto API.
Фильтрует виды строго по whitelist фауны РФ (species/ru_birds_whitelist.json).
Поддерживает фильтрацию по стране (cnt:russia), минимальному качеству (q:>=B),
режим --dry-run, rate-limit и обязательное сохранение лицензий и атрибуции (ATTRIBUTION.md).
"""

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests
import soundfile as sf

# Добавляем корень проекта в sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.validate_dataset import load_whitelist

XENO_CANTO_API_URL = "https://xeno-canto.org/api/3/recordings"


def search_recordings(
    query_terms: List[str],
    api_key: Optional[str] = None,
    retries: int = 3,
    backoff_sec: float = 2.0,
) -> Dict[str, Any]:
    """Запрос к API v3 Xeno-canto с обработкой ошибок и ретраями."""
    full_query = " ".join(query_terms)
    params = {"query": full_query}
    if api_key:
        params["key"] = api_key

    headers = {"User-Agent": "PhoenixBolotniCollector/1.0 (biodiversity-research)"}

    for attempt in range(retries):
        try:
            response = requests.get(XENO_CANTO_API_URL, params=params, headers=headers, timeout=15)
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 401:
                print("⚠️ Xeno-canto API v3 требует API ключ. Укажите --api-key или переменную окружения XENO_CANTO_API_KEY.")
                return {"numRecordings": 0, "recordings": []}
            elif response.status_code == 429:
                wait_time = backoff_sec * (attempt + 1) * 2
                print(f"⚠️ Превышен rate-limit API (429). Ожидание {wait_time:.1f}с...")
                time.sleep(wait_time)
            else:
                print(f"⚠️ HTTP {response.status_code} при запросе к Xeno-canto API")
        except Exception as e:
            print(f"⚠️ Ошибка сети при попытке {attempt + 1}/{retries}: {e}")
            time.sleep(backoff_sec * (attempt + 1))

    return {"numRecordings": 0, "recordings": []}


def download_and_save_audio(
    file_url: str,
    output_wav_path: Path,
    retries: int = 3,
) -> bool:
    """Скачивание аудиопотока и сохранение в .wav формате."""
    headers = {"User-Agent": "PhoenixBolotniCollector/1.0 (biodiversity-research)"}
    output_wav_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(retries):
        try:
            res = requests.get(file_url, headers=headers, timeout=25)
            if res.status_code == 200:
                # Читаем байты аудио через soundfile
                data_io = io.BytesIO(res.content)
                wav_data, sample_rate = sf.read(data_io)
                sf.write(str(output_wav_path), wav_data, sample_rate)
                return True
        except Exception as e:
            time.sleep(1.0 * (attempt + 1))
    return False


def collect_xenocanto(
    whitelist_path: str = "./species/ru_birds_whitelist.json",
    data_dir: str = "./dataset",
    species_filter: Optional[str] = None,
    filter_russia: bool = True,
    min_quality: str = "B",
    max_per_species: int = 5,
    dry_run: bool = False,
    sleep_sec: float = 1.0,
    api_key: Optional[str] = None,
):
    wl_path = Path(whitelist_path)
    whitelist = load_whitelist(wl_path)
    base_dir = Path(data_dir)

    api_key = api_key or os.environ.get("XENO_CANTO_API_KEY")

    target_species = []
    if species_filter and species_filter.lower() != "all":
        requested = [s.strip() for s in species_filter.split(",")]
        for s in requested:
            if s in whitelist:
                target_species.append(s)
            else:
                print(f"⛔️ Вид '{s}' отсутствует в whitelist фауны РФ! Пропущен.")
    else:
        target_species = sorted(list(whitelist.keys()))

    print("=" * 70)
    print("🐦 XENO-CANTO DATA COLLECTOR — ФАУНА РОССИИ")
    print(f"Целевых видов из whitelist: {len(target_species)}")
    print(f"Фильтр по стране РФ (cnt:russia): {'Да' if filter_russia else 'Нет'}")
    print(f"Мин. качество: {min_quality} | Лимит на вид: {max_per_species} | Режим dry-run: {dry_run}")
    print("=" * 70)

    total_found = 0
    total_downloaded = 0

    for idx, sp_slug in enumerate(target_species, start=1):
        sp_info = whitelist[sp_slug]
        # Превращаем slug вида 'anas_platyrhynchos' в латинское название 'Anas platyrhynchos'
        sci_name = sp_slug.replace("_", " ").capitalize()
        ru_name = sp_info.get("ru", "")

        query_terms = [f'"{sci_name}"']
        if filter_russia:
            query_terms.append("cnt:russia")
        if min_quality:
            query_terms.append(f"q:>={min_quality}")

        print(f"\n[{idx}/{len(target_species)}] 🔎 Поиск: {sci_name} ({ru_name}) [запрос: {' '.join(query_terms)}]")
        data = search_recordings(query_terms, api_key=api_key)
        recordings = data.get("recordings", [])
        num_found = len(recordings)
        total_found += num_found
        print(f"    Найдено доступных записей: {num_found}")

        if dry_run or num_found == 0:
            time.sleep(sleep_sec)
            continue

        sp_dir = base_dir / sp_slug
        sp_dir.mkdir(parents=True, exist_ok=True)
        attribution_records = []

        to_download = recordings[:max_per_species]
        for rec in to_download:
            rec_id = rec.get("id")
            rec_file_url = rec.get("file")
            rec_author = rec.get("rec", "Unknown")
            rec_lic = rec.get("lic", "Unknown")
            rec_url = rec.get("url", f"https://xeno-canto.org/{rec_id}")
            rec_country = rec.get("cnt", "")
            rec_loc = rec.get("loc", "")

            out_fn = sp_dir / f"XC{rec_id}.wav"
            print(f"    ⬇️ Скачивание XC{rec_id} (автор: {rec_author}, лицензия: {rec_lic})...")

            success = download_and_save_audio(rec_file_url, out_fn)
            if success:
                total_downloaded += 1
                attribution_records.append({
                    "id": rec_id,
                    "filename": out_fn.name,
                    "species": sci_name,
                    "species_slug": sp_slug,
                    "author": rec_author,
                    "license": rec_lic,
                    "url": rec_url,
                    "country": rec_country,
                    "location": rec_loc,
                })
            else:
                print(f"    ❌ Ошибка скачивания XC{rec_id}")

            time.sleep(sleep_sec)

        # Сохранение манифеста атрибуции
        if attribution_records:
            attr_json_path = sp_dir / "attribution.json"
            attr_md_path = sp_dir / "ATTRIBUTION.md"

            with open(attr_json_path, "w", encoding="utf-8") as f:
                json.dump(attribution_records, f, ensure_ascii=False, indent=2)

            with open(attr_md_path, "w", encoding="utf-8") as f:
                f.write(f"# Атрибуция аудиозаписей для вида {sci_name} ({ru_name})\n\n")
                f.write("Все записи загружены с [Xeno-canto](https://xeno-canto.org) и распространяются под лицензиями авторов:\n\n")
                f.write("| Файл | ID | Автор | Лицензия | Ссылка |\n")
                f.write("|---|---|---|---|---|\n")
                for it in attribution_records:
                    f.write(f"| `{it['filename']}` | [{it['id']}]({it['url']}) | {it['author']} | {it['license']} | [Страница]({it['url']}) |\n")

    print("\n" + "=" * 70)
    print(f"🏁 Завершено. Всего найдено записей: {total_found}, скачано: {total_downloaded}")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        description="Сбор и фильтрация аудиозаписей птиц фауны РФ с Xeno-canto"
    )
    parser.add_argument(
        "--whitelist",
        default="./species/ru_birds_whitelist.json",
        help="Путь к whitelist российских птиц",
    )
    parser.add_argument(
        "--data-dir",
        default="./dataset",
        help="Путь к целевой папке сохранения",
    )
    parser.add_argument(
        "--species",
        default=None,
        help="Конкретные виды через запятую (например 'anas_platyrhynchos,ardea_cinerea') или 'all'",
    )
    parser.add_argument(
        "--no-country-filter",
        action="store_true",
        help="Отключить фильтр записи строго по территории РФ",
    )
    parser.add_argument(
        "--quality",
        default="B",
        help="Минимальный рейтинг качества (по умолчанию: B)",
    )
    parser.add_argument(
        "--max-per-species",
        type=int,
        default=5,
        help="Максимальное количество записей для каждого вида (по умолчанию: 5)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Режим симуляции: показать количество доступных записей без скачивания",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.8,
        help="Задержка между сетевыми запросами в секундах (по умолчанию: 0.8)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API ключ Xeno-canto (также читается из переменной XENO_CANTO_API_KEY)",
    )
    args = parser.parse_args()

    collect_xenocanto(
        whitelist_path=args.whitelist,
        data_dir=args.data_dir,
        species_filter=args.species,
        filter_russia=not args.no_country_filter,
        min_quality=args.quality,
        max_per_species=args.max_per_species,
        dry_run=args.dry_run,
        sleep_sec=args.sleep,
        api_key=args.api_key,
    )


if __name__ == "__main__":
    main()
