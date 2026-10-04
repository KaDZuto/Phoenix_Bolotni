"""
Модуль аудио-препроцессинга и аугментаций для Phoenix_Bolotni.
Единый источник функций обработки звука: загрузка, ресемплинг, обрезка, log-mel и аугментации.
Все параметры читаются из единого model_config.json.
"""

import json
import math
import random
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import numpy as np
import soundfile as sf
import torch
import torchaudio


def find_config_path(explicit_path: Optional[Union[str, Path]] = None) -> Path:
    if explicit_path:
        p = Path(explicit_path)
        if p.exists():
            return p
    candidates = [
        Path("model_config.json"),
        Path(__file__).parent / "model_config.json",
        Path("models/model_config.json"),
        Path(__file__).parent / "models" / "model_config.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return Path("model_config.json")


def load_config(config_path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    default_config = {
        "target_sr": 32000,
        "n_fft": 1024,
        "hop": 320,
        "n_mels": 128,
        "clip_seconds": 5.0,
        "noise_class_slug": "_noise",
        "confidence_threshold": 0.6,
        "window_stride_sec": 2.5,
        "window_count_for_vote": 3,
        "min_agree_windows": 2,
        "tta_time_shift_ms": [-200, 0, 200],
    }
    p = find_config_path(config_path)
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                default_config.update(loaded)
        except Exception as e:
            print(f"⚠️ Ошибка чтения конфигурации {p}: {e}. Используются значения по умолчанию.")
    return default_config


CONFIG = load_config()

TARGET_SR: int = int(CONFIG["target_sr"])
N_FFT: int = int(CONFIG["n_fft"])
HOP: int = int(CONFIG["hop"])
N_MELS: int = int(CONFIG["n_mels"])
CLIP_SECONDS: float = float(CONFIG["clip_seconds"])
TARGET_LEN: int = int(TARGET_SR * CLIP_SECONDS)
FMIN: float = 0.0
FMAX: float = TARGET_SR / 2.0

_FBANKS: Optional[torch.Tensor] = None
_WINDOW: Optional[torch.Tensor] = None


def load_audio(file_path: Union[str, Path], target_sr: int = TARGET_SR) -> Tuple[np.ndarray, int]:
    """Загрузка аудиофайла, перевод в моно и ресемплирование."""
    wav, sr = sf.read(str(file_path), always_2d=True)
    wav = wav.mean(axis=1).astype(np.float32)
    wav = resample_if_needed(wav, sr, target_sr)
    return wav, target_sr


def resample_if_needed(x: np.ndarray, sr: int, target_sr: int = TARGET_SR) -> np.ndarray:
    """Ресемплирование аудиосигнала до target_sr, если частоты не совпадают."""
    if sr == target_sr:
        return x
    xt = torch.from_numpy(x).float().unsqueeze(0)
    xt = torchaudio.functional.resample(xt, sr, target_sr)
    return xt.squeeze(0).cpu().numpy().astype(np.float32)


def crop_or_pad(x: np.ndarray, target_len: int = TARGET_LEN, train: bool = False) -> np.ndarray:
    """Обрезка клипа (случайная при train=True, по центру при train=False) или zero-padding."""
    t = x.shape[0]
    if t > target_len:
        if train:
            start = np.random.randint(0, t - target_len + 1)
        else:
            start = (t - target_len) // 2
        return x[start : start + target_len]
    if t < target_len:
        pad = target_len - t
        return np.pad(x, (0, pad), mode="constant")
    return x


def mel_fbanks(
    n_freqs: int,
    sr: int = TARGET_SR,
    n_mels: int = N_MELS,
    f_min: float = FMIN,
    f_max: Optional[float] = None,
) -> torch.Tensor:
    """Создание мел-фильтров."""
    if f_max is None:
        f_max = sr / 2.0
    return torchaudio.functional.melscale_fbanks(
        n_freqs=n_freqs,
        f_min=f_min,
        f_max=f_max,
        n_mels=n_mels,
        sample_rate=sr,
        norm=None,
        mel_scale="htk",
    )


def audio_to_logmel(
    x: np.ndarray,
    sr: int = TARGET_SR,
    n_fft: int = N_FFT,
    hop: int = HOP,
    n_mels: int = N_MELS,
) -> torch.Tensor:
    """
    Преобразование одномерного аудиосигнала в лог-мел-спектрограмму.
    Возвращает тензор формы [3, n_mels, n_frames].
    """
    global _FBANKS, _WINDOW
    if _WINDOW is None or _WINDOW.shape[0] != n_fft:
        _WINDOW = torch.hann_window(n_fft)

    xt = torch.from_numpy(x).float()
    stft = torch.stft(
        xt,
        n_fft=n_fft,
        hop_length=hop,
        win_length=n_fft,
        window=_WINDOW,
        center=True,
        return_complex=True,
    )
    spec = stft.abs() ** 2

    n_freqs = spec.shape[0]
    if _FBANKS is None or _FBANKS.shape[0] != n_freqs or _FBANKS.shape[1] != n_mels:
        _FBANKS = mel_fbanks(n_freqs=n_freqs, sr=sr, n_mels=n_mels)

    mel = spec.transpose(0, 1) @ _FBANKS
    mel = mel.transpose(0, 1)
    logmel = torch.log(mel + 1e-10)
    logmel = (logmel - logmel.mean()) / (logmel.std() + 1e-6)
    return logmel.unsqueeze(0).repeat(3, 1, 1)


# --- АУГМЕНТАЦИИ ВОЛНЫ ---

def random_gain(x: np.ndarray, min_gain_db: float = -6.0, max_gain_db: float = 6.0) -> np.ndarray:
    """Случайное изменение громкости (gain) в дБ."""
    gain_db = np.random.uniform(min_gain_db, max_gain_db)
    gain_factor = 10.0 ** (gain_db / 20.0)
    out = x * gain_factor
    return np.clip(out, -1.0, 1.0).astype(np.float32)


def time_shift(x: np.ndarray, max_shift_pct: float = 0.5) -> np.ndarray:
    """Циклический сдвиг (time shift) волновой формы."""
    pct = np.random.uniform(-max_shift_pct, max_shift_pct)
    shift_samples = int(len(x) * pct)
    return np.roll(x, shift_samples)


def pitch_shift(x: np.ndarray, sr: int = TARGET_SR, n_steps: Optional[float] = None) -> np.ndarray:
    """
    Питч-шифт волновой формы на n_steps полутонов (по умолчанию случайный от -2 до +2).
    Реализован через интерполяционный ресемплинг без накладных расходов памяти.
    """
    if n_steps is None:
        n_steps = float(np.random.uniform(-2.0, 2.0))
    if abs(n_steps) < 1e-3 or len(x) == 0:
        return x

    factor = 2.0 ** (n_steps / 12.0)
    orig_len = len(x)
    new_len = max(1, int(orig_len / factor))
    indices = np.linspace(0, orig_len - 1, new_len)
    shifted = np.interp(indices, np.arange(orig_len), x).astype(np.float32)
    return crop_or_pad(shifted, orig_len, train=False)


def add_noise(
    x: np.ndarray,
    noise_clip: Optional[np.ndarray] = None,
    min_snr_db: float = 5.0,
    max_snr_db: float = 20.0,
) -> np.ndarray:
    """
    Подмешивание фонового шума (из предоставленного клипа или синтетического шума)
    со случайным отношением сигнал/шум (SNR) в диапазоне min_snr_db..max_snr_db.
    """
    signal_power = np.mean(x ** 2)
    if signal_power < 1e-9:
        signal_power = 1e-9

    snr_db = np.random.uniform(min_snr_db, max_snr_db)
    target_noise_power = signal_power / (10.0 ** (snr_db / 10.0))

    if noise_clip is not None and len(noise_clip) > 0:
        noise = crop_or_pad(noise_clip, len(x), train=True)
    else:
        # Синтетический шум (смесь белого и низкочастотного шума)
        noise = np.random.normal(0.0, 1.0, size=len(x)).astype(np.float32)

    noise_power = np.mean(noise ** 2)
    if noise_power > 1e-9:
        noise = noise * np.sqrt(target_noise_power / noise_power)
    else:
        noise = np.random.normal(0.0, float(np.sqrt(target_noise_power)), size=len(x)).astype(np.float32)

    out = x + noise
    return np.clip(out, -1.0, 1.0).astype(np.float32)


def apply_waveform_augmentations(
    wav: np.ndarray,
    sr: int = TARGET_SR,
    gain_aug: bool = True,
    time_shift_aug: bool = True,
    pitch_aug: bool = True,
    noise_aug: bool = False,
    noise_clip: Optional[np.ndarray] = None,
) -> np.ndarray:
    """Применение набора аугментаций к волновой форме перед построением спектрограммы."""
    out = wav
    if gain_aug and np.random.rand() < 0.5:
        out = random_gain(out, -6.0, 6.0)
    if time_shift_aug and np.random.rand() < 0.5:
        out = time_shift(out, 0.4)
    if pitch_aug and np.random.rand() < 0.4:
        out = pitch_shift(out, sr, np.random.uniform(-1.5, 1.5))
    if noise_aug and np.random.rand() < 0.5:
        out = add_noise(out, noise_clip=noise_clip, min_snr_db=5.0, max_snr_db=20.0)
    return out


# --- АУГМЕНТАЦИИ СПЕКТРОГРАММЫ ---

def spec_augment(
    x: torch.Tensor,
    time_mask_pct: float = 0.15,
    freq_mask_pct: float = 0.15,
    n_time_masks: int = 2,
    n_freq_masks: int = 2,
) -> torch.Tensor:
    """Маскирование времени и частоты (SpecAugment)."""
    c, f, t = x.shape
    max_tw = max(1, int(t * time_mask_pct))
    for _ in range(n_time_masks):
        tw = random.randint(1, max_tw)
        t0 = random.randint(0, max(0, t - tw))
        x[:, :, t0 : t0 + tw] = 0.0

    max_fw = max(1, int(f * freq_mask_pct))
    for _ in range(n_freq_masks):
        fw = random.randint(1, max_fw)
        f0 = random.randint(0, max(0, f - fw))
        x[:, f0 : f0 + fw, :] = 0.0
    return x


def mixup_batch(
    x: torch.Tensor,
    y: torch.Tensor,
    alpha: float = 0.2,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, float]:
    """Mixup аугментация на уровне батча спектрограмм."""
    if alpha > 0:
        lam = float(np.random.beta(alpha, alpha))
    else:
        lam = 1.0

    batch_size = x.size(0)
    index = torch.randperm(batch_size, device=x.device)

    mixed_x = lam * x + (1.0 - lam) * x[index]
    y_a, y_b = y, y[index]
    return mixed_x, y_a, y_b, lam
