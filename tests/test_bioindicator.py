import json
from unittest.mock import patch, MagicMock
from bioindicator.scorer import WetlandBioindicatorScorer, load_species_weights
from bioindicator.weather_client import fetch_weather_context


def test_species_weights_structure():
    weights = load_species_weights()
    assert len(weights) > 50, "Should contain comprehensive list of whitelist species"
    for sp, data in weights.items():
        assert "wetland_affinity" in data
        assert data["wetland_affinity"] is None, "Affinity must be null pending expert calibration"
        assert "ru" in data


def test_scorer_with_uncalibrated_weights():
    scorer = WetlandBioindicatorScorer()
    detections = [
        {"species": "anas_platyrhynchos"},
        {"species": "botaurus_stellaris"},
    ]
    res = scorer.calculate_score(detections)
    assert res["status"] == "ok"
    assert res["total_detections"] == 2
    assert res["calibrated"] is False
    assert res["wetland_index"] is None
    assert res["warning"] is not None


def test_scorer_with_expert_calibrated_weights():
    custom_weights = {
        "anas_platyrhynchos": {"wetland_affinity": 0.8, "ru": "Кряква"},
        "botaurus_stellaris": {"wetland_affinity": 1.0, "ru": "Выпь"},
        "parus_major": {"wetland_affinity": 0.1, "ru": "Синица"},
    }
    scorer = WetlandBioindicatorScorer(custom_weights=custom_weights)
    detections = [
        {"species": "anas_platyrhynchos"},
        {"species": "botaurus_stellaris"},
    ]
    res = scorer.calculate_score(detections)
    assert res["status"] == "ok"
    assert res["calibrated"] is True
    # (0.8 + 1.0) / 2 = 0.9
    assert abs(res["wetland_index"] - 0.9) < 1e-4


def test_weather_client_mock():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "hourly": {
            "temperature_2m": [15.0, 16.0, 17.0],
            "precipitation": [0.0, 0.5, 0.0],
            "relative_humidity_2m": [70, 75, 80],
            "wind_speed_10m": [10.0, 12.0, 8.0],
        }
    }
    with patch("requests.get", return_value=mock_resp):
        res = fetch_weather_context(55.75, 37.61, date_str="2026-05-15")
        assert res["status"] == "success"
        assert res["summary"]["avg_temperature_c"] == 16.0
        assert res["summary"]["total_precipitation_mm"] == 0.5
