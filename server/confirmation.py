"""
Задача 1 [P0] — спецификация «многократной проверки» (window voting) для сервера.

Идея: модель не должна быть самоуверенной по одному 5-секундному окну. Поток
нарезается на перекрывающиеся окна, по каждому окну считаются вероятности
(опционально с TTA), и вид попадает в БД/карту **только** если он выигрывает
голосование в `min_agree_windows` (K) из последних `window_count_for_vote` (M) окон.

Как подключено к репозиторию модели (td3.md, раздел 0)
-------------------------------------------------------
Логика окна/голосования **не дублируется**: основной путь — импорт готового
`AudioPredictor.predict_sliding_window()` из `inference.py` репозитория модели
(подключение как пакет/подмодуль — см. `server/README.md`, раздел «Способы
подключения к репо модели»). Здесь только:

* нормализация вывода модели в стабильные dataclass-ы (`ConfirmationResult`),
  которые знает потребитель (воркер, БД, экспорт);
* вычисление временного интервала подтверждённой детекции (в `inference.py`
  отдаётся только момент времени старта окна);
* чистая функция `vote_windows()` — пересчёт голосования по уже посчитанным
  per-window вероятностям. Она нужна для (а) тестов на синтетических
  последовательностях, (б) QA-пересчёта сохранённых `raw_windows`, (в) подстраховки,
  если импортируемая версия `inference.py` ещё не содержит голосования.

Все числовые параметры читаются из единого `model_config.json` репозитория модели
(`window_stride_sec`, `window_count_for_vote`, `min_agree_windows`,
`confidence_threshold`, `tta_time_shift_ms`), поэтому сервер, обучение и
Android-клиент (Kotlin) используют концептуально одни и те же значения.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .config import REPO_ROOT, get_settings

# ---------------------------------------------------------------------------
# Интеграция с репозиторием модели
# ---------------------------------------------------------------------------

#: Публичная точка входа репозитория модели (td3.md, раздел 0).
MODEL_REPO_MODULE = "inference"


def ensure_model_repo_on_path() -> Path:
    """Добавить корень репозитория модели в `sys.path` (вариант «синхронизируемая копия»)."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    return REPO_ROOT


def import_model_module() -> Optional[Any]:
    """
    Импортировать модуль предсказания репозитория модели.

    Порядок попыток: (1) уже установленный/подключённый пакет `inference`,
    (2) корень текущего git-репозитория (совместная разработка / подмодуль).
    Возвращает `None`, если модуль недоступен, — тогда используется локальная
    реализация голосования (см. `vote_windows`).
    """
    try:
        return importlib.import_module(MODEL_REPO_MODULE)
    except ImportError:
        pass
    ensure_model_repo_on_path()
    try:
        return importlib.import_module(MODEL_REPO_MODULE)
    except ImportError:
        return None


# TODO: заменить на импорт из репо модели, когда там появится inference.confirm_detection()
#       (единая спецификация многократной проверки). До этого используется локальная
#       копия той же спецификации — см. `vote_windows` и критерии приёмки в td3.md.
DEFAULT_MODEL_CONFIG: Dict[str, Any] = {
    "clip_seconds": 5.0,
    "confidence_threshold": 0.6,
    "window_stride_sec": 2.5,
    "window_count_for_vote": 3,
    "min_agree_windows": 2,
    "tta_time_shift_ms": [-200, 0, 200],
    "noise_class_slug": "_noise",
    "target_sr": 32000,
}


@dataclass(frozen=True)
class VotingParams:
    """Параметры многократной проверки, прочитанные из `model_config.json`."""

    clip_seconds: float = 5.0
    stride_seconds: float = 2.5
    votes_required: int = 3
    min_agree: int = 2
    confidence_threshold: float = 0.6
    noise_class: str = "_noise"
    tta_time_shift_ms: Sequence[int] = field(default_factory=lambda: [-200, 0, 200])
    target_sr: int = 32000

    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]] = None) -> "VotingParams":
        cfg = {**DEFAULT_MODEL_CONFIG, **(config or {})}
        return cls(
            clip_seconds=float(cfg.get("clip_seconds", 5.0)),
            stride_seconds=float(cfg.get("window_stride_sec", 2.5)),
            votes_required=int(cfg.get("window_count_for_vote", 3)),
            min_agree=int(cfg.get("min_agree_windows", 2)),
            confidence_threshold=float(cfg.get("confidence_threshold", 0.6)),
            noise_class=str(cfg.get("noise_class_slug", "_noise")),
            tta_time_shift_ms=tuple(cfg.get("tta_time_shift_ms", [-200, 0, 200])),
            target_sr=int(cfg.get("target_sr", 32000)),
        )

    @property
    def window_seconds(self) -> float:
        """Длина окна анализа (`clip_seconds`, в `inference.py` — `clip_seconds`)."""
        return self.clip_seconds


