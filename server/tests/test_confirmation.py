"""
Тесты Задачи 1 [P0] — спецификация многократной проверки (окна / TTA / голосование).

Критерий приёмки из td3.md: на синтетической последовательности per-window вероятностей
логика корректно отличает "один случайный всплеск" (не подтверждается) от "устойчивого
присутствия вида на протяжении нескольких окон" (подтверждается).
"""

from __future__ import annotations

import numpy as np
import pytest

from server.confirmation import (
    ChunkAnalyzer,
    ConfirmationResult,
    VotingParams,
    classify_probabilities,
    confirm_detection,
    import_model_module,
    load_voting_params,
    make_windows_from_probability_series,
    vote_windows,
)

RAVEN = "corvus_frugilegus"
STRIX = "strix_aluco"
NOISE = "_noise"


def probs(top: str, p: float, rest: float = 0.0) -> dict:
    """Вектор вероятностей: `top` с вероятностью `p`, остаток уходит на фоновый шум."""
    vector = {top: p, NOISE: rest}
    if rest == 0.0:
        vector = {top: p}
    return vector


@pytest.fixture
def params() -> VotingParams:
    return VotingParams(clip_seconds=5.0, stride_seconds=2.5, votes_required=3, min_agree=2)


# ---------------------------------------------------------------------------
# Параметры из model_config.json (единый источник констант)
# ---------------------------------------------------------------------------


def test_voting_params_loaded_from_model_config():
    params = load_voting_params()
    # Значения ниже — контракт model_config.json репозитория модели (td3.md, Задача 1 п.5).
    assert params.clip_seconds == pytest.approx(5.0)
    assert params.stride_seconds == pytest.approx(2.5)
    assert params.votes_required == 3
    assert params.min_agree == 2
    assert params.confidence_threshold == pytest.approx(0.6)
    assert list(params.tta_time_shift_ms) == [-200, 0, 200]


def test_voting_params_respect_overrides():
    params = VotingParams.from_config({"window_count_for_vote": 5, "min_agree_windows": 4})
    assert (params.votes_required, params.min_agree) == (5, 4)


# ---------------------------------------------------------------------------
# Ключевой критерий приёмки: всплеск vs устойчивое присутствие
# ---------------------------------------------------------------------------


def test_single_spike_is_not_confirmed(params):
    """Один разовый уверенный всплеск — не повод логировать обнаружение вида."""
    series = [
        probs(RAVEN, 0.95),           # случайный всплеск
        probs(NOISE, 0.90),
        probs(NOISE, 0.88),
        probs(RAVEN, 0.10),           # уверенность ниже порога
        probs(NOISE, 0.95),
    ]
    windows = make_windows_from_probability_series(series, params)
    assert vote_windows(windows, params) == []


def test_sustained_presence_is_confirmed(params):
    """Устойчивое присутствие вида на протяжении нескольких окон подтверждается."""
    series = [probs(NOISE, 0.9)] * 2 + [probs(RAVEN, p) for p in (0.88, 0.91, 0.93, 0.9, 0.87)]
    windows = make_windows_from_probability_series(series, params)
    detections = vote_windows(windows, params)

    assert len(detections) == 1
    detection = detections[0]
    assert detection.species == RAVEN
    assert detection.votes >= params.min_agree
    # Два согласованных окна склеиваются в один интервал детекции.
    assert detection.start_sec == pytest.approx(2 * params.stride_seconds)
    assert detection.end_sec == pytest.approx(6 * params.stride_seconds + params.window_seconds)
    assert detection.confidence == pytest.approx(0.93)
    assert detection.support_ratio >= params.min_agree / params.votes_required


def test_noise_and_below_threshold_never_vote(params):
    assert classify_probabilities({NOISE: 0.99}, params)["confirmed_species"] is None
    assert classify_probabilities({RAVEN: 0.59}, params)["confirmed_species"] is None
    assert classify_probabilities({RAVEN: 0.61}, params)["confirmed_species"] == RAVEN


