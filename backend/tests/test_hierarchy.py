from copy import deepcopy

import pytest

from backend.chunking import prepare_document
from backend.hierarchy import validate_snapshot


def example(text="# A\n" + "正文" * 40 + "\n# B\n尾部", source="guide"):
    return prepare_document(text, source, 30, 5)


def test_validated_snapshot_discards_file_metadata_but_keeps_canonical_content():
    snapshot = example()
    assert validate_snapshot({**snapshot, "input": {"path": "local-file"}}) == snapshot


@pytest.mark.parametrize("field", ["parent", "offset", "text", "id", "stats", "version", "missing"])
def test_tampered_snapshots_cannot_be_imported(field):
    snapshot = deepcopy(example())
    if field == "parent":
        snapshot["chunks"][0]["parent_id"] = "orphan"
    elif field == "offset":
        snapshot["chunks"][0]["start"] += 1
    elif field == "text":
        snapshot["chunks"][0]["text"] = "tampered"
    elif field == "id":
        snapshot["document"]["id"] = "fake"
    elif field == "stats":
        snapshot["stats"]["chunks"] += 1
    elif field == "version":
        snapshot["version"] = True
    else:
        del snapshot["sections"]
    with pytest.raises(ValueError):
        validate_snapshot(snapshot)
