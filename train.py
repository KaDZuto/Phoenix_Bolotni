import os
import json
import random
from pathlib import Path

import numpy as np
import soundfile as sf
from tqdm import tqdm

import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader, WeightedRandomSampler
import torchaudio
from torchvision.models import efficientnet_b0, EfficientNet_B0_Weights
from sklearn.metrics import f1_score

# --- КОНФИГУРАЦИЯ ---
DATA_DIR = Path("./dataset")
ARTIFACTS_DIR = Path("./artifacts")
OUT_DIR = Path("./model_output")
OUT_DIR.mkdir(parents=True, exist_ok=True)

SEED = 42
TARGET_SR = 32000
CLIP_SECONDS = 5.0
TARGET_LEN = int(TARGET_SR * CLIP_SECONDS)

N_FFT = 1024
HOP = 320
N_MELS = 128
FMIN = 0.0
FMAX = TARGET_SR / 2

BATCH_SIZE = 32
EPOCHS = 12
LR = 3e-4
WEIGHT_DECAY = 1e-4

TIME_MASK_PCT = 0.15
FREQ_MASK_PCT = 0.15
N_TIME_MASKS = 2
N_FREQ_MASKS = 2

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
print(f"🚀 Используется устройство: {DEVICE}")

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def resample_if_needed(x: np.ndarray, sr: int, target_sr: int) -> np.ndarray:
    if sr == target_sr:
        return x
    xt = torch.from_numpy(x).float().unsqueeze(0)
    xt = torchaudio.functional.resample(xt, sr, target_sr)
    return xt.squeeze(0).cpu().numpy()

def crop_or_pad(x: np.ndarray, target_len: int, train: bool) -> np.ndarray:
    T = x.shape[0]
    if T > target_len:
        if train:
            start = np.random.randint(0, T - target_len + 1)
        else:
            start = (T - target_len) // 2
        return x[start:start+target_len]
    if T < target_len:
        pad = target_len - T
        return np.pad(x, (0, pad), mode="constant")
    return x

def mel_fbanks(n_freqs: int, sr: int) -> torch.Tensor:
    return torchaudio.functional.melscale_fbanks(
        n_freqs=n_freqs, f_min=FMIN, f_max=FMAX, n_mels=N_MELS, sample_rate=sr, norm=None, mel_scale="htk"
    )

_FBANKS = None
_WINDOW = None

def audio_to_logmel(x: np.ndarray, sr: int) -> torch.Tensor:
    global _FBANKS, _WINDOW
    if _WINDOW is None:
        _WINDOW = torch.hann_window(N_FFT)

    xt = torch.from_numpy(x).float()
    stft = torch.stft(
        xt, n_fft=N_FFT, hop_length=HOP, win_length=N_FFT,
        window=_WINDOW, center=True, return_complex=True
    )
    spec = (stft.abs() ** 2)

    n_freqs = spec.shape[0]
    if _FBANKS is None or _FBANKS.shape[0] != n_freqs:
        _FBANKS = mel_fbanks(n_freqs=n_freqs, sr=sr)

    mel = spec.transpose(0, 1) @ _FBANKS
    mel = mel.transpose(0, 1)
    logmel = torch.log(mel + 1e-10)
    logmel = (logmel - logmel.mean()) / (logmel.std() + 1e-6)
    return logmel.unsqueeze(0).repeat(3, 1, 1)

def spec_augment(x: torch.Tensor) -> torch.Tensor:
    C, F, T = x.shape
    max_tw = max(1, int(T * TIME_MASK_PCT))
    for _ in range(N_TIME_MASKS):
        tw = random.randint(1, max_tw)
        t0 = random.randint(0, max(0, T - tw))
        x[:, :, t0:t0+tw] = 0.0
    max_fw = max(1, int(F * FREQ_MASK_PCT))
    for _ in range(N_FREQ_MASKS):
        fw = random.randint(1, max_fw)
        f0 = random.randint(0, max(0, F - fw))
        x[:, f0:f0+fw, :] = 0.0
    return x

