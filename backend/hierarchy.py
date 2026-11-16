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
            connection.execute("CREATE TABLE IF NOT EXISTS active_sources ("
                               "source TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id))")
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

    def activate(self, snapshot_id):
        snapshot = self.get(snapshot_id)
        if snapshot is None:
            raise ValueError("cannot activate an unknown snapshot")
        source = snapshot["document"]["source"]
        with self._connect() as connection:
            connection.execute("INSERT INTO active_sources VALUES (?, ?) "
                               "ON CONFLICT(source) DO UPDATE SET snapshot_id=excluded.snapshot_id",
                               (source, snapshot_id))
        return source

    def list_versions(self, source=None):
        if not self.path.exists():
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT s.id, s.document_id, s.source, a.snapshot_id FROM snapshots s "
                "LEFT JOIN active_sources a ON a.source=s.source "
                "WHERE (? IS NULL OR s.source=?) ORDER BY s.source, s.id", (source, source)).fetchall()
        return [{"id": row[0], "document_id": row[1], "source": row[2], "active": row[0] == row[3]}
                for row in rows]

    def context(self, chunk_ids, max_chars=4000):
        """Expand ranked active chunk IDs to unique parent sections under a budget."""
        if not isinstance(chunk_ids, list) or any(not isinstance(item, str) or not item.strip() for item in chunk_ids):
            raise ValueError("chunk_ids must be an array of non-empty strings")
        if type(max_chars) is not int or max_chars < 1:
            raise ValueError("max_chars must be a positive integer")
        requested = list(dict.fromkeys(chunk_ids))
        lookup = {}
        if requested:
            for version in self.list_versions():
                if not version["active"]:
                    continue
                snapshot = self.get(version["id"])
                sections = {section["id"]: section for section in snapshot["sections"]}
                for chunk in snapshot["chunks"]:
                    if chunk["id"] in requested:
                        lookup[chunk["id"]] = (version, sections[chunk["parent_id"]])
        if any(chunk_id not in lookup for chunk_id in requested):
            raise ValueError("some chunk IDs are missing or belong to inactive snapshots")
        parents = {}
        for chunk_id in requested:
            version, section = lookup[chunk_id]
            key = (version["id"], section["id"])
            if key not in parents:
                parents[key] = {"snapshot_id": version["id"], "document_id": version["document_id"],
                                "source": version["source"], "section_id": section["id"],
                                "path": section["path"], "start": section["start"],
                                "section_end": section["end"], "text": section["text"], "matched_chunk_ids": []}
            parents[key]["matched_chunk_ids"].append(chunk_id)
        contexts, remaining = [], max_chars
        for parent in parents.values():
            if remaining == 0:
                break
            content = parent["text"][:remaining]
            contexts.append({**parent, "text": content, "end": parent["start"] + len(content),
                             "truncated": len(content) < len(parent["text"])})
            remaining -= len(content)
        return {"contexts": contexts, "chars": max_chars - remaining, "max_chars": max_chars,
                "requested_chunk_ids": requested, "omitted_sections": len(parents) - len(contexts)}
