"""Validated, offline hierarchical document snapshots."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3

from backend.chunking import prepare_document
from backend.lexical import BM25Index, reciprocal_rank_fusion, tokenize


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
        if json.dumps(snapshot.get(key), sort_keys=True, ensure_ascii=False) != json.dumps(canonical[key], sort_keys=True, ensure_ascii=False):
            raise ValueError(f"snapshot {key} does not match its document and configuration")
    return canonical


def _payload(snapshot):
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _snapshot_id(payload):
    return "snapshot-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _decode_row(row):
    snapshot_id, document_id, source, payload = row
    snapshot = validate_snapshot(json.loads(payload))
    if (_snapshot_id(_payload(snapshot)) != snapshot_id or snapshot["document"]["id"] != document_id
            or snapshot["document"]["source"] != source):
        raise ValueError("stored snapshot fingerprint or identity is inconsistent")
    return snapshot


class HierarchyStore:
    """Explicit SQLite snapshot store, separate from production vector storage."""

    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def _connect(self, write=False):
        connection = (sqlite3.connect(self.path) if write else
                      sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True))
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            if write:
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
        payload = _payload(canonical)
        snapshot_id = _snapshot_id(payload)
        with self._connect(write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?, ?)",
                               (snapshot_id, canonical["document"]["id"], canonical["document"]["source"], payload))
        return snapshot_id

    def get(self, snapshot_id):
        if not self.path.exists():
            return None
        with self._connect() as connection:
            row = connection.execute("SELECT id, document_id, source, payload FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        return _decode_row(row) if row else None

    def verify(self):
        if not self.path.is_file():
            raise ValueError("store does not exist")
        with self._connect() as connection:
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("SQLite integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("store has invalid foreign key references")
            rows = connection.execute("SELECT id, document_id, source, payload FROM snapshots").fetchall()
            for row in rows:
                _decode_row(row)
            active = connection.execute("SELECT a.source, s.source FROM active_sources a "
                                        "JOIN snapshots s ON s.id=a.snapshot_id").fetchall()
            if any(source != stored_source for source, stored_source in active):
                raise ValueError("active snapshot belongs to a different source")
        return {"valid": True, "snapshots": len(rows), "active_sources": len(active)}

    def activate(self, snapshot_id):
        snapshot = self.get(snapshot_id)
        if snapshot is None:
            raise ValueError("cannot activate an unknown snapshot")
        source = snapshot["document"]["source"]
        with self._connect(write=True) as connection:
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
            for version in self.active_snapshots():
                snapshot = version["snapshot"]
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

    def search(self, query, top_k=5, section_k=5):
        """Offline lexical section selection, then body/title chunk RRF."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if any(type(value) is not int or not 1 <= value <= 200 for value in (top_k, section_k)):
            raise ValueError("top_k and section_k must be integers between 1 and 200")
        if not tokenize(query):
            return []
        sections, chunks = {}, {}
        for version in self.active_snapshots():
            snapshot = version["snapshot"]
            for section in snapshot["sections"]:
                sections[section["id"]] = {**section, "source": version["source"], "snapshot_id": version["id"]}
            for chunk in snapshot["chunks"]:
                chunks[chunk["id"]] = chunk
        selected = BM25Index([{"id": key, "text": " ".join(value["path"]) + "\n" + value["text"]}
                              for key, value in sections.items()]).search(query, section_k)
        section_scores = {item["id"]: item["score"] for item in selected}
        candidates = [chunk for chunk in chunks.values() if chunk["parent_id"] in section_scores]
        if not candidates:
            return []
        body = BM25Index(candidates).search(query, len(candidates))
        title = BM25Index([{"id": chunk["id"], "text": " ".join(sections[chunk["parent_id"]]["path"])}
                           for chunk in candidates]).search(query, len(candidates))
        fused = reciprocal_rank_fusion([[item["id"] for item in body], [item["id"] for item in title]], top_k)
        body_scores, title_scores = ({item["id"]: item["score"] for item in route} for route in (body, title))
        results = []
        for hit in fused:
            chunk = chunks[hit["id"]]
            section = sections[chunk["parent_id"]]
            results.append({**hit, "body_score": body_scores.get(hit["id"], 0),
                            "title_score": title_scores.get(hit["id"], 0),
                            "section_score": section_scores[section["id"]], "text": chunk["text"],
                            "start": chunk["start"], "end": chunk["end"], "parent_id": section["id"],
                            "document_id": chunk["document_id"], "snapshot_id": section["snapshot_id"],
                            "source": section["source"], "path": section["path"]})
        return results

    def active_snapshots(self):
        """Read all active pointers and payloads in one SQLite statement."""
        if not self.path.exists():
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT a.source, s.id, s.document_id, s.source, s.payload "
                "FROM active_sources a LEFT JOIN snapshots s ON s.id=a.snapshot_id ORDER BY a.source"
            ).fetchall()
        result = []
        for source, snapshot_id, document_id, stored_source, payload in rows:
            if snapshot_id is None or source != stored_source:
                raise ValueError("active snapshot source or reference is inconsistent")
            snapshot = _decode_row((snapshot_id, document_id, stored_source, payload))
            result.append({"id": snapshot_id, "document_id": document_id, "source": source, "snapshot": snapshot})
        return result
