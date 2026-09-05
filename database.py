"""Small persistent SQLite job store. Replaceable by PostgreSQL later."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import uuid


class JobStore:
    def __init__(self, database_url: str) -> None:
        if not database_url.startswith("sqlite:///"):
            raise RuntimeError("This deployment supports SQLite; configure PostgreSQL through a future repository adapter")
        self.path = Path(database_url.removeprefix("sqlite:///")).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            source_type TEXT NOT NULL, source_url TEXT, status TEXT NOT NULL,
            created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, error TEXT
        )""")
        self.connection.commit()
        self.lock = asyncio.Lock()

    async def create(self, chat_id: int, user_id: int, source_type: str, source_url: str | None = None) -> str:
        job_id = uuid.uuid4().hex
        async with self.lock:
            self.connection.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                                    (job_id, chat_id, user_id, source_type, source_url, "RECEIVED", datetime.now(timezone.utc).isoformat()))
            self.connection.commit()
        return job_id

    async def update(self, job_id: str, status: str, error: str | None = None) -> None:
        async with self.lock:
            now = datetime.now(timezone.utc).isoformat()
            self.connection.execute("UPDATE jobs SET status=?, error=?, started_at=CASE WHEN ?='TRANSCRIBING' AND started_at IS NULL THEN ? ELSE started_at END, completed_at=CASE WHEN ? IN ('COMPLETED','FAILED') THEN ? ELSE completed_at END WHERE id=?",
                                    (status, error, status, now, status, now, job_id))
            self.connection.commit()