def load_voting_params(config_path: Optional[Path] = None) -> VotingParams:
    """Прочитать параметры из `model_config.json` репозитория модели."""
    model_module = import_model_module()
    config: Optional[Dict[str, Any]] = None
    if model_module is not None and hasattr(model_module, "load_config"):
        try:  # audio_utils.load_config умеет искать файл рядом с репозиторием модели
            config = model_module.load_config(str(config_path) if config_path else None)
        except Exception:  # pragma: no cover - подстраховка, не должно падать
            config = None
    if config is None:
        path = config_path or get_settings().model_config_path
        path = Path(path)
        if path.exists():
            import json

            with open(path, "r", encoding="utf-8") as f:
                config = json.load(f)
    return VotingParams.from_config(config)


# ---------------------------------------------------------------------------
# Структуры результата
# ---------------------------------------------------------------------------


@dataclass
class WindowPrediction:
    """Сырое предсказание по одному окну (для отладки/QA — в БД не пишется аудио, только числа)."""

    index: int
    start_sec: float
    end_sec: float
    top_species: Optional[str]
    top_probability: float
    status: str
    reason: str = ""
    probabilities: Dict[str, float] = field(default_factory=dict)

    @property
    def vote_class(self) -> Optional[str]:
        """Класс, отдающий голос за вид (None, если окно не даёт голоса)."""
        return self.top_species if self.status == "detected" else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "start_sec": round(self.start_sec, 3),
            "end_sec": round(self.end_sec, 3),
            "top_species": self.top_species,
            "top_probability": round(self.top_probability, 6),
            "status": self.status,
            "reason": self.reason,
            "probabilities": {k: round(v, 6) for k, v in self.probabilities.items()},
        }


@dataclass
class ConfirmedDetection:
    """Подтверждённая детекция: то, что реально попадает в БД и на карту."""

    species: str
    start_sec: float
    end_sec: float
    confidence: float
    mean_confidence: float
    votes: int
    windows_evaluated: int
    window_indices: List[int] = field(default_factory=list)

    @property
    def support_ratio(self) -> float:
        return self.votes / self.windows_evaluated if self.windows_evaluated else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "species": self.species,
            "start_sec": round(self.start_sec, 3),
            "end_sec": round(self.end_sec, 3),
            "confidence": round(self.confidence, 6),
            "mean_confidence": round(self.mean_confidence, 6),
            "votes": self.votes,
            "windows_evaluated": self.windows_evaluated,
            "support_ratio": round(self.support_ratio, 4),
            "window_indices": list(self.window_indices),
        }


