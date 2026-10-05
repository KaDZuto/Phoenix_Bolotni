"""
Справочник видов: загрузка `species/ru_birds_whitelist.json` и `models/classes.json`.

Whitelist репозитория модели — единый источник правды о допустимых видах фауны РФ,
русских названиях и биотопах. Сервер не дублирует этот список, а читает его напрямую
(см. td3.md, раздел 0 «Зависимости от основного репо (модели)»).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .config import get_settings

#: Человекочитаемые подписи биотопов из whitelist (для UI карты).
HABITAT_LABELS_RU: Dict[str, str] = {
    "wetland": "Околоводные и болотные",
    "forest": "Лесные",
    "field_steppe": "Полевые и лесостепные",
    "urban": "Городские и антропогенные",
    "other": "Прочие",
}


class SpeciesRegistry:
    """Whitelist видов РФ + классы модели, с русскими названиями и группировкой по биотопу."""

    def __init__(self, whitelist: Dict[str, Dict[str, str]], model_classes: Optional[List[str]] = None):
        self._whitelist = whitelist
        self._model_classes = list(model_classes or [])
        self._model_class_set = set(self._model_classes)

    # --- доступ по слагу -------------------------------------------------
    def __contains__(self, slug: str) -> bool:
        return slug in self._whitelist

    def name_ru(self, slug: str) -> str:
        entry = self._whitelist.get(slug)
        return entry["ru"] if entry else slug

    def habitat(self, slug: str) -> str:
        entry = self._whitelist.get(slug)
        return entry["habitat"] if entry else "other"

    def label(self, slug: str) -> str:
        """`Грач (corvus_frugilegus)` — подпись для дашборда и легенды."""
        if slug == "_noise":
            return "Фоновый шум (_noise)"
        return f"{self.name_ru(slug)} ({slug})"

    # --- списки ----------------------------------------------------------
    @property
    def slugs(self) -> List[str]:
        return list(self._whitelist.keys())

    @property
    def model_classes(self) -> List[str]:
        return list(self._model_classes)

    def supported_slugs(self) -> List[str]:
        """Виды из whitelist, которые реально умеет распознавать текущая модель."""
        if not self._model_class_set:
            return self.slugs
        return [s for s in self._whitelist if s in self._model_class_set]

    def unknown_slugs(self, slugs: Iterable[str]) -> List[str]:
        return [s for s in slugs if s not in self._whitelist]

    def group_by_habitat(self, slugs: Optional[Iterable[str]] = None) -> Dict[str, List[dict]]:
        """Виды, сгруппированные по биотопу — структура для `<optgroup>` в UI."""
        wanted = list(slugs) if slugs is not None else self.supported_slugs()
        groups: Dict[str, List[dict]] = {}
        for slug in wanted:
            groups.setdefault(self.habitat(slug), []).append(
                {"slug": slug, "ru": self.name_ru(slug), "latin": slug}
            )
        for items in groups.values():
            items.sort(key=lambda i: i["ru"])
        return {k: groups[k] for k in sorted(groups)}

    def catalog(self) -> List[dict]:
        """Плоский список видов с русскими названиями (для легенды карты и экспорта)."""
        return [
            {
                "slug": slug,
                "ru": self.name_ru(slug),
                "habitat": self.habitat(slug),
                "habitat_ru": HABITAT_LABELS_RU.get(self.habitat(slug), self.habitat(slug)),
                "in_model": slug in self._model_class_set,
            }
            for slug in self.supported_slugs()
        ]


def _load_json(path: Path) -> object:
    if not path.exists():
        raise FileNotFoundError(
            f"Не найден файл репозитория модели: {path}. "
            "Проверьте переменные PHOENIX_WHITELIST / PHOENIX_CLASSES (см. server/.env.example)."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def get_species_registry() -> SpeciesRegistry:
    settings = get_settings()
    whitelist = _load_json(settings.whitelist_path)
    classes: Optional[List[str]] = None
    try:
        classes = _load_json(settings.classes_path)
    except FileNotFoundError:
        # classes.json нужен только для пересечения с моделью; без него отдаём весь whitelist.
        classes = None
    return SpeciesRegistry(whitelist, classes)  # type: ignore[arg-type]