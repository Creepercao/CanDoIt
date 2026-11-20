"""Validated, offline hierarchical document snapshots."""

from contextlib import contextmanager
from copy import deepcopy
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import sqlite3

from backend.chunking import prepare_document
from backend.lexical import BM25Index, reciprocal_rank_fusion, tokenize

CANDIDATE_CACHE_LIMIT = 4
RETRIEVER_SPEC = {"version": "hierarchical-lexical-v2", "tokenizer": "cjk-char-bigram-ascii-v1",
                  "bm25": {"k1": 1.2, "b": .75}, "rrf": {"rank_constant": 60}}


def _validate_search_config(top_k, section_k, route):
    if any(type(value) is not int or not 1 <= value <= 200 for value in (top_k, section_k)):
        raise ValueError("top_k and section_k must be integers between 1 and 200")
    if route not in ("rrf", "body"):
        raise ValueError("route must be rrf or body")


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

    def deactivate(self, source):
        """Remove a source's active pointer while retaining every stored snapshot."""
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source must be a non-empty string")
        if not self.path.exists():
            return False
        with self._connect(write=True) as connection:
            removed = connection.execute("DELETE FROM active_sources WHERE source=?", (source,)).rowcount
        return bool(removed)

    def prepare(self, sources=None):
        """Capture a fixed, validated corpus for a sequence of queries."""
        if sources is not None and (not isinstance(sources, list) or not sources or
                any(not isinstance(source, str) or not source.strip() for source in sources)):
            raise ValueError("sources must be a non-empty array of source keys")
        snapshots = self.active_snapshots()
        if sources is not None:
            selected = set(sources)
            if selected - {record["source"] for record in snapshots}:
                raise ValueError("some sources have no active snapshot")
            snapshots = [record for record in snapshots if record["source"] in selected]
        return HierarchyView(snapshots)

    def prepare_versions(self, snapshot_ids):
        """Explicitly read historical snapshots without changing active sources."""
        if not isinstance(snapshot_ids, list) or not snapshot_ids or any(
                not isinstance(identifier, str) or not identifier.strip() for identifier in snapshot_ids):
            raise ValueError("snapshot_ids must be a non-empty array of IDs")
        identifiers = sorted(set(snapshot_ids))
        if not self.path.is_file():
            raise ValueError("some requested snapshots do not exist")
        placeholders = ",".join("?" for _ in identifiers)
        with self._connect() as connection:
            rows = connection.execute("SELECT id, document_id, source, payload FROM snapshots WHERE id IN ("
                                      + placeholders + ") ORDER BY source, id", identifiers).fetchall()
        if len(rows) != len(identifiers):
            raise ValueError("some requested snapshots do not exist")
        if len({row[2] for row in rows}) != len(rows):
            raise ValueError("select only one snapshot per source")
        records = [{"id": row[0], "document_id": row[1], "source": row[2], "snapshot": _decode_row(row)} for row in rows]
        return HierarchyView(records)

    def context(self, chunk_ids, max_chars=4000, *, sources=None, mode="prefix"):
        return self.prepare(sources).context(chunk_ids, max_chars, mode=mode)

    def search(self, query, top_k=5, section_k=5, *, sources=None, route="rrf"):
        return self.prepare(sources).search(query, top_k, section_k, route=route)

    def search_report(self, query, top_k=5, section_k=5, *, sources=None, route="rrf"):
        return self.prepare(sources).search_report(query, top_k, section_k, route=route)

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


