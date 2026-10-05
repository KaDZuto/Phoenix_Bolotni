"""Unit tests for Database operations."""
import tempfile
from pathlib import Path
import pytest
from collector_bot.db import Database


@pytest.fixture
def test_db():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test_collector.db"
        db = Database(db_path)
        yield db


def test_consent_lifecycle(test_db):
    user_id = 12345
    assert not test_db.is_user_consented(user_id)

    test_db.set_user_consent(user_id, username="ornithologist", first_name="Anna")
    assert test_db.is_user_consented(user_id)


def test_submission_lifecycle(test_db):
    user_id = 999
    test_db.set_user_consent(user_id, username="alex")

    sub = test_db.create_incoming_submission(
        user_id=user_id,
        username="alex",
        file_id="voice_file_123",
        chat_id=999,
        message_id=42,
        duration_sec=4.5
    )
    assert sub["id"] is not None
    assert sub["uuid"] is not None
    assert test_db.count_pending() == 0  # Still 'incoming'

    # Finalize submission as pending
    success = test_db.finalize_submission(
        sub_id=sub["id"],
        species_slug="anas_platyrhynchos",
        archive_file_id="arch_123",
        archive_message_id=101,
        duration_sec=4.5,
        lat=56.12,
        lon=38.45,
        comment="near river",
        annotation_type="fast"
    )
    assert success
    assert test_db.count_pending() == 1

    retrieved = test_db.get_submission_by_id(sub["id"])
    assert retrieved["species_slug"] == "anas_platyrhynchos"
    assert retrieved["status"] == "pending"
    assert retrieved["lat"] == 56.12


def test_proposed_species(test_db):
    user_id = 888
    sub = test_db.create_incoming_submission(
        user_id=user_id,
        username="biologist",
        file_id="voice_888",
        chat_id=888,
        message_id=1,
        duration_sec=3.0
    )
    test_db.finalize_submission(
        sub_id=sub["id"],
        species_slug="_proposed",
        archive_file_id="arch_888",
        archive_message_id=202,
        proposed_name="Болотная сова (Asio flammeus)",
        duration_sec=3.0
    )
    assert test_db.count_pending() == 1

    proposed = test_db.get_proposed_species()
    assert len(proposed) == 1
    assert proposed[0][0] == "Болотная сова (Asio flammeus)"
    assert proposed[0][1] == 1


def test_undo_last_submission(test_db):
    user_id = 777
    sub = test_db.create_incoming_submission(
        user_id=user_id,
        username="user777",
        file_id="voice_777",
        chat_id=777,
        message_id=10
    )
    test_db.finalize_submission(
        sub_id=sub["id"],
        species_slug="ardea_cinerea",
        archive_file_id="arch_777",
        archive_message_id=10
    )
    assert test_db.count_pending() == 1

    undone = test_db.undo_last_pending_submission(user_id)
    assert undone is not None
    assert undone["id"] == sub["id"]
    assert test_db.count_pending() == 0

    # Cannot undo again
    undone2 = test_db.undo_last_pending_submission(user_id)
    assert undone2 is None


def test_contributor_trust_metrics(test_db):
    user_id = 555
    test_db.set_user_consent(user_id, username="volunteer_kate")

    # Create and finalize 3 submissions
    subs = []
    for i in range(3):
        s = test_db.create_incoming_submission(user_id, "volunteer_kate", f"file_{i}", 555, i)
        test_db.finalize_submission(s["id"], "anas_platyrhynchos", f"arch_{i}", i)
        subs.append(s["id"])

    # Initial trust
    stats = test_db.get_user_trust_stats(user_id)
    assert stats["total"] == 3
    assert stats["accepted"] == 0
    assert stats["rejected"] == 0

    # Review: approve 2, reject 1
    test_db.review_submission(subs[0], is_approved=True)
    test_db.review_submission(subs[1], is_approved=True)
    test_db.review_submission(subs[2], is_approved=False)

    updated = test_db.get_user_trust_stats(user_id)
    assert updated["total"] == 3
    assert updated["accepted"] == 2
    assert updated["rejected"] == 1
    assert updated["trust_rate"] == 0.67  # 2 / 3 = 0.67

    # Top contributors
    top = test_db.get_top_contributors(limit=5)
    assert len(top) == 1
    assert top[0]["name"] == "volunteer_kate"
    assert top[0]["accepted"] == 2

