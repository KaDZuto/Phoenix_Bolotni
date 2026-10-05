"""Batch exporter for Phoenix_Bolotni Collector Bot.

Packages every N pending submissions into a structured .zip archive:
  dataset/<species_slug>/<uuid>.wav
  dataset/_proposed_species/<proposed_name>/<uuid>.wav
  manifest.json
Sends the archive to admin chat, updates DB status to 'sent_for_review',
and GUARANTEES immediate deletion of temporary directories and files.
"""
import json
import logging
import re
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiogram import Bot
from aiogram.types import FSInputFile

from .archive import download_from_archive
from .db import Database

logger = logging.getLogger(__name__)


def sanitize_filename(name: str) -> str:
    """Sanitize proposed species name for filesystem folder naming."""
    clean = re.sub(r'[^\w\-]+', '_', name.strip(), flags=re.UNICODE)
    clean = re.sub(r'_+', '_', clean).strip('_')
    return clean[:60] if clean else "unnamed_species"



async def export_batch(
    bot: Bot,
    db: Database,
    admin_chat_id: int,
    limit: Optional[int] = 10,
    temp_dir: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """
    Exports pending submissions to a ZIP file and sends it to the admin.
    Returns export metadata dictionary, or None if no pending submissions.
    Guarantees cleanup of all temporary local files in a finally block.
    """
    pending = db.get_pending_for_export(limit=limit)
    if not pending:
        logger.info("No pending submissions to export.")
        return None

    batch_timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    batch_uuid = uuid.uuid4().hex[:6]
    batch_id = f"batch_{batch_timestamp}_{batch_uuid}"

    root_tmp = Path(temp_dir) if temp_dir else Path(tempfile.gettempdir())
    work_dir = Path(tempfile.mkdtemp(prefix=f"export_{batch_id}_", dir=root_tmp))
    zip_path = root_tmp / f"{batch_id}.zip"

    manifest_records: List[Dict[str, Any]] = []

    try:
        dataset_dir = work_dir / "dataset"
        dataset_dir.mkdir(parents=True, exist_ok=True)

        for sub in pending:
            sub_uuid = sub["uuid"]
            is_proposed = bool(sub.get("is_proposed"))
            species_slug = sub.get("species_slug") or "unknown"
            proposed_name = sub.get("proposed_name") or ""

            if is_proposed and proposed_name:
                folder_name = sanitize_filename(proposed_name)
                target_folder = dataset_dir / "_proposed_species" / folder_name
                rel_path = f"dataset/_proposed_species/{folder_name}/{sub_uuid}.wav"
            else:
                target_folder = dataset_dir / species_slug
                rel_path = f"dataset/{species_slug}/{sub_uuid}.wav"

            target_folder.mkdir(parents=True, exist_ok=True)
            wav_file = target_folder / f"{sub_uuid}.wav"

            # Download audio from Telegram archive
            archive_file_id = sub.get("archive_file_id") or sub.get("telegram_file_id")
            await download_from_archive(bot, archive_file_id, wav_file)

            manifest_records.append({
                "id": sub["id"],
                "uuid": sub_uuid,
                "species_slug": species_slug,
                "is_proposed": is_proposed,
                "proposed_name": proposed_name if is_proposed else None,
                "file_path": rel_path,
                "duration_sec": sub.get("duration_sec"),
                "start_sec": sub.get("start_sec"),
                "end_sec": sub.get("end_sec"),
                "annotation_type": sub.get("annotation_type", "fast"),
                "contributor_id": sub.get("telegram_user_id"),
                "contributor_username": sub.get("username"),
                "lat": sub.get("lat"),
                "lon": sub.get("lon"),
                "comment": sub.get("comment"),
                "archive_file_id": archive_file_id,
                "created_at": sub.get("created_at")
            })

        # Create manifest.json
        manifest_data = {
            "batch_id": batch_id,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "records_count": len(manifest_records),
            "records": manifest_records
        }
        manifest_file = work_dir / "manifest.json"
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, ensure_ascii=False, indent=2)

        # Build zip
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in work_dir.rglob("*"):
                if file_path.is_file():
                    arcname = file_path.relative_to(work_dir)
                    zf.write(file_path, arcname=arcname)

        admin_msg_id = None
        sent_file_id = None

        # Send to admin chat if configured
        if admin_chat_id != 0:
            doc = FSInputFile(str(zip_path), filename=f"{batch_id}.zip")
            caption = (
                f"📦 <b>Новый батч записей:</b> <code>{batch_id}</code>\n"
                f"📊 Клипов: {len(manifest_records)}\n"
                f"📁 Структура: dataset/<вид>/*.wav + manifest.json"
            )
            msg = await bot.send_document(
                chat_id=admin_chat_id,
                document=doc,
                caption=caption,
                parse_mode="HTML"
            )
            admin_msg_id = msg.message_id
            if msg.document:
                sent_file_id = msg.document.file_id
        else:
            logger.info(f"[Offline/Mock mode] Admin chat not configured (0). Batch {batch_id} generated locally.")

        # Update DB records
        sub_ids = [sub["id"] for sub in pending]
        db.mark_as_sent_for_review(sub_ids, batch_id)
        db.record_batch_export(batch_id, len(sub_ids), sent_file_id, admin_msg_id)

        return {
            "batch_id": batch_id,
            "records_count": len(sub_ids),
            "admin_message_id": admin_msg_id,
            "archive_file_id": sent_file_id
        }

    finally:
        # GUARANTEED CLEANUP: delete temporary extraction folder and temporary zip file
        if work_dir.exists():
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
                logger.info(f"Cleaned up temp export folder {work_dir}")
            except Exception as e:
                logger.error(f"Error removing temp workdir {work_dir}: {e}")
        if zip_path.exists():
            try:
                zip_path.unlink()
                logger.info(f"Cleaned up temp export zip {zip_path}")
            except Exception as e:
                logger.error(f"Error removing temp zip {zip_path}: {e}")