class HierarchyView:
    """Detached corpus captured at preparation time; later activation is invisible."""

    def __init__(self, snapshots):
        self._records = json.dumps(snapshots, ensure_ascii=False)
        self._snapshot_ids = tuple(sorted(record["id"] for record in snapshots))
        self._sections, self._chunks, self._chunk_lookup = {}, {}, {}
        for version in json.loads(self._records):
            sections = {section["id"]: section for section in version["snapshot"]["sections"]}
            for identifier, section in sections.items():
                self._sections[identifier] = {**section, "source": version["source"], "snapshot_id": version["id"]}
            for chunk in version["snapshot"]["chunks"]:
                self._chunks[chunk["id"]] = chunk
                self._chunk_lookup[chunk["id"]] = (version, sections[chunk["parent_id"]], chunk)
        self._section_index = BM25Index([
            {"id": key, "text": " ".join(value["path"]) + "\n" + value["text"]}
            for key, value in self._sections.items()])
        self._candidate_indexes = OrderedDict()

    def _indexes_for(self, section_ids):
        key = tuple(sorted(section_ids))
        if key in self._candidate_indexes:
            self._candidate_indexes.move_to_end(key)
            return self._candidate_indexes[key]
        candidates = [chunk for chunk in self._chunks.values() if chunk["parent_id"] in section_ids]
        body = BM25Index(candidates)
        title = BM25Index([{"id": chunk["id"], "text": " ".join(self._sections[chunk["parent_id"]]["path"])}
                           for chunk in candidates])
        self._candidate_indexes[key] = (candidates, body, title)
        if len(self._candidate_indexes) > CANDIDATE_CACHE_LIMIT:
            self._candidate_indexes.popitem(last=False)
        return candidates, body, title

    def active_snapshots(self):
        # Return detached records so caller mutations cannot change this view.
        return json.loads(self._records)

    @property
    def snapshot_ids(self):
        return self._snapshot_ids

    def retrieval_identity(self, top_k=5, section_k=5, *, route="rrf"):
        """Describe corpus/configuration, excluding query labels and runtime cache state."""
        _validate_search_config(top_k, section_k, route)
        corpus = hashlib.sha256(_payload(list(self.snapshot_ids)).encode("utf-8")).hexdigest()
        identity = {"snapshot_ids": list(self.snapshot_ids), "corpus_sha256": corpus,
                    "configuration": {**deepcopy(RETRIEVER_SPEC), "route": route,
                                      "top_k": top_k, "section_k": section_k}}
        identity["retrieval_sha256"] = hashlib.sha256(_payload(identity).encode("utf-8")).hexdigest()
        return identity

    def inventory(self, preview_chars=160, *, include_text=False):
        """Expose exact labeling IDs and offsets without inventing relevance labels."""
        if type(preview_chars) is not int or preview_chars < 1:
            raise ValueError("preview_chars must be a positive integer")
        if type(include_text) is not bool:
            raise ValueError("include_text must be boolean")

        def content(text):
            return {"content_preview": text[:preview_chars], "preview_truncated": len(text) > preview_chars,
                    **({"text": text} if include_text else {})}

        documents = [{"snapshot_id": version["id"], "id": version["document_id"], "source": version["source"],
                      "chars": version["snapshot"]["stats"]["source_chars"]} for version in self.active_snapshots()]
        sections = [{**{key: section[key] for key in ("id", "parent_id", "source", "snapshot_id", "path", "start", "end")},
                     **content(section["text"])} for section in sorted(self._sections.values(),
                            key=lambda item: (item["source"], item["start"], item["id"]))]
        chunks = []
        for version, section, chunk in sorted(self._chunk_lookup.values(),
                key=lambda item: (item[0]["source"], item[2]["start"], item[2]["id"])):
            chunks.append({**{key: chunk[key] for key in ("id", "parent_id", "document_id", "start", "end")},
                           "snapshot_id": version["id"], "source": version["source"], "path": section["path"],
                           **content(chunk["text"])})
        return deepcopy({"kind": "hierarchy-inventory-v1", "snapshot_ids": list(self.snapshot_ids),
                         "corpus_sha256": self.retrieval_identity()["corpus_sha256"], "preview_chars": preview_chars,
                         "include_text": include_text, "documents": documents, "sections": sections, "chunks": chunks})

    def require_chunk_ids(self, identifiers):
        if not isinstance(identifiers, list) or any(
                not isinstance(identifier, str) or not identifier.strip() for identifier in identifiers):
            raise ValueError("chunk IDs must be an array of non-empty strings")
        missing = sorted(set(identifiers) - self._chunks.keys())
        if missing:
            raise ValueError("relevant IDs are absent from the selected corpus: " + ", ".join(missing[:5]))

    def context(self, chunk_ids, max_chars=4000, *, mode="prefix"):
        """Expand ranked active chunk IDs to unique parent sections under a budget."""
        if not isinstance(chunk_ids, list) or any(not isinstance(item, str) or not item.strip() for item in chunk_ids):
            raise ValueError("chunk_ids must be an array of non-empty strings")
        if type(max_chars) is not int or max_chars < 1:
            raise ValueError("max_chars must be a positive integer")
        if mode not in ("prefix", "hit"):
            raise ValueError("context mode must be prefix or hit")
        requested = list(dict.fromkeys(chunk_ids))
        lookup = {identifier: self._chunk_lookup[identifier] for identifier in requested
                  if identifier in self._chunk_lookup}
        if any(chunk_id not in lookup for chunk_id in requested):
            raise ValueError("some chunk IDs are missing or belong to inactive snapshots")
        parents = {}
        for chunk_id in requested:
            version, section, chunk = lookup[chunk_id]
            key = (version["id"], section["id"])
            if key not in parents:
                parents[key] = {"snapshot_id": version["id"], "document_id": version["document_id"],
                                "source": version["source"], "section_id": section["id"],
                                "path": section["path"], "start": section["start"],
                                "section_start": section["start"], "section_end": section["end"],
                                "text": section["text"], "matched_chunk_ids": [], "matched_ranges": []}
            parents[key]["matched_chunk_ids"].append(chunk_id)
            parents[key]["matched_ranges"].append({"id": chunk_id, "start": chunk["start"], "end": chunk["end"]})
        contexts, remaining = [], max_chars
        for parent in parents.values():
            if remaining == 0:
                break
            length = min(len(parent["text"]), remaining)
            offset = 0
            if mode == "hit":
                anchor = parent["matched_ranges"][0]
                before = max(0, (length - (anchor["end"] - anchor["start"])) // 2)
                offset = max(0, anchor["start"] - parent["start"] - before)
                offset = min(offset, len(parent["text"]) - length)
            content = parent["text"][offset:offset + length]
            start, end = parent["start"] + offset, parent["start"] + offset + len(content)
            visible = [hit["id"] for hit in parent["matched_ranges"] if hit["start"] < end and hit["end"] > start]
            complete = [hit["id"] for hit in parent["matched_ranges"] if start <= hit["start"] and hit["end"] <= end]
            contexts.append({**parent, "text": content, "start": start, "end": end,
                             "visible_chunk_ids": visible, "complete_chunk_ids": complete,
                             "truncated": len(content) < len(parent["text"])})
            remaining -= len(content)
        return {"contexts": deepcopy(contexts), "chars": max_chars - remaining, "max_chars": max_chars,
                "requested_chunk_ids": requested, "omitted_sections": len(parents) - len(contexts), "mode": mode}

    def search(self, query, top_k=5, section_k=5, *, route="rrf"):
        return self.search_report(query, top_k, section_k, route=route)["hits"]

    def search_report(self, query, top_k=5, section_k=5, *, route="rrf"):
        """Offline lexical section selection, then body/title chunk RRF."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        _validate_search_config(top_k, section_k, route)
        diagnostics = {"sections": len(self._sections), "chunks": len(self._chunks),
                       "query_terms": sorted(set(tokenize(query))), "selected_sections": [],
                       "selected_chunks": 0, "body_candidates": 0, "title_candidates": 0,
                       "fused_candidates": 0, "candidate_cache_hit": False}

        def report(hits):
            return {"query": query, "hits": deepcopy(hits), **self.retrieval_identity(top_k, section_k, route=route),
                    "diagnostics": {**deepcopy(diagnostics), "returned_hits": len(hits)}}

        if not diagnostics["query_terms"]:
            return report([])
        sections, chunks = self._sections, self._chunks
        selected = self._section_index.search(query, section_k)
        diagnostics["selected_sections"] = selected
        section_scores = {item["id"]: item["score"] for item in selected}
        if not section_scores:
            return report([])
        diagnostics["candidate_cache_hit"] = tuple(sorted(section_scores)) in self._candidate_indexes
        candidates, body_index, title_index = self._indexes_for(section_scores)
        if not candidates:
            return report([])
        diagnostics["selected_chunks"] = len(candidates)
        body = body_index.search(query, len(candidates))
        title = title_index.search(query, len(candidates)) if route == "rrf" else []
        fused = (reciprocal_rank_fusion([[item["id"] for item in body], [item["id"] for item in title]], top_k)
                 if route == "rrf" else [{**item, "ranks": [index, None]}
                                         for index, item in enumerate(body[:top_k], 1)])
        diagnostics["body_candidates"] = len(body)
        diagnostics["title_candidates"] = len(title)
        diagnostics["fused_candidates"] = len({item["id"] for item in body + title})
        body_scores, title_scores = ({item["id"]: item["score"] for item in route} for route in (body, title))
        results = []
        for hit in fused:
            chunk = chunks[hit["id"]]
            section = sections[chunk["parent_id"]]
            results.append({**hit, "route": route, "score_kind": "rrf" if route == "rrf" else "bm25",
                            "body_score": body_scores.get(hit["id"], 0),
                            "title_score": title_scores.get(hit["id"], 0),
                            "section_score": section_scores[section["id"]], "text": chunk["text"],
                            "start": chunk["start"], "end": chunk["end"], "parent_id": section["id"],
                            "document_id": chunk["document_id"], "snapshot_id": section["snapshot_id"],
                            "source": section["source"], "path": section["path"]})
        return report(results)

