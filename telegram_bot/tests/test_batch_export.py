"""Unit tests for batch export packaging, manifest schema, and guaranteed cleanup."""
import json
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

from collector_bot.batch_export import export_batch, sanitize_filename
from collector_bot.db import Database


@pytest.fixture
def test_db_with_records(tmp_path):
    db_file = tmp_path / "test_export.db"
    db = Database(db_file)

    # Insert 12 pending records: 10 standard, 2 proposed
    for i in range(1, 11):
        sub = db.create_incoming_submission(
            user_id=100 + i,
            username=f"user_{i}",
            file_id=f"tg_file_{i}",
            chat_id=100 + i,
            message_id=i,
            duration_sec=3.5
        )
        db.finalize_submission(
            sub_id=sub["id"],
            species_slug="anas_platyrhynchos" if i % 2 == 0 else "ardea_cinerea",
            archive_file_id=f"mock_archive_file_{i}",
            archive_message_id=200 + i,
            lat=55.75 + (i * 0.01),
            lon=37.61 + (i * 0.01),
            comment=f"Sample note {i}"
        )

    # 2 proposed species
    for i in range(11, 13):
        sub = db.create_incoming_submission(
            user_id=100 + i,
            username=f"user_{i}",
            file_id=f"tg_file_{i}",
            chat_id=100 + i,
            message_id=i,
            duration_sec=4.0
        )
        db.finalize_submission(
            sub_id=sub["id"],
            species_slug="_proposed",
            proposed_name="Болотная сова (Asio flammeus)",
            archive_file_id=f"mock_archive_file_{i}",
            archive_message_id=200 + i
        )

    return db


def test_sanitize_filename():
    assert sanitize_filename("Болотная сова / Asio flammeus") == "Болотная_сова_Asio_flammeus"
    assert sanitize_filename("Bad:Chars?*<test>") == "Bad_Chars_test"
    assert sanitize_filename("   ") == "unnamed_species"



@pytest.mark.asyncio
async def test_batch_export_structure_manifest_and_cleanup(test_db_with_records, tmp_path):
    db = test_db_with_records
    temp_dir = tmp_path / "export_tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    assert db.count_pending() == 12

    # Mock Telegram Bot
    mock_bot = MagicMock()
    mock_bot.send_document = AsyncMock(return_value=MagicMock(message_id=999, document=MagicMock(file_id="tg_zip_doc_123")))

    # Track created zip file for post-cleanup verification
    intercepted_zip_paths = []

    original_zipfile_init = zipfile.ZipFile.__init__
    def tracking_zipfile_init(self, file, *args, **kwargs):
        intercepted_zip_paths.append(Path(file))
        return original_zipfile_init(self, file, *args, **kwargs)

    zipfile.ZipFile.__init__ = tracking_zipfile_init

    try:
        # Run export for 10 records
        result = await export_batch(
            bot=mock_bot,
            db=db,
            admin_chat_id=12345,
            limit=10,
            temp_dir=temp_dir
        )

        assert result is not None
        assert result["records_count"] == 10
        assert result["admin_message_id"] == 999
        assert result["archive_file_id"] == "tg_zip_doc_123"

        # 2 records should remain pending
        assert db.count_pending() == 2

        # Verify DB batch_exports entry
        with db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM batch_exports WHERE batch_id = ?", (result["batch_id"],))
            row = cursor.fetchone()
            assert row is not None
            assert row["records_count"] == 10

        # CRITICAL VERIFICATION: Check that temp directory has ZERO residual files or directories!
        residual_items = list(temp_dir.iterdir())
        assert len(residual_items) == 0, f"Temporary directory {temp_dir} contains lingering files: {residual_items}"

        for zip_p in intercepted_zip_paths:
            assert not zip_p.exists(), f"ZIP archive {zip_p} was not cleaned up!"

    finally:
        zipfile.ZipFile.__init__ = original_zipfile_init


@pytest.mark.asyncio
async def test_batch_export_manifest_content_and_zip_contents(test_db_with_records, tmp_path):
    db = test_db_with_records
    temp_dir = tmp_path / "export_tmp_inspect"
    temp_dir.mkdir(parents=True, exist_ok=True)

    mock_bot = MagicMock()
    # Intercept document upload to inspect ZIP content before deletion
    extracted_manifest = {}
    zip_entries = []

    async def mock_send_document(chat_id, document, caption, parse_mode=None):
        # Read the zip while it exists
        zip_path = document.path
        with zipfile.ZipFile(zip_path, "r") as zf:
            zip_entries.extend(zf.namelist())
            with zf.open("manifest.json") as mf:
                data = json.loads(mf.read().decode("utf-8"))
                extracted_manifest.update(data)
        return MagicMock(message_id=777, document=MagicMock(file_id="arch_doc_777"))

    mock_bot.send_document = AsyncMock(side_effect=mock_send_document)

    # Export all 12 records
    result = await export_batch(
        bot=mock_bot,
        db=db,
        admin_chat_id=12345,
        limit=None,
        temp_dir=temp_dir
    )

    assert result is not None
    assert result["records_count"] == 12

    # Verify manifest
    assert "batch_id" in extracted_manifest
    assert extracted_manifest["records_count"] == 12
    records = extracted_manifest["records"]
    assert len(records) == 12

    # Verify directory structure inside zip:
    # 1. Standard species in dataset/<species_slug>/<uuid>.wav
    # 2. Proposed species in dataset/_proposed_species/<name>/<uuid>.wav
    # 3. manifest.json in root
    assert "manifest.json" in zip_entries
    assert any(e.startswith("dataset/anas_platyrhynchos/") for e in zip_entries)
    assert any(e.startswith("dataset/ardea_cinerea/") for e in zip_entries)
    assert any(e.startswith("dataset/_proposed_species/Болотная_сова_Asio_flammeus/") for e in zip_entries)

    # Verify temporary folder is clean after completion
    assert len(list(temp_dir.iterdir())) == 0



@pytest.mark.asyncio
async def test_batch_export_guaranteed_cleanup_on_failure(test_db_with_records, tmp_path):
    """Ensure that if sending to Telegram fails, temporary files are STILL deleted."""
    db = test_db_with_records
    temp_dir = tmp_path / "export_tmp_failure"
    temp_dir.mkdir(parents=True, exist_ok=True)

    mock_bot = MagicMock()
    mock_bot.send_document = AsyncMock(side_effect=RuntimeError("Network drop / Telegram timeout"))

    with pytest.raises(RuntimeError):
        await export_batch(
            bot=mock_bot,
            db=db,
            admin_chat_id=12345,
            limit=5,
            temp_dir=temp_dir
        )

    # Even on failure, temp_dir MUST be clean
    residual_items = list(temp_dir.iterdir())
    assert len(residual_items) == 0, f"Lingering files after failure: {residual_items}"
