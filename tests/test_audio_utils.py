import numpy as np
import torch
from audio_utils import (
    TARGET_SR,
    TARGET_LEN,
    N_MELS,
    resample_if_needed,
    crop_or_pad,
    audio_to_logmel,
    random_gain,
    time_shift,
    pitch_shift,
    add_noise,
    spec_augment,
    mixup_batch,
    apply_waveform_augmentations,
)


def test_resample_same_sr():
    wav = np.random.randn(1000).astype(np.float32)
    out = resample_if_needed(wav, TARGET_SR, TARGET_SR)
    assert np.array_equal(wav, out)


def test_resample_different_sr():
    wav = np.random.randn(44100).astype(np.float32)
    out = resample_if_needed(wav, 44100, TARGET_SR)
    assert abs(len(out) - TARGET_SR) < 5


def test_crop_or_pad():
    # Longer
    wav_long = np.random.randn(TARGET_LEN + 500).astype(np.float32)
    cropped = crop_or_pad(wav_long, TARGET_LEN, train=False)
    assert len(cropped) == TARGET_LEN

    # Shorter
    wav_short = np.random.randn(TARGET_LEN - 500).astype(np.float32)
    padded = crop_or_pad(wav_short, TARGET_LEN, train=False)
    assert len(padded) == TARGET_LEN
    assert np.all(padded[TARGET_LEN - 500:] == 0.0)


def test_audio_to_logmel_shape():
    wav = np.random.randn(TARGET_LEN).astype(np.float32)
    spec = audio_to_logmel(wav, TARGET_SR)
    # Expected shape: [3, 128, 501]
    assert spec.shape == (3, 128, 501), f"Expected (3, 128, 501), got {spec.shape}"


def test_waveform_augmentations_shapes():
    wav = np.random.randn(TARGET_LEN).astype(np.float32)

    # Gain
    g = random_gain(wav)
    assert g.shape == wav.shape

    # Time shift
    ts = time_shift(wav)
    assert ts.shape == wav.shape

    # Pitch shift
    ps = pitch_shift(wav, sr=TARGET_SR, n_steps=1.5)
    assert ps.shape == wav.shape

    # Noise
    ns = add_noise(wav)
    assert ns.shape == wav.shape

    # Combined
    comb = apply_waveform_augmentations(
        wav, TARGET_SR, gain_aug=True, time_shift_aug=True, pitch_aug=True, noise_aug=True
    )
    assert comb.shape == wav.shape


def test_spec_augment():
    x = torch.randn(3, 128, 501)
    aug_x = spec_augment(x.clone(), time_mask_pct=0.15, freq_mask_pct=0.15)
    assert aug_x.shape == (3, 128, 501)
    # Some zeros should have been introduced
    assert (aug_x == 0.0).any()


def test_mixup_batch():
    x = torch.randn(4, 3, 128, 501)
    y = torch.tensor([0, 1, 2, 3])
    mixed_x, y_a, y_b, lam = mixup_batch(x, y, alpha=0.2)
    assert mixed_x.shape == x.shape
    assert y_a.shape == y.shape
    assert y_b.shape == y.shape
    assert 0.0 <= lam <= 1.0
