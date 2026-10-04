"""
Модуль расчета индекса биоиндикации заболоченности на основе состава авифауны.

ВАЖНОЕ ПРИМЕЧАНИЕ ДЛЯ РАЗРАБОТЧИКОВ И ПОЛЬЗОВАТЕЛЕЙ:
Нейросетевая аудио-модель Phoenix_Bolotni НЕ прогнозирует погоду и НЕ определяет
степень заболоченности водоема напрямую. Аудио-классификатор решает исключительно
задачу детекции видов птиц по звуку.

Данный модуль представляет собой вспомогательный каркас экологической биоиндикации:
веса видовой приуроченности (wetland_affinity) и методика расчета индекса ДОЛЖНЫ
быть согласованы и откалиброваны профессиональным орнитологом/экологом перед
использованием в природоохранных отчетах.
"""

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


def load_species_weights(weights_path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    p = Path(weights_path) if weights_path else Path(__file__).parent / "species_weights.json"
    if not p.exists():
        raise FileNotFoundError(f"Файл весов не найден: {p}")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


class WetlandBioindicatorScorer:
    """
    Калькулятор вспомогательного индекса заболоченности по составу обнаруженных видов птиц.
    """

    def __init__(self, weights_path: Optional[Union[str, Path]] = None, custom_weights: Optional[Dict[str, Any]] = None):
        if custom_weights is not None:
            self.weights = custom_weights
        else:
            self.weights = load_species_weights(weights_path)

    def calculate_score(self, detections: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Расчет индекса по списку обнаружений.
        Каждое обнаружение должно содержать поле 'species'. Опционально: 'timestamp', 'lat', 'lon'.
        """
        if not detections:
            return {
                "status": "empty",
                "message": "Нет данных обнаружений для анализа.",
                "total_detections": 0,
                "unique_species": 0,
                "wetland_index": None,
                "calibrated": False,
            }

        # Проверка, заполнены ли веса экспертом
        species_in_detections = [d["species"] for d in detections if "species" in d]
        counts = Counter(species_in_detections)

        weighted_sum = 0.0
        total_calibrated_weight = 0.0
        uncalibrated_species = []

        species_breakdown = {}

        for sp, count in counts.items():
            sp_info = self.weights.get(sp, {})
            affinity = sp_info.get("wetland_affinity")

            if affinity is None:
                uncalibrated_species.append(sp)
                val = 0.0
            else:
                val = float(affinity)
                weighted_sum += val * count
                total_calibrated_weight += count

            species_breakdown[sp] = {
                "count": count,
                "wetland_affinity": affinity,
                "habitat_tag": sp_info.get("habitat", "unknown"),
                "ru_name": sp_info.get("ru", ""),
            }

        is_fully_calibrated = (len(uncalibrated_species) == 0 and total_calibrated_weight > 0)

        if total_calibrated_weight > 0:
            wetland_index = round(weighted_sum / total_calibrated_weight, 4)
        else:
            wetland_index = None

        warning_note = None
        if uncalibrated_species:
            warning_note = (
                f"⚠️ Для {len(uncalibrated_species)} видов веса 'wetland_affinity' не заполнены (null). "
                "Требуется заполнение bioindicator/species_weights.json профильным биологом."
            )

        return {
            "status": "ok",
            "total_detections": len(detections),
            "unique_species": len(counts),
            "wetland_index": wetland_index,
            "calibrated": is_fully_calibrated,
            "warning": warning_note,
            "uncalibrated_species": uncalibrated_species,
            "species_summary": species_breakdown,
        }