@dataclass
class ConfirmationResult:
    """Результат многократной проверки чанка: сырые окна + подтверждённые детекции."""

    clip_duration_sec: float
    target_sr: int
    raw_windows: List[WindowPrediction] = field(default_factory=list)
    confirmed_detections: List[ConfirmedDetection] = field(default_factory=list)
    source: str = "local_voting"
    tta_used: bool = False

    @property
    def total_windows(self) -> int:
        return len(self.raw_windows)

    @property
    def has_detection(self) -> bool:
        return bool(self.confirmed_detections)

    @property
    def top_species(self) -> Optional[str]:
        return self.confirmed_detections[0].species if self.confirmed_detections else None

    def with_time_offset(self, offset_sec: float) -> "ConfirmationResult":
        """Копия результата со сдвигом временных меток (сек → абсолютное время чанка)."""
        return ConfirmationResult(
            clip_duration_sec=self.clip_duration_sec,
            target_sr=self.target_sr,
            raw_windows=[
                WindowPrediction(
                    index=w.index,
                    start_sec=w.start_sec + offset_sec,
                    end_sec=w.end_sec + offset_sec,
                    top_species=w.top_species,
                    top_probability=w.top_probability,
                    status=w.status,
                    reason=w.reason,
                    probabilities=dict(w.probabilities),
                )
                for w in self.raw_windows
            ],
            confirmed_detections=[
                ConfirmedDetection(
                    species=d.species,
                    start_sec=d.start_sec + offset_sec,
                    end_sec=d.end_sec + offset_sec,
                    confidence=d.confidence,
                    mean_confidence=d.mean_confidence,
                    votes=d.votes,
                    windows_evaluated=d.windows_evaluated,
                    window_indices=list(d.window_indices),
                )
                for d in self.confirmed_detections
            ],
            source=self.source,
            tta_used=self.tta_used,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "clip_duration_sec": round(self.clip_duration_sec, 3),
            "target_sr": self.target_sr,
            "total_windows": self.total_windows,
            "source": self.source,
            "tta_used": self.tta_used,
            "overall_status": "detected" if self.has_detection else "unknown/uncertain",
            "confirmed_species": self.top_species,
            "confirmed_detections": [d.to_dict() for d in self.confirmed_detections],
            "raw_windows": [w.to_dict() for w in self.raw_windows],
        }


# ---------------------------------------------------------------------------
# Чистая логика голосования (тестируется на синтетических вероятностях)
# ---------------------------------------------------------------------------


def classify_probabilities(
    probabilities: Dict[str, float],
    params: VotingParams,
) -> Dict[str, Any]:
    """Повторить решение `inference.AudioPredictor.predict_window` по вектору вероятностей."""
    if not probabilities:
        return {
            "status": "unknown/uncertain",
            "confirmed_species": None,
            "reason": "пустой вектор вероятностей",
            "top_1_species": None,
            "top_1_probability": 0.0,
        }
    top_species, top_probability = max(probabilities.items(), key=lambda kv: kv[1])
    if top_species == params.noise_class:
        return {
            "status": "unknown/uncertain",
            "confirmed_species": None,
            "reason": f"Top-1 прогноз распознан как фоновый класс ({params.noise_class})",
            "top_1_species": top_species,
            "top_1_probability": top_probability,
        }
    if top_probability < params.confidence_threshold:
        return {
            "status": "unknown/uncertain",
            "confirmed_species": None,
            "reason": (
                f"Уверенность {top_probability:.3f} ниже порога {params.confidence_threshold:.3f}"
            ),
            "top_1_species": top_species,
            "top_1_probability": top_probability,
        }
    return {
        "status": "detected",
        "confirmed_species": top_species,
        "reason": "Уверенность выше порога",
        "top_1_species": top_species,
        "top_1_probability": top_probability,
    }


def make_windows_from_probability_series(
    series: Sequence[Dict[str, float]],
    params: Optional[VotingParams] = None,
    window_seconds: Optional[float] = None,
    stride_seconds: Optional[float] = None,
) -> List[WindowPrediction]:
    """
    Собрать список `WindowPrediction` из синтетической последовательности per-window вероятностей.

    Используется в тестах (критерий приёмки Задачи 1) и для QA-пересчёта сохранённых окон.
    """
    params = params or VotingParams()
    window = window_seconds if window_seconds is not None else params.window_seconds
    stride = stride_seconds if stride_seconds is not None else params.stride_seconds
    windows: List[WindowPrediction] = []
    for i, probs in enumerate(series):
        decision = classify_probabilities(probs, params)
        start = i * stride
        windows.append(
            WindowPrediction(
                index=i,
                start_sec=start,
                end_sec=start + window,
                top_species=decision["top_1_species"],
                top_probability=float(decision["top_1_probability"]),
                status=decision["status"],
                reason=decision["reason"],
                probabilities=dict(probs),
            )
        )
    return windows


