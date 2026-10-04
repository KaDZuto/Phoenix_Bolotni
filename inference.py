#!/usr/bin/env python3
"""
Единая точка предсказания модели классификации птиц Phoenix_Bolotni.
Поддерживает форматы весов PyTorch (.pth) и ONNX (.onnx).
Реализует порог уверенности, обработку класса _noise и многократную проверку (sliding window voting).
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import torch
import torch.nn.functional as F

from audio_utils import (
    CONFIG,
    TARGET_SR,
    TARGET_LEN,
    load_audio,
    audio_to_logmel,
    load_config,
    resample_if_needed,
)


def load_classes(classes_path: Optional[Union[str, Path]] = None) -> List[str]:
    """Поиск и загрузка списка классов."""
    if classes_path:
        p = Path(classes_path)
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    candidates = [
        Path("metadata/classes.json"),
        Path("models/classes.json"),
        Path("artifacts/classes.json"),
        Path(__file__).parent / "metadata" / "classes.json",
        Path(__file__).parent / "models" / "classes.json",
    ]
    for c in candidates:
        if c.exists():
            with open(c, "r", encoding="utf-8") as f:
                return json.load(f)
    raise FileNotFoundError("Файл classes.json не найден ни по одному из стандартных путей.")


class AudioPredictor:
    """Обертка над моделью (PyTorch или ONNX) для инференса."""

    def __init__(
        self,
        model_path: Optional[Union[str, Path]] = None,
        classes: Optional[List[str]] = None,
        config: Optional[Dict[str, Any]] = None,
        session: Any = None,
        pytorch_model: Any = None,
    ):
        self.config = config or load_config()
        self.conf_threshold = float(self.config.get("confidence_threshold", 0.6))
        self.noise_class = self.config.get("noise_class_slug", "_noise")
        self.target_sr = int(self.config.get("target_sr", TARGET_SR))
        self.window_stride_sec = float(self.config.get("window_stride_sec", 2.5))
        self.window_count_for_vote = int(self.config.get("window_count_for_vote", 3))
        self.min_agree_windows = int(self.config.get("min_agree_windows", 2))
        self.tta_time_shift_ms = self.config.get("tta_time_shift_ms", [-200, 0, 200])

        self.classes = classes or []
        self.model_path = Path(model_path) if model_path else None
        self.session = session
        self.pytorch_model = pytorch_model

        if self.model_path and not self.session and not self.pytorch_model:
            self._load_backend()

    def _load_backend(self):
        suffix = self.model_path.suffix.lower()
        if suffix == ".onnx":
            import onnxruntime as ort
            self.session = ort.InferenceSession(
                str(self.model_path), providers=["CPUExecutionProvider"]
            )
            self.input_name = self.session.get_inputs()[0].name
        else:
            # PyTorch .pth
            from torchvision.models import efficientnet_b0
            import torch.nn as nn

            num_classes = len(self.classes)
            model = efficientnet_b0(weights=None)
            in_f = model.classifier[1].in_features
            model.classifier[1] = nn.Linear(in_f, num_classes)

            ckpt = torch.load(self.model_path, map_location="cpu")
            if isinstance(ckpt, dict) and "model" in ckpt:
                model.load_state_dict(ckpt["model"])
                if not self.classes and "classes" in ckpt:
                    self.classes = ckpt["classes"]
            elif isinstance(ckpt, dict) and "state_dict" in ckpt:
                model.load_state_dict(ckpt["state_dict"])
            elif isinstance(ckpt, dict):
                model.load_state_dict(ckpt)
            else:
                model = ckpt

            model.eval()
            self.pytorch_model = model

    def _forward_batch(self, x: torch.Tensor) -> np.ndarray:
        """Прямой проход батча лог-мел спектрограмм [B, 3, 128, 501]. Возвращает softmax вероятности [B, num_classes]."""
        if self.session is not None:
            inp = x.numpy().astype(np.float32)
            logits = self.session.run(None, {self.input_name: inp})[0]
            # Softmax
            exp_l = np.exp(logits - np.max(logits, axis=1, keepdims=True))
            probs = exp_l / np.sum(exp_l, axis=1, keepdims=True)
            return probs
        elif self.pytorch_model is not None:
            self.pytorch_model.eval()
            with torch.no_grad():
                logits = self.pytorch_model(x)
                probs = F.softmax(logits, dim=1).cpu().numpy()
            return probs
        else:
            raise RuntimeError("Модель не инициализирована.")

    def predict_window(
        self,
        wav_chunk: np.ndarray,
        conf_threshold: Optional[float] = None,
        top_k: int = 3,
        use_tta: bool = False,
    ) -> Dict[str, Any]:
        """
        Предсказание для одного 5-секундного окна.
        Возвращает top-k видов, вероятности и статус (detected / unknown/uncertain).
        """
        thresh = conf_threshold if conf_threshold is not None else self.conf_threshold

        # Подготовка окна до TARGET_LEN
        target_len = int(self.target_sr * float(self.config.get("clip_seconds", 5.0)))
        if len(wav_chunk) < target_len:
            pad = target_len - len(wav_chunk)
            chunk = np.pad(wav_chunk, (0, pad), mode="constant")
        elif len(wav_chunk) > target_len:
            start = (len(wav_chunk) - target_len) // 2
            chunk = wav_chunk[start : start + target_len]
        else:
            chunk = wav_chunk

        # TTA со сдвигом по времени
        shifts_ms = self.tta_time_shift_ms if use_tta else [0]
        specs = []
        for ms in shifts_ms:
            shift_samples = int((ms / 1000.0) * self.target_sr)
            shifted = np.roll(chunk, shift_samples) if shift_samples != 0 else chunk
            spec = audio_to_logmel(shifted, self.target_sr)
            specs.append(spec)

        batch = torch.stack(specs, dim=0)  # [N_TTA, 3, 128, 501]
        probs_all = self._forward_batch(batch)  # [N_TTA, num_classes]
        probs = np.mean(probs_all, axis=0)  # Усреднение TTA

        # Сортировка по убыванию вероятности
        ranked_indices = np.argsort(probs)[::-1]
        top_indices = ranked_indices[: min(top_k, len(self.classes))]

        top_predictions = []
        for idx in top_indices:
            name = self.classes[idx] if idx < len(self.classes) else f"class_{idx}"
            top_predictions.append({"species": name, "probability": float(probs[idx])})

        top_1 = top_predictions[0]
        top_1_species = top_1["species"]
        top_1_prob = top_1["probability"]

        # Проверка порога и фонового шума
        if top_1_species == self.noise_class:
            status = "unknown/uncertain"
            reason = f"Top-1 прогноз распознан как фоновый класс ({self.noise_class})"
            confirmed_species = None
        elif top_1_prob < thresh:
            status = "unknown/uncertain"
            reason = f"Уверенность {top_1_prob:.3f} ниже порога {thresh:.3f}"
            confirmed_species = None
        else:
            status = "detected"
            reason = "Уверенность выше порога"
            confirmed_species = top_1_species

        return {
            "status": status,
            "confirmed_species": confirmed_species,
            "reason": reason,
            "top_1_species": top_1_species,
            "top_1_probability": top_1_prob,
            "predictions": top_predictions,
        }

    def predict_sliding_window(
        self,
        audio: np.ndarray,
        sr: int = TARGET_SR,
        conf_threshold: Optional[float] = None,
        use_tta: bool = False,
    ) -> Dict[str, Any]:
        """
        Многократная проверка (голосование по скользящему окну).
        Нарезка длинного аудио с шагом window_stride_sec.
        Подтверждение вида только если min_agree_windows из последних window_count_for_vote согласны.
        """
        thresh = conf_threshold if conf_threshold is not None else self.conf_threshold
        audio = resample_if_needed(audio, sr, self.target_sr)

        clip_sec = float(self.config.get("clip_seconds", 5.0))
        window_size = int(self.target_sr * clip_sec)
        stride = int(self.target_sr * self.window_stride_sec)

        total_samples = len(audio)
        if total_samples <= window_size:
            starts = [0]
        else:
            starts = list(range(0, total_samples - window_size + 1, stride))
            if (total_samples - starts[-1]) > stride // 2 and starts[-1] + window_size < total_samples:
                starts.append(total_samples - window_size)

        window_results = []
        recent_top_classes: List[Optional[str]] = []
        confirmed_detections = []

        for w_idx, start in enumerate(starts):
            chunk = audio[start : start + window_size]
            res = self.predict_window(chunk, conf_threshold=thresh, use_tta=use_tta)
            time_start_sec = start / self.target_sr
            time_end_sec = (start + len(chunk)) / self.target_sr
            res["window_index"] = w_idx
            res["time_range_sec"] = [round(time_start_sec, 2), round(time_end_sec, 2)]
            window_results.append(res)

            # Проверка для голосования
            if res["status"] == "detected":
                recent_top_classes.append(res["confirmed_species"])
            else:
                recent_top_classes.append(None)

            # Ограничиваем буфер последних окон
            if len(recent_top_classes) > self.window_count_for_vote:
                recent_top_classes.pop(0)

            # Подсчет голосов
            active_votes = [c for c in recent_top_classes if c is not None]
            if active_votes:
                from collections import Counter
                counts = Counter(active_votes)
                top_voted_species, vote_count = counts.most_common(1)[0]
                if vote_count >= self.min_agree_windows:
                    if not confirmed_detections or confirmed_detections[-1]["species"] != top_voted_species:
                        confirmed_detections.append({
                            "species": top_voted_species,
                            "votes": vote_count,
                            "windows_evaluated": len(recent_top_classes),
                            "window_index": w_idx,
                            "time_sec": round(time_start_sec, 2),
                        })

        if confirmed_detections:
            overall_status = "detected"
            final_species = confirmed_detections[0]["species"]
        else:
            overall_status = "unknown/uncertain"
            final_species = None

        return {
            "overall_status": overall_status,
            "confirmed_species": final_species,
            "total_windows": len(window_results),
            "confirmed_detections": confirmed_detections,
            "windows": window_results,
        }


def predict(
    audio_path: Union[str, Path],
    model_path: Optional[Union[str, Path]] = None,
    classes_path: Optional[Union[str, Path]] = None,
    conf_threshold: float = 0.6,
    sliding_window: bool = False,
    use_tta: bool = False,
) -> Dict[str, Any]:
    """Основная функция для вызова инференса."""
    classes = load_classes(classes_path)
    predictor = AudioPredictor(model_path=model_path, classes=classes)

    wav, sr = load_audio(audio_path, target_sr=predictor.target_sr)

    if sliding_window or (len(wav) > predictor.target_len * 1.5):
        return predictor.predict_sliding_window(
            wav, sr=sr, conf_threshold=conf_threshold, use_tta=use_tta
        )
    else:
        return predictor.predict_window(
            wav, conf_threshold=conf_threshold, use_tta=use_tta
        )


def main():
    parser = argparse.ArgumentParser(description="Инференс модели распознавания птиц Phoenix_Bolotni")
    parser.add_argument("--audio", required=True, help="Путь к аудиофайлу .wav")
    parser.add_argument(
        "--model",
        default="models/best.pth",
        help="Путь к модели (.pth или .onnx, по умолчанию: models/best.pth)",
    )
    parser.add_argument(
        "--classes",
        default=None,
        help="Путь к classes.json (по умолчанию: поиск в metadata/ или models/)",
    )
    parser.add_argument(
        "--conf-threshold",
        type=float,
        default=0.6,
        help="Порог уверенности классификации (по умолчанию: 0.6)",
    )
    parser.add_argument(
        "--sliding-window",
        action="store_true",
        help="Включить скользящее окно с многократным голосованием",
    )
    parser.add_argument(
        "--tta",
        action="store_true",
        help="Включить test-time augmentation (сдвиги времени)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Путь для сохранения JSON отчёта",
    )
    args = parser.parse_args()

    result = predict(
        audio_path=args.audio,
        model_path=args.model,
        classes_path=args.classes,
        conf_threshold=args.conf_threshold,
        sliding_window=args.sliding_window,
        use_tta=args.tta,
    )

    print("\n--- Результаты классификации ---")
    print(f"Статус: {result.get('status') or result.get('overall_status')}")
    print(f"Подтверждённый вид: {result.get('confirmed_species')}")
    if "predictions" in result:
        print("Top-3 предсказания:")
        for p in result["predictions"]:
            print(f"  • {p['species']}: {p['probability']*100:.2f}%")

    if args.output:
        out_p = Path(args.output)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"Отчёт сохранен в {out_p}")


if __name__ == "__main__":
    main()
