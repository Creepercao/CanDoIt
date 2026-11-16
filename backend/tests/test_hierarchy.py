from copy import deepcopy

import pytest

from backend.chunking import prepare_document
from backend.hierarchy import validate_snapshot, HierarchyStore


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


def test_store_reopens_immutable_snapshots_and_distinguishes_chunk_configuration(tmp_path):
    path = tmp_path / "hierarchy.sqlite"
    store = HierarchyStore(path)
    first = example()
    snapshot_id = store.save(first)
    assert store.save(first) == snapshot_id
    assert HierarchyStore(path).get(snapshot_id) == first
    alternative = prepare_document(first["document"]["text"], "guide", 20, 0)
    assert alternative["document"]["id"] == first["document"]["id"]
    other_id = store.save(alternative)
    assert other_id != snapshot_id
    assert store.get(other_id) == alternative
    assert store.get(snapshot_id) == first
    assert store.get("missing") is None


def test_invalid_snapshot_does_not_create_store(tmp_path):
    path = tmp_path / "store.sqlite"
    with pytest.raises(ValueError):
        HierarchyStore(path).save({})
    assert HierarchyStore(path).get("missing") is None
    assert not path.exists()


def test_activation_is_explicit_reversible_and_isolated_per_source(tmp_path):
    store = HierarchyStore(tmp_path / "store.sqlite")
    first = store.save(example("old", "guide"))
    second = store.save(example("new", "guide"))
    other = store.save(example("other", "policy"))
    assert not any(item["active"] for item in store.list_versions())
    assert store.activate(first) == "guide"
    store.activate(other)
    store.activate(second)
    assert {item["id"] for item in store.list_versions() if item["active"]} == {second, other}
    with pytest.raises(ValueError):
        store.activate("missing")
    store.activate(first)
    assert [item["id"] for item in store.list_versions("guide") if item["active"]] == [first]
    assert store.get(second)["document"]["text"] == "new"


def test_context_expands_unique_parents_in_rank_order_and_enforces_budget(tmp_path):
    store = HierarchyStore(tmp_path / "store.sqlite")
    snapshot = example()
    store.activate(store.save(snapshot))
    first_section = snapshot["sections"][0]
    first_chunks = [item["id"] for item in snapshot["chunks"] if item["parent_id"] == first_section["id"]]
    last = snapshot["chunks"][-1]["id"]
    result = store.context([last, first_chunks[0], first_chunks[1], last], max_chars=1000)
    assert len(result["contexts"]) == 2
    assert result["contexts"][0]["text"].endswith("尾部")
    assert result["contexts"][1]["text"] == first_section["text"]
    assert result["contexts"][1]["matched_chunk_ids"] == first_chunks[:2]
    limited = store.context([first_chunks[0], last], max_chars=10)
    assert limited["chars"] == 10
    assert limited["contexts"][0]["truncated"] is True
    assert limited["contexts"][0]["end"] == first_section["start"] + 10
    assert limited["omitted_sections"] == 1


def test_context_never_serves_old_versions_or_silently_drops_unknown_hits(tmp_path):
    store = HierarchyStore(tmp_path / "store.sqlite")
    old, new = example("old"), example("new")
    old_id = store.save(old)
    store.activate(old_id)
    store.activate(store.save(new))
    with pytest.raises(ValueError, match="inactive"):
        store.context([old["chunks"][0]["id"]])
    with pytest.raises(ValueError):
        store.context([new["chunks"][0]["id"], "unknown"])
    assert store.context([])["contexts"] == []
