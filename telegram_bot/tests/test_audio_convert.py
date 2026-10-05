"""Unit tests for audio conversion and tempfile cleanup."""
import subprocess
import tempfile
import wave
from pathlib import Path
import pytest
from collector_bot.audio_convert import (
    TempAudioProcessor,
    convert_to_pipeline_wav,
    get_audio_duration,
)


def _generate_synthetic_wav(path: Path, duration_sec: float = 2.0, sr: int = 44100, channels: int = 2):
    """Generate a clean synthetic sine/silence WAV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    num_frames = int(duration_sec * sr)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        # write 0s
        wf.writeframes(b"\x00\x00" * channels * num_frames)


def test_audio_conversion_mono_and_sample_rate():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        input_wav = tmp_path / "stereo_44k.wav"
        output_wav = tmp_path / "mono_32k.wav"

        _generate_synthetic_wav(input_wav, duration_sec=1.5, sr=44100, channels=2)

        res = convert_to_pipeline_wav(
            input_path=input_wav,
            output_path=output_wav,
            target_sr=32000,
            mono=True
        )
        assert res.exists()

        # Check properties with wave module
        with wave.open(str(output_wav), "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getframerate() == 32000
            assert wf.getsampwidth() == 2
            duration = wf.getnframes() / wf.getframerate()
            assert abs(duration - 1.5) < 0.1


def test_audio_slicing():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        input_wav = tmp_path / "full.wav"
        output_wav = tmp_path / "sliced.wav"

        _generate_synthetic_wav(input_wav, duration_sec=5.0, sr=32000, channels=1)

        convert_to_pipeline_wav(
            input_path=input_wav,
            output_path=output_wav,
            target_sr=32000,
            mono=True,
            start_sec=1.0,
            end_sec=3.5
        )
        assert output_wav.exists()

        with wave.open(str(output_wav), "rb") as wf:
            duration = wf.getnframes() / wf.getframerate()
            assert abs(duration - 2.5) < 0.15


def test_temp_audio_processor_cleanup():
    with tempfile.TemporaryDirectory() as base_dir:
        base_path = Path(base_dir)
        with TempAudioProcessor(base_dir=base_path) as tap:
            test_file = tap.create_path("dummy.wav")
            test_file.write_bytes(b"12345")
            assert test_file.exists()
            assert tap.temp_dir.exists()

        # After context exit, directory and files must be deleted
        assert not tap.temp_dir.exists()
        assert not test_file.exists()
