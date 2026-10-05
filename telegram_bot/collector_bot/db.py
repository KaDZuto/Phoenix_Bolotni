"""SQLite database layer for Phoenix_Bolotni Collector Bot."""
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    consent INTEGER DEFAULT 0,
                    consented_at TEXT,
                    total_submissions INTEGER DEFAULT 0,
                    accepted_submissions INTEGER DEFAULT 0,
                    rejected_submissions INTEGER DEFAULT 0,
                    created_at TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uuid TEXT UNIQUE NOT NULL,
                    telegram_user_id INTEGER NOT NULL,
                    username TEXT,
                    telegram_file_id TEXT NOT NULL,
                    telegram_message_id INTEGER,
                    telegram_chat_id INTEGER,
                    archive_file_id TEXT,
                    archive_message_id INTEGER,
                    species_slug TEXT,
                    proposed_name TEXT,
                    is_proposed INTEGER DEFAULT 0,
                    start_sec REAL,
                    end_sec REAL,
                    duration_sec REAL,
                    lat REAL,
                    lon REAL,
                    comment TEXT,
                    annotation_type TEXT DEFAULT 'fast',
                    status TEXT DEFAULT 'incoming',
                    batch_id TEXT,
                    created_at TEXT NOT NULL,
                    exported_at TEXT,
                    FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_id)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS batch_exports (
                    batch_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    records_count INTEGER NOT NULL,
                    archive_file_id TEXT,
                    admin_message_id INTEGER,
                    status TEXT DEFAULT 'sent'
                )
            """)

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sub_status ON submissions(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sub_user ON submissions(telegram_user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sub_species ON submissions(species_slug)")
            conn.commit()

    # User & Consent
    def set_user_consent(self, user_id: int, username: Optional[str] = None, first_name: Optional[str] = None) -> None:
        now = _utc_now_iso()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO users (telegram_id, username, first_name, consent, consented_at, created_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = COALESCE(excluded.username, users.username),
                    first_name = COALESCE(excluded.first_name, users.first_name),
                    consent = 1,
                    consented_at = excluded.consented_at
            """, (user_id, username, first_name, now, now))
            conn.commit()

    def is_user_consented(self, user_id: int, validity_days: int = 365) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT consent, consented_at FROM users WHERE telegram_id = ?", (user_id,))
            row = cursor.fetchone()
            if not row or not row["consent"] or not row["consented_at"]:
                return False
            try:
                consented_at = datetime.fromisoformat(row["consented_at"])
                if consented_at.tzinfo is None:
                    consented_at = consented_at.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                if (now - consented_at).days > validity_days:
                    return False
                return True
            except Exception:
                return bool(row["consent"])

    # Submissions
    def create_incoming_submission(
        self,
        user_id: int,
        username: Optional[str],
        file_id: str,
        chat_id: int,
        message_id: int,
        duration_sec: Optional[float] = None
    ) -> Dict[str, Any]:
        sub_uuid = str(uuid.uuid4())
        now = _utc_now_iso()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO submissions (
                    uuid, telegram_user_id, username, telegram_file_id,
                    telegram_chat_id, telegram_message_id, duration_sec,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'incoming', ?)
            """, (sub_uuid, user_id, username, file_id, chat_id, message_id, duration_sec, now))
            sub_id = cursor.lastrowid
            conn.commit()
            return {"id": sub_id, "uuid": sub_uuid, "telegram_file_id": file_id, "duration_sec": duration_sec}

    def get_submission_by_id(self, sub_id: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM submissions WHERE id = ?", (sub_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_submission_by_uuid(self, sub_uuid: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM submissions WHERE uuid = ?", (sub_uuid,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_last_active_submission(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get the latest submission from user that is either incoming or pending."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM submissions 
                WHERE telegram_user_id = ? AND status IN ('incoming', 'pending')
                ORDER BY id DESC LIMIT 1
            """, (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def finalize_submission(
        self,
        sub_id: int,
        species_slug: str,
        archive_file_id: str,
        archive_message_id: int,
        proposed_name: Optional[str] = None,
        duration_sec: Optional[float] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        comment: Optional[str] = None,
        annotation_type: str = "fast",
        start_sec: Optional[float] = None,
        end_sec: Optional[float] = None
    ) -> bool:
        is_proposed = 1 if (species_slug == "_proposed" or proposed_name) else 0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE submissions SET
                    species_slug = ?,
                    proposed_name = ?,
                    is_proposed = ?,
                    archive_file_id = ?,
                    archive_message_id = ?,
                    duration_sec = COALESCE(?, duration_sec),
                    lat = COALESCE(?, lat),
                    lon = COALESCE(?, lon),
                    comment = COALESCE(?, comment),
                    annotation_type = ?,
                    start_sec = ?,
                    end_sec = ?,
                    status = 'pending'
                WHERE id = ?
            """, (
                species_slug, proposed_name, is_proposed, archive_file_id,
                archive_message_id, duration_sec, lat, lon, comment,
                annotation_type, start_sec, end_sec, sub_id
            ))
            # Also update user submission count
            cursor.execute("""
                UPDATE users SET total_submissions = total_submissions + 1
                WHERE telegram_id = (SELECT telegram_user_id FROM submissions WHERE id = ?)
            """, (sub_id,))
            conn.commit()
            return cursor.rowcount > 0

    def undo_last_pending_submission(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Undo last submission by user if it is still in pending status (before batch export)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM submissions
                WHERE telegram_user_id = ? AND status = 'pending'
                ORDER BY id DESC LIMIT 1
            """, (user_id,))
            row = cursor.fetchone()
            if not row:
                return None
            sub = dict(row)
            cursor.execute("UPDATE submissions SET status = 'undone' WHERE id = ?", (sub["id"],))
            cursor.execute("UPDATE users SET total_submissions = MAX(0, total_submissions - 1) WHERE telegram_id = ?", (user_id,))
            conn.commit()
            return sub

    def count_pending(self) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM submissions WHERE status = 'pending'")
            return cursor.fetchone()[0]

    def get_pending_for_export(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if limit:
                cursor.execute("""
                    SELECT * FROM submissions WHERE status = 'pending'
                    ORDER BY id ASC LIMIT ?
                """, (limit,))
            else:
                cursor.execute("""
                    SELECT * FROM submissions WHERE status = 'pending'
                    ORDER BY id ASC
                """)
            return [dict(r) for r in cursor.fetchall()]

    def mark_as_sent_for_review(self, submission_ids: List[int], batch_id: str) -> None:
        now = _utc_now_iso()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in submission_ids)
            cursor.execute(f"""
                UPDATE submissions SET
                    status = 'sent_for_review',
                    batch_id = ?,
                    exported_at = ?
                WHERE id IN ({placeholders})
            """, [batch_id, now] + submission_ids)
            conn.commit()

    def record_batch_export(
        self,
        batch_id: str,
        records_count: int,
        archive_file_id: Optional[str] = None,
        admin_message_id: Optional[int] = None
    ) -> None:
        now = _utc_now_iso()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO batch_exports (batch_id, created_at, records_count, archive_file_id, admin_message_id, status)
                VALUES (?, ?, ?, ?, ?, 'sent')
            """, (batch_id, now, records_count, archive_file_id, admin_message_id))
            conn.commit()

    # Stats & Metrics
    def get_species_counts(self) -> List[Tuple[str, int]]:
        """Returns species and counts for pending + sent_for_review + accepted, sorted ASC by count."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COALESCE(species_slug, 'unknown') as sp, COUNT(*) as cnt
                FROM submissions
                WHERE status IN ('pending', 'sent_for_review', 'accepted')
                GROUP BY sp
                ORDER BY cnt ASC, sp ASC
            """)
            return [(r["sp"], r["cnt"]) for r in cursor.fetchall()]

    def get_proposed_species(self) -> List[Tuple[str, int]]:
        """Returns proposed species names and their submission counts."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT proposed_name, COUNT(*) as cnt
                FROM submissions
                WHERE is_proposed = 1 AND status IN ('pending', 'sent_for_review', 'accepted')
                GROUP BY proposed_name
                ORDER BY cnt DESC, proposed_name ASC
            """)
            return [(r["proposed_name"], r["cnt"]) for r in cursor.fetchall() if r["proposed_name"]]

    def review_submission(self, sub_id: int, is_approved: bool) -> bool:
        new_status = 'accepted' if is_approved else 'rejected'
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT telegram_user_id FROM submissions WHERE id = ?", (sub_id,))
            row = cursor.fetchone()
            if not row:
                return False
            user_id = row["telegram_user_id"]
            cursor.execute("UPDATE submissions SET status = ? WHERE id = ?", (new_status, sub_id))
            if is_approved:
                cursor.execute("UPDATE users SET accepted_submissions = accepted_submissions + 1 WHERE telegram_id = ?", (user_id,))
            else:
                cursor.execute("UPDATE users SET rejected_submissions = rejected_submissions + 1 WHERE telegram_id = ?", (user_id,))
            conn.commit()
            return True

    def get_user_trust_stats(self, user_id: int) -> Dict[str, Any]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT total_submissions, accepted_submissions, rejected_submissions
                FROM users WHERE telegram_id = ?
            """, (user_id,))
            row = cursor.fetchone()
            if not row:
                return {"total": 0, "accepted": 0, "rejected": 0, "trust_rate": 0.0}
            total = row["total_submissions"]
            acc = row["accepted_submissions"]
            rej = row["rejected_submissions"]
            reviewed = acc + rej
            rate = (acc / reviewed) if reviewed > 0 else 1.0
            return {
                "total": total,
                "accepted": acc,
                "rejected": rej,
                "trust_rate": round(rate, 2)
            }

    def get_top_contributors(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT telegram_id, username, first_name, total_submissions, accepted_submissions, rejected_submissions
                FROM users
                WHERE total_submissions > 0
                ORDER BY accepted_submissions DESC, total_submissions DESC
                LIMIT ?
            """, (limit,))
            results = []
            for r in cursor.fetchall():
                acc = r["accepted_submissions"]
                rej = r["rejected_submissions"]
                total_rev = acc + rej
                rate = (acc / total_rev) if total_rev > 0 else 1.0
                results.append({
                    "telegram_id": r["telegram_id"],
                    "name": r["username"] or r["first_name"] or str(r["telegram_id"]),
                    "total": r["total_submissions"],
                    "accepted": acc,
                    "rejected": rej,
                    "trust_rate": round(rate, 2)
                })
            return results