def vote_windows(
    windows: Sequence[WindowPrediction],
    params: Optional[VotingParams] = None,
) -> List[ConfirmedDetection]:
    """
    Подтвердить виды по последовательности окон (правило K из M).

    Вид подтверждается, если он голосовал как минимум в `min_agree` (K) из последних
    `votes_required` (M) окон. Соседние подтверждения одного вида склеиваются в один
    интервал детекции, одиночные всплески не подтверждаются вовсе.
    """
    params = params or VotingParams()
    buffer: List[Optional[WindowPrediction]] = []
    confirmed: List[ConfirmedDetection] = []
    open_detections: Dict[str, ConfirmedDetection] = {}

    for window in windows:
        buffer.append(window if window.vote_class else None)
        if len(buffer) > params.votes_required:
            buffer.pop(0)

        votes_by_species: Dict[str, int] = {}
        for candidate in buffer:
            if candidate is not None and candidate.vote_class:
                votes_by_species[candidate.vote_class] = votes_by_species.get(candidate.vote_class, 0) + 1
        if not votes_by_species:
            continue

        species, votes = max(votes_by_species.items(), key=lambda kv: (kv[1], kv[0]))
        if votes < params.min_agree:
            continue

        supporting_windows = [
            candidate
            for candidate in buffer
            if candidate is not None and candidate.vote_class == species
        ]
        if not supporting_windows:
            continue
        start_sec = min(w.start_sec for w in supporting_windows)
        end_sec = max(w.end_sec for w in supporting_windows)
        confidences = [w.top_probability for w in supporting_windows]

        existing = open_detections.get(species)
        if existing is not None and start_sec <= existing.end_sec + 1e-6:
            existing.end_sec = max(existing.end_sec, end_sec)
            existing.confidence = max(existing.confidence, max(confidences))
            existing.mean_confidence = (existing.mean_confidence + sum(confidences) / len(confidences)) / 2
            existing.votes = max(existing.votes, votes)
            existing.windows_evaluated = max(existing.windows_evaluated, len(buffer))
            existing.window_indices = sorted(set(existing.window_indices) | {w.index for w in supporting_windows})
        else:
            detection = ConfirmedDetection(
                species=species,
                start_sec=start_sec,
                end_sec=end_sec,
                confidence=max(confidences),
                mean_confidence=sum(confidences) / len(confidences),
                votes=votes,
                windows_evaluated=len(buffer),
                window_indices=sorted({w.index for w in supporting_windows}),
            )
            confirmed.append(detection)
            open_detections[species] = detection

    return confirmed


def confirm_detection(
    windows: Sequence[WindowPrediction],
    params: Optional[VotingParams] = None,
) -> ConfirmationResult:
    """Собрать `ConfirmationResult` из готовых окон (чистая функция, без аудио)."""
    params = params or VotingParams()
    clip_duration = max((w.end_sec for w in windows), default=0.0)
    return ConfirmationResult(
        clip_duration_sec=clip_duration,
        target_sr=params.target_sr,
        raw_windows=list(windows),
        confirmed_detections=vote_windows(windows, params),
    )


# ---------------------------------------------------------------------------
# Обёртка над моделью репозитория модели
# ---------------------------------------------------------------------------


