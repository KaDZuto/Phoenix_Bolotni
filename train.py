#!/usr/bin/env python3
"""
Основной скрипт обучения модели классификации птиц Phoenix_Bolotni.
Использует архитектуру EfficientNet-B0 на Log-Mel спектрограммах.
Поддерживает CLI параметры, расширенные аугментации, балансировку классов,
сохранение метрик (classification report, confusion matrix, history, run_config)
и вывод худших по F1 классов для экологов.
"""

import argparse
import csv
import json
import os
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import soundfile as sf
import torch
import torch.nn as nn
from sklearn.metrics import classification_report, confusion_matrix, f1_score
from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler
from torchvision.models import EfficientNet_B0_Weights, efficientnet_b0
from tqdm import tqdm

from audio_utils import (
    CONFIG,
    HOP,
    N_FFT,
    N_MELS,
    TARGET_LEN,
    TARGET_SR,
    apply_waveform_augmentations,
    audio_to_logmel,
    crop_or_pad,
    load_config,
    mixup_batch,
    resample_if_needed,
    spec_augment,
)


def set_seed(seed: int = 42):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


class BirdLogMelDataset(Dataset):
    def __init__(
        self,
        jsonl_path: Path,
        train: bool = True,
        gain_aug: bool = True,
        time_shift_aug: bool = True,
        pitch_aug: bool = True,
        noise_aug: bool = False,
    ):
        self.paths = []
        self.labels = []
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                data = json.loads(line)
                self.paths.append(data["path"])
                self.labels.append(data["label"])
        self.labels = np.array(self.labels, dtype=np.int64)
        self.train = train
        self.gain_aug = gain_aug
        self.time_shift_aug = time_shift_aug
        self.pitch_aug = pitch_aug
        self.noise_aug = noise_aug

    def __len__(self):
        return len(self.paths)

    def __getitem__(self, i):
        p = self.paths[i]
        y = int(self.labels[i])
        wav, sr = sf.read(p, always_2d=True)
        wav = wav.mean(axis=1).astype(np.float32)
        wav = resample_if_needed(wav, sr, TARGET_SR)
        wav = crop_or_pad(wav, TARGET_LEN, train=self.train)

        if self.train:
            wav = apply_waveform_augmentations(
                wav,
                sr=TARGET_SR,
                gain_aug=self.gain_aug,
                time_shift_aug=self.time_shift_aug,
                pitch_aug=self.pitch_aug,
                noise_aug=self.noise_aug,
            )

        x = audio_to_logmel(wav, TARGET_SR)
        if self.train:
            x = spec_augment(x)
        return x, y


def make_model(num_classes: int):
    model = efficientnet_b0(weights=EfficientNet_B0_Weights.IMAGENET1K_V1)
    in_f = model.classifier[1].in_features
    model.classifier[1] = nn.Linear(in_f, num_classes)
    return model


