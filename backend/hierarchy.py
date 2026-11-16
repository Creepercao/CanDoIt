"""Validated, offline hierarchical document snapshots."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3

from backend.chunking import prepare_document


def validate_snapshot(snapshot):
    """Rebuild canonical records to verify IDs, offsets, coverage and parents."""
    if not isinstance(snapshot, dict) or type(snapshot.get("version")) is not int or snapshot["version"] != 1:
        raise ValueError("unsupported hierarchy snapshot version")
    document, config = snapshot.get("document"), snapshot.get("configuration")
    if not isinstance(document, dict) or not isinstance(config, dict):
        raise ValueError("snapshot needs document and configuration objects")
    if set(config) != {"max_chars", "overlap_chars"}:
        raise ValueError("invalid chunk configuration")
    canonical = prepare_document(document.get("text"), document.get("source"), **config)
    for key in canonical:
        if snapshot.get(key) != canonical[key]:
            raise ValueError(f"snapshot {key} does not match its document and configuration")
    return canonical


class HierarchyStore:
    """Explicit SQLite snapshot store, separate from production vector storage."""

    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("CREATE TABLE IF NOT EXISTS snapshots ("
                               "id TEXT PRIMARY KEY, document_id TEXT NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL)")
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, snapshot):
        canonical = validate_snapshot(snapshot)  # No file creation for invalid input.
        payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        snapshot_id = "snapshot-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?, ?)",
                               (snapshot_id, canonical["document"]["id"], canonical["document"]["source"], payload))
        return snapshot_id

    def get(self, snapshot_id):
        if not self.path.exists():
            return None
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        return validate_snapshot(json.loads(row[0])) if row else None
