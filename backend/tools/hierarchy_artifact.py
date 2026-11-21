"""Self-contained offline hierarchy rankings with verifiable experiment metadata."""

from copy import deepcopy
import hashlib
import json
import re

from backend.tools.retrieval_eval import evaluate
from backend.tools.retrieval_export import validate_export


def _digest(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    except (TypeError, ValueError) as exc:
        raise ValueError("artifact must contain finite JSON values") from exc


def make_artifact(rows, metadata):
    artifact = {"kind": "hierarchy-rankings-v1", "rows": deepcopy(rows), "metadata": deepcopy(metadata)}
    artifact["content_sha256"] = _digest(artifact)
    unpack_artifact(artifact)
    return artifact


def unpack_artifact(artifact):
    """Check consistency without accessing the store or executing retrieval."""
    if (not isinstance(artifact, dict)
            or set(artifact) != {"kind", "rows", "metadata", "content_sha256"}
            or artifact["kind"] != "hierarchy-rankings-v1"):
        raise ValueError("unsupported rankings artifact")
    payload = {key: artifact[key] for key in ("kind", "rows", "metadata")}
    if artifact["content_sha256"] != _digest(payload):
        raise ValueError("artifact content fingerprint does not match")
    metadata = artifact["metadata"]
    if not isinstance(metadata, dict):
        raise ValueError("artifact metadata must be an object")
    ids = metadata.get("snapshot_ids")
    if (not isinstance(ids, list) or not ids
            or any(not isinstance(sid, str) or not re.fullmatch(r"snapshot-[0-9a-f]{64}", sid) for sid in ids)
            or ids != sorted(set(ids))):
        raise ValueError("artifact snapshot_ids must be sorted unique snapshot IDs")
    if metadata.get("corpus_sha256") != _digest(ids):
        raise ValueError("artifact corpus fingerprint does not match snapshots")
    configuration = metadata.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError("artifact configuration must be an object")
    depth = configuration.get("top_k")
    validate_export(artifact["rows"], depth=depth, threshold=0)
    evaluate(artifact["rows"])
    if any(len(row["ranked_ids"]) > depth for row in artifact["rows"]):
        raise ValueError("artifact ranking exceeds configured depth")
    identity = {"snapshot_ids": ids, "corpus_sha256": metadata["corpus_sha256"], "configuration": configuration}
    if metadata.get("retrieval_sha256") != _digest(identity):
        raise ValueError("artifact retrieval fingerprint does not match configuration")
    if "queries" in metadata and (type(metadata["queries"]) is not int or metadata["queries"] != len(artifact["rows"])):
        raise ValueError("artifact query count does not match rows")
    return deepcopy(artifact["rows"]), deepcopy(metadata)


def decode_rankings(value, k=5):
    """Accept legacy arrays or validate a bundled experiment before scoring."""
    if isinstance(value, list):
        return value, None
    rows, metadata = unpack_artifact(value)
    if type(k) is not int or k < 1 or k > metadata["configuration"]["top_k"]:
        raise ValueError("evaluation k must be positive and not exceed artifact retrieval depth")
    return rows, metadata


def require_comparable_experiments(baseline, candidate):
    """Avoid silently comparing a known corpus with an unknown or different one."""
    if (baseline is None) != (candidate is None):
        raise ValueError("comparison cannot mix a rankings artifact with a legacy array of unknown corpus")
    if baseline is not None and baseline["corpus_sha256"] != candidate["corpus_sha256"]:
        raise ValueError("artifact comparison requires the same corpus snapshots")
