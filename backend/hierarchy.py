"""Validated, offline hierarchical document snapshots."""

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
