"""Audio conversion utilities for Phoenix_Bolotni Collector Bot using ffmpeg.

Converts input audio (voice/ogg/mp3/m4a/wav) to:
- WAV format (PCM 16-bit)
- Mono channel
- Target sample rate (default 32000 Hz from model_config.json)
- Optional time slicing [start_sec, end_sec] for precise annotations.

Guarantees immediate cleanup of temporary files.
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class AudioConversionError(Exception):
    """Raised when ffmpeg or ffprobe fails."""
    pass


def get_audio_duration(file_path: Path) -> float:
    """Probe audio duration in seconds using ffprobe."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path)
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return float(result.stdout.strip())
    except Exception as e:
        logger.warning(f"ffprobe failed for {file_path}: {e}")
        return 0.0


def convert_to_pipeline_wav(
    input_path: Path,
    output_path: Path,
    target_sr: int = 32000,
    mono: bool = True,
    start_sec: Optional[float] = None,
    end_sec: Optional[float] = None
) -> Path:
    """
    Convert any audio file to 16-bit PCM WAV, mono, target_sr.
    Optionally cuts the segment [start_sec, end_sec].
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["ffmpeg", "-y"]

    # If start_sec is provided, apply seek
    if start_sec is not None and start_sec > 0:
        cmd.extend(["-ss", f"{start_sec:.3f}"])

    cmd.extend(["-i", str(input_path)])

    # If end_sec is provided, calculate duration or cut to end
    if end_sec is not None and start_sec is not None:
        duration = max(0.01, end_sec - start_sec)
        cmd.extend(["-t", f"{duration:.3f}"])
    elif end_sec is not None:
        cmd.extend(["-to", f"{end_sec:.3f}"])

    # Audio channel: mono
    if mono:
        cmd.extend(["-ac", "1"])

    # Sample rate & codec: pcm_s16le
    cmd.extend([
        "-ar", str(target_sr),
        "-acodec", "pcm_s16le",
        str(output_path)
    ])

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            raise AudioConversionError(f"ffmpeg conversion failed: {proc.stderr}")
        return output_path
    except Exception as e:
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                pass
        raise AudioConversionError(f"Audio conversion failed: {e}")


class TempAudioProcessor:
    """
    Context manager ensuring temporary directories and files are purged.
    Usage:
        with TempAudioProcessor(base_dir=config.TEMP_DIR) as tap:
            input_wav = tap.create_temp_path("in.ogg")
            # process...
    """
    def __init__(self, base_dir: Optional[Path] = None, prefix: str = "collector_"):
        self.base_dir = Path(base_dir) if base_dir else None
        if self.base_dir:
            self.base_dir.mkdir(parents=True, exist_ok=True)
        self.prefix = prefix
        self.temp_dir: Optional[Path] = None

    def __enter__(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix=self.prefix, dir=self.base_dir))
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.temp_dir and self.temp_dir.exists():
            try:
                shutil.rmtree(self.temp_dir, ignore_errors=True)
            except Exception as e:
                logger.error(f"Failed to remove temp dir {self.temp_dir}: {e}")

    def create_path(self, filename: str) -> Path:
        if not self.temp_dir:
            raise RuntimeError("TempAudioProcessor context not entered")
        return self.temp_dir / filename