class BirdLogMelDataset(Dataset):
    def __init__(self, jsonl_path, train=True):
        self.paths = []
        self.labels = []
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                data = json.loads(line)
                self.paths.append(data["path"])
                self.labels.append(data["label"])
        self.labels = np.array(self.labels, dtype=np.int64)
        self.train = train

    def __len__(self):
        return len(self.paths)

    def __getitem__(self, i):
        p = self.paths[i]
        y = int(self.labels[i])
        wav, sr = sf.read(p, always_2d=True)
        wav = wav.mean(axis=1).astype(np.float32)
        wav = resample_if_needed(wav, sr, TARGET_SR)
        wav = crop_or_pad(wav, TARGET_LEN, train=self.train)
        X = audio_to_logmel(wav, TARGET_SR)
        if self.train:
            X = spec_augment(X)
        return X, y

# --- МОДЕЛЬ ---

def make_model(num_classes: int):
    model = efficientnet_b0(weights=EfficientNet_B0_Weights.IMAGENET1K_V1)
    in_f = model.classifier[1].in_features
    model.classifier[1] = nn.Linear(in_f, num_classes)
    return model

# --- ЦИКЛ ОБУЧЕНИЯ ---

def train():
    # Загрузка классов
    with open(ARTIFACTS_DIR / "classes.json", "r", encoding="utf-8") as f:
        CLASSES = json.load(f)
    NUM_CLASSES = len(CLASSES)

    train_ds = BirdLogMelDataset(ARTIFACTS_DIR / "train.jsonl", train=True)
    val_ds = BirdLogMelDataset(ARTIFACTS_DIR / "val.jsonl", train=False)

    # Sampler для баланса
    counts = np.bincount(train_ds.labels, minlength=NUM_CLASSES)
    class_w = 1.0 / np.maximum(counts, 1)
    sample_w = class_w[train_ds.labels]
    sampler = WeightedRandomSampler(torch.tensor(sample_w, dtype=torch.double), len(sample_w), replacement=True)

    train_dl = DataLoader(train_ds, batch_size=BATCH_SIZE, sampler=sampler, pin_memory=True)
    val_dl = DataLoader(val_ds, batch_size=BATCH_SIZE, pin_memory=True)

    model = make_model(NUM_CLASSES).to(DEVICE)
    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=WEIGHT_DECAY)
    scaler = torch.amp.GradScaler(enabled=(DEVICE=="cuda"))

    best_f1 = -1.0
    
    for epoch in range(1, EPOCHS + 1):
        model.train()
        total_loss = 0
        for x, y in tqdm(train_dl, desc=f"Epoch {epoch}/{EPOCHS}"):
            x, y = x.to(DEVICE), y.to(DEVICE)
            optimizer.zero_grad()
            with torch.amp.autocast(device_type="cuda", enabled=(DEVICE=="cuda")):
                logits = model(x)
                loss = criterion(logits, y)
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()
            total_loss += loss.item()

        # Валидация
        model.eval()
        all_pred, all_true = [], []
        with torch.no_grad():
            for x, y in val_dl:
                x, y = x.to(DEVICE), y.to(DEVICE)
                logits = model(x)
                all_pred.append(logits.argmax(1).cpu().numpy())
                all_true.append(y.cpu().numpy())
        
        all_pred = np.concatenate(all_pred)
        all_true = np.concatenate(all_true)
        f1 = f1_score(all_true, all_pred, average="macro")
        print(f"Epoch {epoch} | Loss: {total_loss/len(train_dl):.4f} | Val F1: {f1:.4f}")

        if f1 > best_f1:
            best_f1 = f1
            torch.save({
                "model": model.state_dict(),
                "classes": CLASSES,
                "config": {"TARGET_SR": TARGET_SR, "N_MELS": N_MELS}
            }, OUT_DIR / "best.pth")
            print("⭐️ Модель сохранена!")

    print(f"Обучение завершено. Лучший F1: {best_f1:.4f}")

if __name__ == "__main__":
    train()
