"""Hash-chained, append-only audit trail for agent executions.

Every safety-relevant decision (plan review, action gate, approval, output
review, goal verification, remediation) is appended as one JSON line to
``outputs/audit/audit-YYYY-MM-DD.jsonl``.  Each record carries ``prev_hash``
and ``hash``, so removing or editing a line breaks ``verify_chain()``.

A bounded in-memory ring buffer mirrors recent records so the REST API can
answer without re-reading files, and so auditing still works when the outputs
directory is not writable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from backend.safety.redact import redact

logger = logging.getLogger("safety.audit")

GENESIS_HASH = "0" * 32
_RING_SIZE = 2000
_DETAIL_LIMIT = 4000
_TAIL_BYTES = 16 * 1024


@contextmanager
def _cross_process_lock(handle):
    """Best-effort exclusive lock so two processes cannot fork the chain.

    A non-blocking lock is retried briefly and then given up on: auditing must
    never hang a run, and the caller additionally re-reads the chain head from
    disk inside the lock, which already covers the realistic case of several
    short-lived processes appending in turn.
    """
    if handle is None:
        yield
        return

    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            for _ in range(20):
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    import time

                    time.sleep(0.005)
        else:
            import fcntl

            for _ in range(20):
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    import time

                    time.sleep(0.005)
    except Exception:  # platform without either module
        locked = False

    try:
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _digest(prev_hash: str, body: dict) -> str:
    return hashlib.sha256((prev_hash + _canonical(body)).encode("utf-8")).hexdigest()[:32]


def _truncate(value: Any) -> Any:
    """Keep audit details small enough to stay greppable."""
    if isinstance(value, str):
        return value if len(value) <= _DETAIL_LIMIT else value[:_DETAIL_LIMIT] + "…[truncated]"
    if isinstance(value, dict):
        return {k: _truncate(v) for k, v in list(value.items())[:60]}
    if isinstance(value, list):
        return [_truncate(v) for v in value[:60]]
    return value


class AuditLog:
    """Append-only audit trail with a tamper-evident hash chain."""

    def __init__(self, directory: Optional[Path] = None, ring_size: int = _RING_SIZE):
        if directory is None:
            from backend.api import OUTPUTS_DIR

            directory = OUTPUTS_DIR / "audit"
        self.directory = Path(directory)
        self._ring: deque[dict] = deque(maxlen=ring_size)
        self._lock = threading.Lock()
        self._last_hash: dict[str, str] = {}

    # ── paths ──────────────────────────────────────────────────────────

    @staticmethod
    def day_of(ts: Optional[datetime] = None) -> str:
        return (ts or _now()).strftime("%Y-%m-%d")

    def path_for(self, day: str) -> Path:
        return self.directory / f"audit-{day}.jsonl"

    def _ensure_dir(self) -> bool:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            return True
        except OSError as exc:  # read-only volume → stay in-memory
            logger.debug("Audit directory unavailable (%s); using memory only", exc)
            return False

    # ── write ──────────────────────────────────────────────────────────

    def record(
        self,
        event: str,
        *,
        run_id: str = "",
        session_id: str = "",
        stage: str = "",
        actor: str = "system",
        target: str = "",
        decision: str = "",
        risk: str = "",
        severity: str = "info",
        message: str = "",
        details: Optional[dict] = None,
        tags: Optional[list[str]] = None,
    ) -> dict:
        """Append one audit record and return it."""
        ts = _now()
        day = self.day_of(ts)
        body = {
            "ts": ts.isoformat(),
            "event": event,
            "run_id": run_id,
            "session_id": session_id,
            "stage": stage,
            "actor": actor,
            "target": target,
            "decision": decision,
            "risk": risk,
            "severity": severity,
            "message": _truncate(str(message)),
            "details": _truncate(redact(details or {})),
            "tags": list(tags or []),
        }

        handle = None
        if self._ensure_dir():
            try:
                # "a+" keeps O_APPEND semantics while still allowing the tail
                # read used to recover the chain head.
                handle = open(self.path_for(day), "a+", encoding="utf-8")
            except OSError as exc:
                logger.debug("Audit append unavailable: %s", exc)

        try:
            with self._lock, _cross_process_lock(handle):
                # Always recover the head from disk: another process may have
                # appended since this one last wrote.
                prev_hash = (
                    self._tail_hash_from(handle) if handle is not None
                    else self._last_hash.get(day, GENESIS_HASH)
                )
                record = {
                    "id": uuid.uuid4().hex[:16],
                    **body,
                    "prev_hash": prev_hash,
                }
                record["hash"] = _digest(prev_hash, body)
                self._last_hash[day] = record["hash"]

                if handle is not None:
                    try:
                        handle.seek(0, os.SEEK_END)
                        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    except OSError as exc:
                        logger.debug("Audit append failed: %s", exc)

                self._ring.append(record)
        finally:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass

        return record

    @staticmethod
    def _tail_hash_from(handle) -> str:
        """Read the chain head from the end of an open audit file.

        Only the last chunk is read, so appending stays O(1) instead of
        re-parsing the whole day.  A torn final line (crash mid-write) is
        skipped by walking backwards to the last parseable record.
        """
        try:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size == 0:
                return GENESIS_HASH
            handle.seek(max(0, size - _TAIL_BYTES))
            data = handle.read()
        except (OSError, ValueError):
            return GENESIS_HASH

        for line in reversed(data.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue                     # partial line at the chunk boundary
            if isinstance(parsed, dict) and parsed.get("hash"):
                return parsed["hash"]
        return GENESIS_HASH

    def _read_tail_hash(self, day: str) -> str:
        """Recover the chain head for *day* (used when no handle is open)."""
        path = self.path_for(day)
        if not path.exists():
            return GENESIS_HASH
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return self._tail_hash_from(fh)
        except OSError:
            return GENESIS_HASH

    # ── read ───────────────────────────────────────────────────────────

    def recent(self, limit: int = 100) -> list[dict]:
        """Return the newest records from the in-memory ring (newest first)."""
        items = list(self._ring)[-max(1, limit):]
        return list(reversed(items))

    def read_day(self, day: str) -> list[dict]:
        path = self.path_for(day)
        if not path.exists():
            return []
        records: list[dict] = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError as exc:
            logger.warning("Audit read failed for %s: %s", day, exc)
        return records

    def query(
        self,
        *,
        run_id: str = "",
        session_id: str = "",
        event: str = "",
        severity: str = "",
        decision: str = "",
        day: str = "",
        limit: int = 200,
    ) -> list[dict]:
        """Filter the audit trail (newest first)."""
        records = self.read_day(day) if day else list(self._ring)
        if not records and not day:
            records = list(self._ring)
        result = []
        for rec in reversed(records):
            if run_id and rec.get("run_id") != run_id:
                continue
            if session_id and rec.get("session_id") != session_id:
                continue
            if event and rec.get("event") != event:
                continue
            if severity and rec.get("severity") != severity:
                continue
            if decision and rec.get("decision") != decision:
                continue
            result.append(rec)
            if len(result) >= max(1, limit):
                break
        return result

    def available_days(self, limit: int = 30) -> list[str]:
        if not self.directory.exists():
            return []
        days = sorted(
            (p.stem.removeprefix("audit-") for p in self.directory.glob("audit-*.jsonl")),
            reverse=True,
        )
        return days[:limit]

    # ── integrity ──────────────────────────────────────────────────────

    def verify_chain(self, day: str = "") -> dict:
        """Recompute the hash chain and report the first broken link."""
        days = [day] if day else (self.available_days() or [self.day_of()])
        results = []
        for d in days:
            records = self.read_day(d)
            prev = GENESIS_HASH
            broken_at = None
            for idx, rec in enumerate(records):
                # Same projection used when the record was written:
                # everything except the chain fields and the record id.
                body = {
                    k: v for k, v in rec.items()
                    if k not in ("hash", "prev_hash", "id")
                }
                expected = _digest(prev, body)
                if rec.get("prev_hash") != prev or rec.get("hash") != expected:
                    broken_at = {"index": idx, "id": rec.get("id"), "ts": rec.get("ts")}
                    break
                prev = rec.get("hash", prev)
            results.append({
                "day": d,
                "records": len(records),
                "intact": broken_at is None,
                "broken_at": broken_at,
                "head": prev,
            })
        return {
            "ok": all(r["intact"] for r in results),
            "days": results,
        }

    # ── stats ──────────────────────────────────────────────────────────

    def stats(self) -> dict:
        records = list(self._ring)
        by_severity: dict[str, int] = {}
        by_event: dict[str, int] = {}
        for rec in records:
            sev = rec.get("severity", "info")
            by_severity[sev] = by_severity.get(sev, 0) + 1
            ev = rec.get("event", "unknown")
            by_event[ev] = by_event.get(ev, 0) + 1
        return {
            "buffered": len(records),
            "directory": str(self.directory),
            "writable": self.directory.exists() or self._ensure_dir(),
            "by_severity": by_severity,
            "by_event": by_event,
            "days": self.available_days(),
        }


_audit_log: Optional[AuditLog] = None


def get_audit_log() -> AuditLog:
    """Return the process-wide audit log singleton."""
    global _audit_log
    if _audit_log is None:
        _audit_log = AuditLog()
    return _audit_log