def test_two_of_three_votes_required(params):
    """K=2 из M=3: две соседние пары подряд подтверждают вид, одиночные — нет."""
    series = [
        probs(NOISE, 0.9),
        probs(RAVEN, 0.9),
        probs(RAVEN, 0.9),
        probs(NOISE, 0.9),
    ]
    windows = make_windows_from_probability_series(series, params)
    detections = vote_windows(windows, params)
    assert [d.species for d in detections] == [RAVEN]
    assert detections[0].votes == 2
    assert detections[0].windows_evaluated == 3


def test_species_change_splits_detections(params):
    series = (
        [probs(NOISE, 0.9)]
        + [probs(RAVEN, 0.9)] * 3
        + [probs(NOISE, 0.9)]
        + [probs(STRIX, 0.9)] * 3
    )
    windows = make_windows_from_probability_series(series, params)
    detections = vote_windows(windows, params)
    assert [d.species for d in detections] == [RAVEN, STRIX]
    assert detections[0].end_sec <= detections[1].start_sec


def test_species_competing_in_same_window(params):
    """Вид-переходник, победивший по числу голосов, подтверждается; разовый — нет."""
    series = [
        probs(RAVEN, 0.90),
        {STRIX: 0.88, RAVEN: 0.06},
        {STRIX: 0.91, RAVEN: 0.04},
        {STRIX: 0.87, RAVEN: 0.05},
    ]
    windows = make_windows_from_probability_series(series, params)
    detections = vote_windows(windows, params)
    assert [d.species for d in detections] == [STRIX]
    assert detections[0].window_indices == [1, 2, 3]


def test_confirmation_result_exposes_raw_and_confirmed_separately(params):
    """Потребители (карта, экспорт) работают только с confirmed; raw — для QA."""
    series = [probs(NOISE, 0.9), probs(RAVEN, 0.95), probs(RAVEN, 0.95), probs(NOISE, 0.9)]
    result = confirm_detection(make_windows_from_probability_series(series, params), params)

    assert isinstance(result, ConfirmationResult)
    assert result.total_windows == 4
    assert result.has_detection and result.top_species == RAVEN
    assert len(result.confirmed_detections) == 1
    raw_dump = result.to_dict()
    assert len(raw_dump["raw_windows"]) == 4
    assert raw_dump["overall_status"] == "detected"
    assert raw_dump["raw_windows"][0]["probabilities"][NOISE] == pytest.approx(0.9)


def test_result_time_offset_shifts_all_timestamps(params):
    series = [probs(NOISE, 0.9), probs(RAVEN, 0.9), probs(RAVEN, 0.9)]
    result = confirm_detection(make_windows_from_probability_series(series, params), params)
    shifted = result.with_time_offset(1000.0)
    assert shifted.raw_windows[0].start_sec == pytest.approx(1000.0)
    assert shifted.confirmed_detections[0].end_sec == pytest.approx(
        result.confirmed_detections[0].end_sec + 1000.0
    )


# ---------------------------------------------------------------------------
# Интеграция с репозиторием модели (td3.md, раздел 0)
# ---------------------------------------------------------------------------


class StubPredictor:
    """Заглушка `AudioPredictor` репозитория модели: окна уже посчитаны."""

    def __init__(self, windows, confirmed):
        self._windows = windows
        self._confirmed = confirmed
        self.use_tta_seen: object = None

    def predict_sliding_window(self, audio, sr=32000, use_tta=False):
        self.use_tta_seen = use_tta
        return {
            "overall_status": "detected" if self._confirmed else "unknown/uncertain",
            "confirmed_species": self._confirmed[0]["species"] if self._confirmed else None,
            "total_windows": len(self._windows),
            "confirmed_detections": list(self._confirmed),
            "windows": list(self._windows),
        }


class StubLegacyPredictor:
    """Старая версия репо модели: есть только предсказание одного окна."""

    def __init__(self, window_results):
        self._window_results = window_results
        self.calls: list = []

    def predict_window(self, wav_chunk, conf_threshold=None, top_k=3, use_tta=False):
        self.calls.append(len(wav_chunk))
        return self._window_results[len(self.calls) - 1]