def save_confusion_matrix_plot(cm: np.ndarray, class_names: List[str], save_path: Path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        plt.figure(figsize=(10, 8))
        plt.imshow(cm, interpolation="nearest", cmap=plt.cm.Blues)
        plt.title("Confusion Matrix")
        plt.colorbar()
        tick_marks = np.arange(len(class_names))
        plt.xticks(tick_marks, class_names, rotation=45, ha="right", fontsize=8)
        plt.yticks(tick_marks, class_names, fontsize=8)
        plt.ylabel("Истинный класс")
        plt.xlabel("Предсказанный класс")
        plt.tight_layout()
        plt.savefig(save_path, dpi=150)
        plt.close()
    except Exception as e:
        print(f"⚠️ Ошибка сохранения графика confusion matrix: {e}")


def train(args):
    set_seed(args.seed)

    data_dir = Path(args.data_dir)
    artifacts_dir = Path(args.artifacts_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    classes_file = artifacts_dir / "classes.json"
    if not classes_file.exists():
        print(f"❌ Файл {classes_file} не найден. Сначала запустите prepare_data.py!")
        return

    with open(classes_file, "r", encoding="utf-8") as f:
        classes = json.load(f)
    num_classes = len(classes)

    print(f"🚀 Запуск обучения Phoenix_Bolotni на {num_classes} классов")
    print(f"Устройство: {args.device} | Эпох: {args.epochs} | Batch size: {args.batch_size} | LR: {args.lr}")

    train_jsonl = artifacts_dir / "train.jsonl"
    val_jsonl = artifacts_dir / "val.jsonl"

    train_ds = BirdLogMelDataset(
        train_jsonl,
        train=True,
        gain_aug=args.gain_aug,
        time_shift_aug=args.time_shift_aug,
        pitch_aug=args.pitch_aug,
        noise_aug=args.noise_aug,
    )
    val_ds = BirdLogMelDataset(val_jsonl, train=False)

    counts = np.bincount(train_ds.labels, minlength=num_classes)
    class_w = 1.0 / np.maximum(counts, 1)
    sample_w = class_w[train_ds.labels]
    sampler = WeightedRandomSampler(
        torch.tensor(sample_w, dtype=torch.double), len(sample_w), replacement=True
    )

    pin_mem = (args.device == "cuda")
    train_dl = DataLoader(
        train_ds, batch_size=args.batch_size, sampler=sampler, pin_memory=pin_mem
    )
    val_dl = DataLoader(val_ds, batch_size=args.batch_size, pin_memory=pin_mem)

    model = make_model(num_classes).to(args.device)
    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    scaler = torch.amp.GradScaler(enabled=(args.device == "cuda"))

    best_f1 = -1.0
    history = []
    best_pred, best_true = None, None

    for epoch in range(1, args.epochs + 1):
        model.train()
        total_train_loss = 0.0

        for x, y in tqdm(train_dl, desc=f"Epoch {epoch}/{args.epochs}"):
            x, y = x.to(args.device), y.to(args.device)
            optimizer.zero_grad()

            if args.mixup and random.random() < 0.5:
                mixed_x, y_a, y_b, lam = mixup_batch(x, y, alpha=0.2)
                with torch.amp.autocast(device_type=args.device if args.device == "cuda" else "cpu", enabled=(args.device == "cuda")):
                    logits = model(mixed_x)
                    loss = lam * criterion(logits, y_a) + (1.0 - lam) * criterion(logits, y_b)
            else:
                with torch.amp.autocast(device_type=args.device if args.device == "cuda" else "cpu", enabled=(args.device == "cuda")):
                    logits = model(x)
                    loss = criterion(logits, y)

            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()
            total_train_loss += loss.item()

        avg_train_loss = total_train_loss / max(1, len(train_dl))

        # Валидация
        model.eval()
        all_pred, all_true = [], []
        total_val_loss = 0.0

        with torch.no_grad():
            for x, y in val_dl:
                x, y = x.to(args.device), y.to(args.device)
                logits = model(x)
                v_loss = criterion(logits, y)
                total_val_loss += v_loss.item()
                all_pred.append(logits.argmax(1).cpu().numpy())
                all_true.append(y.cpu().numpy())

        avg_val_loss = total_val_loss / max(1, len(val_dl))
        all_pred = np.concatenate(all_pred) if all_pred else np.array([])
        all_true = np.concatenate(all_true) if all_true else np.array([])

        if len(all_true) > 0:
            val_f1 = f1_score(all_true, all_pred, average="macro")
        else:
            val_f1 = 0.0

        print(
            f"Epoch {epoch:02d} | Train Loss: {avg_train_loss:.4f} | "
            f"Val Loss: {avg_val_loss:.4f} | Val F1: {val_f1:.4f}"
        )

        history.append({
            "epoch": epoch,
            "train_loss": avg_train_loss,
            "val_loss": avg_val_loss,
            "val_macro_f1": val_f1,
        })

        if val_f1 > best_f1:
            best_f1 = val_f1
            best_pred = all_pred
            best_true = all_true
            torch.save({
                "model": model.state_dict(),
                "classes": classes,
                "config": {
                    "TARGET_SR": TARGET_SR,
                    "N_MELS": N_MELS,
                    "N_FFT": N_FFT,
                    "HOP": HOP,
                    "CLIP_SECONDS": CONFIG.get("clip_seconds", 5.0),
                },
            }, out_dir / "best.pth")
            print("⭐️ Новая лучшая модель сохранена в model_output/best.pth!")

    # --- СОХРАНЕНИЕ МЕТРИК И ОТЧЁТОВ ---
    print("\n📊 Формирование отчетов и метрик экспериментов...")

    # 1. Training history CSV
    with open(out_dir / "training_history.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["epoch", "train_loss", "val_loss", "val_macro_f1"])
        writer.writeheader()
        writer.writerows(history)

    # 2. Run config JSON
    import sklearn
    run_cfg = {
        "args": vars(args),
        "classes_count": num_classes,
        "classes": classes,
        "best_macro_f1": best_f1,
        "library_versions": {
            "torch": torch.__version__,
            "torchvision": sys.modules.get("torchvision").__version__ if "torchvision" in sys.modules else None,
            "sklearn": sklearn.__version__,
            "numpy": np.__version__,
            "soundfile": sf.__version__,
        },
    }
    with open(out_dir / "run_config.json", "w", encoding="utf-8") as f:
        json.dump(run_cfg, f, ensure_ascii=False, indent=2)

    # Сохраняем актуальный model_config.json в out_dir
    with open(out_dir / "model_config.json", "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, ensure_ascii=False, indent=2)

    # 3. Classification Report & Confusion Matrix
    if best_true is not None and len(best_true) > 0:
        target_names = [classes[i] for i in sorted(list(set(best_true) | set(best_pred)))]
        labels_present = sorted(list(set(best_true) | set(best_pred)))

        report_dict = classification_report(
            best_true, best_pred, labels=labels_present, target_names=target_names, output_dict=True, zero_division=0
        )
        with open(out_dir / "classification_report.json", "w", encoding="utf-8") as f:
            json.dump(report_dict, f, ensure_ascii=False, indent=2)

        # Classification report as CSV
        with open(out_dir / "classification_report.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["class", "precision", "recall", "f1-score", "support"])
            for cls_key, metrics in report_dict.items():
                if isinstance(metrics, dict):
                    writer.writerow([
                        cls_key,
                        f"{metrics.get('precision', 0):.4f}",
                        f"{metrics.get('recall', 0):.4f}",
                        f"{metrics.get('f1-score', 0):.4f}",
                        metrics.get("support", 0),
                    ])

        # Confusion Matrix
        cm = confusion_matrix(best_true, best_pred, labels=labels_present)
        with open(out_dir / "confusion_matrix.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([""] + target_names)
            for row_name, row in zip(target_names, cm):
                writer.writerow([row_name] + list(row))

        save_confusion_matrix_plot(cm, target_names, out_dir / "confusion_matrix.png")

        # 4. Вывод худших классов по F1 (Задача 7)
        print("\n" + "=" * 65)
        print("🔍 ХУДШИЕ КЛАССЫ ПО F1 (ТРЕБУЮТ ДОПОЛНИТЕЛЬНЫХ ДАННЫХ/ДОРАЗМЕТКИ):")
        print("=" * 65)
        per_class_f1 = []
        for c_name in target_names:
            if c_name in report_dict and isinstance(report_dict[c_name], dict):
                score = report_dict[c_name]["f1-score"]
                supp = report_dict[c_name]["support"]
                prec = report_dict[c_name]["precision"]
                rec = report_dict[c_name]["recall"]
                per_class_f1.append((c_name, score, prec, rec, supp))

        per_class_f1.sort(key=lambda item: item[1])
        print(f"{'Вид':30s} | {'F1':6s} | {'Precision':9s} | {'Recall':6s} | {'Кол-во':6s}")
        print("-" * 65)
        for c_name, score, prec, rec, supp in per_class_f1[:8]:
            print(f"{c_name:30s} | {score:.4f} | {prec:.4f}    | {rec:.4f} | {supp:6d}")
        print("=" * 65)

    print(f"\n✅ Обучение успешно завершено! Лучший Macro-F1: {best_f1:.4f}")
    print(f"Все артефакты сохранены в: {out_dir.resolve()}")


def parse_args():
    parser = argparse.ArgumentParser(description="Обучение модели классификации птиц Phoenix_Bolotni")
    parser.add_argument("--data-dir", default="./dataset", help="Папка датасета")
    parser.add_argument("--artifacts-dir", default="./artifacts", help="Папка с метаданными (classes.json, etc.)")
    parser.add_argument("--out-dir", default="./model_output", help="Папка сохранения моделей и отчетов")
    parser.add_argument("--epochs", type=int, default=12, help="Количество эпох обучения")
    parser.add_argument("--batch-size", type=int, default=32, help="Размер батча")
    parser.add_argument("--lr", type=float, default=3e-4, help="Скорость обучения (learning rate)")
    parser.add_argument("--weight-decay", type=float, default=1e-4, help="Weight decay для AdamW")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument(
        "--device",
        default="cuda" if torch.cuda.is_available() else "cpu",
        help="Устройство: cuda или cpu",
    )
    # Аугментации
    parser.add_argument("--noise-aug", action="store_true", help="Включить подмешивание шума")
    parser.add_argument("--gain-aug", action="store_true", default=True, help="Включить случайную громкость")
    parser.add_argument("--time-shift-aug", action="store_true", default=True, help="Включить time-shift")
    parser.add_argument("--pitch-aug", action="store_true", default=True, help="Включить pitch-shift")
    parser.add_argument("--mixup", action="store_true", help="Включить mixup аугментацию (экспериментально)")
    parser.add_argument("--conf-threshold", type=float, default=0.6, help="Порог уверенности для инференса")
    return parser.parse_args()


if __name__ == "__main__":
    train(parse_args())