class ChunkAnalyzer:
    """
    Прогон чанка аудио через многократную проверку.

    Основной путь — `AudioPredictor.predict_sliding_window()` из `inference.py`
    репозитория модели (нарезка окон, TTA и голосование там). Если модуль недоступен
    или в нём ещё нет `predict_sliding_window`, используется локальная реализация той же
    спецификации: окна режутся здесь, а голосование считает `vote_windows`.
    """

    def __init__(
        self,
        predictor: Optional[Any] = None,
        model_path: Optional[Path] = None,
        classes_path: Optional[Path] = None,
        config: Optional[Dict[str, Any]] = None,
        use_tta: Optional[bool] = None,
        params: Optional[VotingParams] = None,
    ):
        settings = get_settings()
        self.params = params or load_voting_params()
        self.use_tta = settings.use_tta if use_tta is None else use_tta
        self.model_path = Path(model_path) if model_path else settings.model_path
        self.classes_path = Path(classes_path) if classes_path else settings.classes_path
        self.predictor = predictor if predictor is not None else self._build_predictor(config)

    # -- построение предиктора ------------------------------------------
    def _build_predictor(self, config: Optional[Dict[str, Any]]) -> Optional[Any]:
        module = import_model_module()
        if module is None or not hasattr(module, "AudioPredictor"):
            return None
        classes: Optional[List[str]] = None
        if self.classes_path.exists():
            import json

            with open(self.classes_path, "r", encoding="utf-8") as f:
                classes = json.load(f)
        try:
            return module.AudioPredictor(
                model_path=self.model_path if self.model_path.exists() else None,
                classes=classes,
                config=config,
            )
        except Exception:  # pragma: no cover - модель не инициализировалась
            return None

    @property
    def uses_repo_voting(self) -> bool:
        return self.predictor is not None and hasattr(self.predictor, "predict_sliding_window")

    # -- основной API ----------------------------------------------------
    def analyze(self, audio, sr: int) -> ConfirmationResult:
        """
        Проверить чанк аудио (numpy, mono/float32) и вернуть сырые окна + подтверждённые детекции.

        Временные метки окон — секунды **от начала чанка**; абсолютное время получается
        через `ConfirmationResult.with_time_offset(start_sec)` (Unix-время начала записи).
        """
        import numpy as np

        duration_sec = float(len(audio)) / float(sr) if sr else 0.0
        if self.predictor is None:
            return ConfirmationResult(
                clip_duration_sec=duration_sec,
                target_sr=self.params.target_sr,
                source="unavailable",
                tta_used=False,
            )

        if self.uses_repo_voting:
            raw = self.predictor.predict_sliding_window(
                np.asarray(audio),
                sr=sr,
                use_tta=self.use_tta,
            )
            return self._parse_repo_result(raw, duration_sec)

        # --- запасной путь: окна режем здесь, голосуем локально ---
        windows = self._score_windows_locally(np.asarray(audio), sr)
        result = confirm_detection(windows, self.params)
        result.source = "local_windowing"
        result.tta_used = self.use_tta
        return result

    def analyze_file(self, path: Path, sr: Optional[int] = None) -> ConfirmationResult:
        """Прогон аудиофайла (используется в CLI и тестах)."""
        import numpy as np
        import soundfile as sf

        data, file_sr = sf.read(str(path), dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.mean(axis=1)
        return self.analyze(np.asarray(data), sr or file_sr)

    # -- разбор вывода репозитория модели -------------------------------
    def _parse_repo_result(self, raw: Dict[str, Any], duration_sec: float) -> ConfirmationResult:
        windows = [
            WindowPrediction(
                index=int(w.get("window_index", i)),
                start_sec=float(w.get("time_range_sec", [0.0, 0.0])[0]),
                end_sec=float(w.get("time_range_sec", [0.0, 0.0])[1]),
                top_species=w.get("top_1_species"),
                top_probability=float(w.get("top_1_probability", 0.0) or 0.0),
                status=str(w.get("status", "unknown/uncertain")),
                reason=str(w.get("reason", "")),
                probabilities={
                    p["species"]: float(p["probability"]) for p in w.get("predictions", []) or []
                },
            )
            for i, w in enumerate(raw.get("windows", []) or [])
        ]
        detections = self._spans_from_repo_votes(raw.get("confirmed_detections") or [], windows)
        if not detections and windows:
            detections = vote_windows(windows, self.params)
        return ConfirmationResult(
            clip_duration_sec=float(raw.get("clip_duration_sec", duration_sec)),
            target_sr=self.params.target_sr,
            raw_windows=windows,
            confirmed_detections=detections,
            source="inference.predict_sliding_window",
            tta_used=self.use_tta,
        )

    @staticmethod
    def _spans_from_repo_votes(
        repo_detections: Sequence[Dict[str, Any]],
        windows: Sequence[WindowPrediction],
    ) -> List[ConfirmedDetection]:
        """
        Превратить решения `inference.predict_sliding_window` в интервалы детекций.

        Репозиторий модели отдаёт для подтверждения момент старта окна и число голосов;
        серверу нужен полный интервал (начало первого согласного окна → конец последнего),
        чтобы корректно склеивать перекрывающиеся чанки без задвоений (Задача 3).
        """
        result: List[ConfirmedDetection] = []
        for det in repo_detections:
            species = det.get("species")
            if not species:
                continue
            supporting = [
                w for w in windows if w.top_species == species and w.status == "detected"
            ]
            if supporting:
                start_sec = min(w.start_sec for w in supporting)
                end_sec = max(w.end_sec for w in supporting)
                confidences = [w.top_probability for w in supporting]
            else:  # деградационный случай: окна не отданы, доверяем времени старта
                start_sec = float(det.get("time_sec", 0.0))
                end_sec = start_sec
                confidences = []
            votes = int(det.get("votes", len(supporting)))
            windows_evaluated = int(det.get("windows_evaluated", votes) or votes)
            previous = result[-1] if result else None
            if previous is not None and previous.species == species and start_sec <= previous.end_sec + 1e-6:
                previous.end_sec = max(previous.end_sec, end_sec)
                previous.confidence = max(previous.confidence, max(confidences or [0.0]))
                previous.votes = max(previous.votes, votes)
                previous.windows_evaluated = max(previous.windows_evaluated, windows_evaluated)
                previous.window_indices = sorted(set(previous.window_indices) | {w.index for w in supporting})
            else:
                result.append(
                    ConfirmedDetection(
                        species=species,
                        start_sec=start_sec,
                        end_sec=end_sec,
                        confidence=max(confidences) if confidences else 0.0,
                        mean_confidence=(sum(confidences) / len(confidences)) if confidences else 0.0,
                        votes=votes,
                        windows_evaluated=windows_evaluated,
                        window_indices=sorted({w.index for w in supporting}),
                    )
                )
        return result

    # -- локальная нарезка окон (запасной путь) --------------------------
    def _score_windows_locally(self, audio, sr: int) -> List[WindowPrediction]:
        """Нарезка перекрывающимися окнами + батч-инференс (когда нет predict_sliding_window)."""
        params = self.params
        total_samples = int(len(audio))
        window_size = int(params.target_sr * params.clip_seconds)
        stride = int(params.target_sr * params.stride_seconds)
        if total_samples == 0:
            return []
        if total_samples <= window_size:
            starts = [0]
        else:
            starts = list(range(0, total_samples - window_size + 1, stride))
            if starts[-1] + window_size < total_samples:
                starts.append(max(0, total_samples - window_size))

        windows: List[WindowPrediction] = []
        for i, start in enumerate(starts):
            chunk = audio[start : start + window_size]
            result = self.predictor.predict_window(chunk, use_tta=self.use_tta)
            windows.append(
                WindowPrediction(
                    index=i,
                    start_sec=start / params.target_sr,
                    end_sec=(start + len(chunk)) / params.target_sr,
                    top_species=result.get("top_1_species"),
                    top_probability=float(result.get("top_1_probability", 0.0) or 0.0),
                    status=str(result.get("status", "unknown/uncertain")),
                    reason=str(result.get("reason", "")),
                    probabilities={
                        p["species"]: float(p["probability"]) for p in result.get("predictions", []) or []
                    },
                )
            )
        return windows


_analyzer: Optional[ChunkAnalyzer] = None
#: Фабрика анализатора. Подменяется в тестах: настоящий `ChunkAnalyzer` грузит ONNX,
#: а в репозитории модель хранится как git-LFS-указатель и недоступна.
_analyzer_factory: Optional[Callable[[], ChunkAnalyzer]] = None


def get_chunk_analyzer() -> ChunkAnalyzer:
    """Ленивая инициализация общего анализатора (модель грузится один раз на процесс)."""
    global _analyzer
    if _analyzer is None:
        _analyzer = _analyzer_factory() if _analyzer_factory else ChunkAnalyzer()
    return _analyzer


def set_chunk_analyzer_factory(factory: Optional[Callable[[], ChunkAnalyzer]]) -> None:
    """Подменить создание анализатора (только для тестов)."""
    global _analyzer, _analyzer_factory
    _analyzer_factory = factory
    _analyzer = None


def reset_chunk_analyzer() -> None:
    """Сбросить кэш анализатора (используется в тестах)."""
    global _analyzer, _analyzer_factory
    _analyzer = None
    _analyzer_factory = None
