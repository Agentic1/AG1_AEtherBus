import sqlite3
from pathlib import Path
from typing import Optional


class Ledger:
    def __init__(self, path: str = "attestations.db"):
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS attestations (envelope_id TEXT PRIMARY KEY, sender_id TEXT, signature TEXT, signed_at TEXT)"
        )
        self.db.commit()

    def record(self, envelope_id: str, sender_id: str, signature: str, signed_at: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO attestations (envelope_id, sender_id, signature, signed_at) VALUES (?, ?, ?, ?)",
            (envelope_id, sender_id, signature, signed_at),
        )
        self.db.commit()

    def get_signature(self, envelope_id: str) -> Optional[str]:
        row = self.db.execute(
            "SELECT signature FROM attestations WHERE envelope_id=?", (envelope_id,)
        ).fetchone()
        return row[0] if row else None

    def verify(self, envelope_id: str, signature: str) -> bool:
        stored = self.get_signature(envelope_id)
        return stored == signature
