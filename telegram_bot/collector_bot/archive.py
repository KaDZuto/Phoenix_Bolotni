"""Telegram storage layer for Phoenix_Bolotni Collector Bot.

Uses a private Telegram channel/chat as cloud storage:
- Stores processed mono 32kHz .wav files
- Retains only file_id and message_id in local SQLite
- Downloads files back when packaging batch archives
"""
import logging
from pathlib import Path
from typing import Optional, Tuple
import uuid

from aiogram import Bot
from aiogram.types import FSInputFile

logger = logging.getLogger(__name__)


async def send_to_archive(
    bot: Bot,
    channel_id: int,
    file_path: Path,
    caption: str = ""
) -> Tuple[str, int]:
    """
    Sends processed audio file to the private archive channel.
    Returns (telegram_file_id, message_id).
    In test/offline mode (channel_id == 0), returns a simulated file_id.
    """
    if channel_id == 0:
        logger.info(f"[Offline/Mock mode] Simulating archive send for {file_path}")
        simulated_id = f"mock_archive_file_{uuid.uuid4().hex[:12]}"
        return simulated_id, 1

    input_file = FSInputFile(str(file_path), filename=file_path.name)
    try:
        msg = await bot.send_document(
            chat_id=channel_id,
            document=input_file,
            caption=caption
        )
        file_id = msg.document.file_id
        return file_id, msg.message_id
    except Exception as e:
        logger.error(f"Failed to send {file_path} to archive channel {channel_id}: {e}")
        raise


async def download_from_archive(
    bot: Bot,
    file_id: str,
    destination_path: Path
) -> Path:
    """
    Downloads file from Telegram by file_id to destination_path.
    In mock mode (starts with 'mock_archive_file_'), creates a dummy audio file.
    """
    destination_path = Path(destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    if file_id.startswith("mock_archive_file_"):
        logger.info(f"[Mock mode] Writing mock wav to {destination_path}")
        destination_path.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x00}\x00\x00\x00\xfa\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00")
        return destination_path

    file_info = await bot.get_file(file_id)
    await bot.download_file(file_info.file_path, destination_path)
    return destination_path