def _repo_window(index, start, species, prob, status="detected"):
    return {
        "window_index": index,
        "time_range_sec": [start, start + 5.0],
        "status": status,
        "confirmed_species": species if status == "detected" else None,
        "reason": "",
        "top_1_species": species,
        "top_1_probability": prob,
        "predictions": [{"species": species, "probability": prob}],
    }


def test_uses_repo_sliding_window_voting_and_enriches_span():
    """Основной путь — `inference.predict_sliding_window`; сервер дополняет интервалом."""
    windows = [
        _repo_window(0, 0.0, NOISE, 0.95, status="unknown/uncertain"),
        _repo_window(1, 2.5, RAVEN, 0.93),
        _repo_window(2, 5.0, RAVEN, 0.91),
        _repo_window(3, 7.5, RAVEN, 0.89),
    ]
    confirmed = [
        {"species": RAVEN, "votes": 3, "windows_evaluated": 3, "window_index": 1, "time_sec": 2.5}
    ]
    predictor = StubPredictor(windows, confirmed)
    analyzer = ChunkAnalyzer(predictor=predictor, use_tta=True)

    result = analyzer.analyze(np.zeros(32000 * 8, dtype=np.float32), 32000)

    assert analyzer.uses_repo_voting is True
    assert predictor.use_tta_seen is True  # TTA пробрасывается в модель репо
    assert result.source == "inference.predict_sliding_window"
    assert result.total_windows == 4
    assert len(result.confirmed_detections) == 1
    detection = result.confirmed_detections[0]
    assert detection.species == RAVEN
    # Интервал собран из всех согласных окон репозитория модели: 2.5 .. 12.5
    assert detection.start_sec == pytest.approx(2.5)
    assert detection.end_sec == pytest.approx(12.5)


def test_falls_back_to_local_voting_when_repo_has_no_sliding_window():
    """Резервный путь: окна режем сами, голосуем той же спецификацией."""
    window_results = [
        {
            "status": "unknown/uncertain",
            "confirmed_species": None,
            "top_1_species": NOISE,
            "top_1_probability": 0.9,
            "reason": "noise",
            "predictions": [{"species": NOISE, "probability": 0.9}],
        },
        {
            "status": "detected",
            "confirmed_species": RAVEN,
            "top_1_species": RAVEN,
            "top_1_probability": 0.92,
            "reason": "",
            "predictions": [{"species": RAVEN, "probability": 0.92}],
        },
        {
            "status": "detected",
            "confirmed_species": RAVEN,
            "top_1_species": RAVEN,
            "top_1_probability": 0.9,
            "reason": "",
            "predictions": [{"species": RAVEN, "probability": 0.9}],
        },
    ]
    predictor = StubLegacyPredictor(window_results)
    analyzer = ChunkAnalyzer(predictor=predictor, use_tta=False)

    result = analyzer.analyze(np.zeros(32000 * 8, dtype=np.float32), 32000)

    assert analyzer.uses_repo_voting is False
    assert result.source == "local_windowing"
    assert result.total_windows == 3
    assert all(c == 160000 for c in predictor.calls)  # окно ровно clip_seconds
    assert [d.species for d in result.confirmed_detections] == [RAVEN]


def test_analyzer_without_model_reports_unavailable():
    analyzer = ChunkAnalyzer(predictor=None)
    analyzer.predictor = None  # модель не инициализировалась (нет весов/LFS-файла)
    result = analyzer.analyze(np.zeros(16000, dtype=np.float32), 16000)
    assert result.source == "unavailable"
    assert result.total_windows == 0
    assert result.has_detection is False


def test_model_repo_module_is_discoverable():
    """Репозиторий модели лежит рядом и импортируется как внешняя библиотека."""
    module = import_model_module()
    assert module is not None, "модуль inference репозитория модели должен находиться через sys.path"
    assert hasattr(module, "AudioPredictor")
    # Спецификация окон/голосования в репозитории модели уже есть — дублировать её не нужно.
    assert hasattr(module.AudioPredictor, "predict_sliding_window")