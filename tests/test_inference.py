import numpy as np
import torch
import torch.nn as nn
from inference import AudioPredictor


class DummyModel(nn.Module):
    """Модель-заглушка с настраиваемыми логитами для тестирования порогов и шума."""

    def __init__(self, logits_value: torch.Tensor):
        super().__init__()
        self.logits_value = logits_value

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        batch_size = x.size(0)
        return self.logits_value.unsqueeze(0).repeat(batch_size, 1)


def test_inference_noise_class_returns_uncertain():
    classes = ["anas_platyrhynchos", "ardea_cinerea", "_noise"]
    # Logits where _noise has highest score
    logits = torch.tensor([1.0, 2.0, 10.0])
    model = DummyModel(logits)
    predictor = AudioPredictor(classes=classes, pytorch_model=model)

    synthetic_audio = np.random.randn(32000 * 5).astype(np.float32)
    result = predictor.predict_window(synthetic_audio, conf_threshold=0.6)

    assert result["status"] == "unknown/uncertain"
    assert result["confirmed_species"] is None
    assert result["top_1_species"] == "_noise"


def test_inference_low_confidence_returns_uncertain():
    classes = ["anas_platyrhynchos", "ardea_cinerea", "_noise"]
    # Equal distribution among classes (each ~0.33 probability)
    logits = torch.tensor([1.0, 1.0, 1.0])
    model = DummyModel(logits)
    predictor = AudioPredictor(classes=classes, pytorch_model=model)

    synthetic_audio = np.random.randn(32000 * 5).astype(np.float32)
    result = predictor.predict_window(synthetic_audio, conf_threshold=0.6)

    assert result["status"] == "unknown/uncertain"
    assert result["confirmed_species"] is None
    assert result["top_1_probability"] < 0.6


def test_inference_high_confidence_detected():
    classes = ["anas_platyrhynchos", "ardea_cinerea", "_noise"]
    # Very high probability for anas_platyrhynchos
    logits = torch.tensor([10.0, 1.0, 1.0])
    model = DummyModel(logits)
    predictor = AudioPredictor(classes=classes, pytorch_model=model)

    synthetic_audio = np.random.randn(32000 * 5).astype(np.float32)
    result = predictor.predict_window(synthetic_audio, conf_threshold=0.6)

    assert result["status"] == "detected"
    assert result["confirmed_species"] == "anas_platyrhynchos"
    assert result["top_1_probability"] > 0.6


def test_sliding_window_voting():
    classes = ["anas_platyrhynchos", "ardea_cinerea", "_noise"]

    class SequentialModel(nn.Module):
        def __init__(self):
            super().__init__()
            self.call_count = 0

        def forward(self, x: torch.Tensor):
            self.call_count += 1
            # Window 1: anas (high confidence)
            # Window 2: anas (high confidence) -> 2 agreements = confirmed!
            # Window 3: noise
            logits = torch.tensor([10.0, 1.0, 1.0])
            return logits.unsqueeze(0).repeat(x.size(0), 1)

    model = SequentialModel()
    predictor = AudioPredictor(classes=classes, pytorch_model=model)

    # 10 seconds audio (multiple windows)
    long_audio = np.random.randn(32000 * 10).astype(np.float32)
    result = predictor.predict_sliding_window(long_audio, conf_threshold=0.6)

    assert result["overall_status"] == "detected"
    assert result["confirmed_species"] == "anas_platyrhynchos"
    assert len(result["windows"]) >= 2
