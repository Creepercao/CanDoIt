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


@pytest.mark.parametrize("tamper", ["payload", "source", "active"])
def test_integrity_check_rejects_valid_content_under_wrong_identity(tmp_path, tamper):
    import json
    import sqlite3

    path = tmp_path / "store.sqlite"
    store = HierarchyStore(path)
    first = store.save(example("first", "guide"))
    other = store.save(example("other", "policy"))
    store.activate(first)
    assert store.verify() == {"valid": True, "snapshots": 2, "active_sources": 1}
    with sqlite3.connect(path) as connection:
        if tamper == "payload":
            connection.execute("UPDATE snapshots SET payload=? WHERE id=?", (json.dumps(example("changed", "guide")), first))
        elif tamper == "source":
            connection.execute("UPDATE snapshots SET source='wrong' WHERE id=?", (first,))
        else:
            connection.execute("UPDATE active_sources SET snapshot_id=? WHERE source='guide'", (other,))
    with pytest.raises(ValueError):
        store.verify()
    if tamper != "active":
        with pytest.raises(ValueError):
            store.get(first)


def test_snapshot_offsets_reject_boolean_numeric_aliases():
    snapshot = example()
    snapshot["chunks"][0]["start"] = False
    with pytest.raises(ValueError):
        validate_snapshot(snapshot)


def test_read_connections_cannot_write_or_change_the_database(tmp_path):
    import sqlite3
    path = tmp_path / "store # 中文.sqlite"
    store = HierarchyStore(path)
    sid = store.save(example())
    store.activate(sid)
    before = path.read_bytes()
    assert store.get(sid)
    assert store.list_versions()
    assert store.verify()["valid"]
    with store._connect() as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM snapshots")
    assert path.read_bytes() == before


def test_read_does_not_initialize_an_unrelated_database(tmp_path):
    import sqlite3
    path = tmp_path / "empty.sqlite"
    sqlite3.connect(path).close()
    with pytest.raises(sqlite3.OperationalError):
        HierarchyStore(path).list_versions()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


def test_active_read_checks_references_instead_of_hiding_invalid_pointers(tmp_path):
    import sqlite3
    path = tmp_path / "store.sqlite"
    store = HierarchyStore(path)
    sid = store.save(example("needle", "manual"))
    other = store.save(example("other", "policy"))
    store.activate(sid)
    assert [row["id"] for row in store.active_snapshots()] == [sid]
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE active_sources SET snapshot_id=?", (other,))
    with pytest.raises(ValueError, match="inconsistent"):
        store.search("needle")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE active_sources SET snapshot_id='missing'")
    with pytest.raises(ValueError, match="inconsistent"):
        store.active_snapshots()


def test_prepared_view_keeps_search_and_context_on_captured_version(tmp_path):
    store = HierarchyStore(tmp_path / "store.sqlite")
    first = store.save(example("oldneedle", "manual"))
    store.activate(first)
    view = store.prepare()
    store.activate(store.save(example("newneedle", "manual")))
    hits = view.search("oldneedle")
    assert hits and hits[0]["snapshot_id"] == first
    assert view.context([hits[0]["id"]])["contexts"][0]["text"] == "oldneedle"
    assert store.search("oldneedle") == []
    records = view.active_snapshots()
    records[0]["snapshot"]["document"]["text"] = "changed"
    assert view.active_snapshots()[0]["snapshot"]["document"]["text"] == "oldneedle"


def test_hit_window_keeps_tail_hits_and_reports_partial_visibility(tmp_path):
    store = HierarchyStore(tmp_path / "store.sqlite")
    text = "# A\n" + "x" * 200 + " tailneedle"
    snapshot = prepare_document(text, "manual", 20, 0)
    store.activate(store.save(snapshot))
    hit = snapshot["chunks"][-1]
    prefix = store.context([hit["id"]], 30)["contexts"][0]
    assert prefix["visible_chunk_ids"] == []
    window = store.context([hit["id"]], 30, mode="hit")["contexts"][0]
    assert "tailneedle" in window["text"]
    assert window["text"] == text[window["start"]:window["end"]]
    assert window["complete_chunk_ids"] == [hit["id"]]
    assert window["section_start"] == 0 and window["truncated"]
    tiny = store.context([hit["id"]], 3, mode="hit")["contexts"][0]
    assert tiny["visible_chunk_ids"] == [hit["id"]]
    assert tiny["complete_chunk_ids"] == []
    assert len(tiny["text"]) == 3
    with pytest.raises(ValueError, match="mode"):
        store.context([hit["id"]], mode="unknown")


def test_deactivate_keeps_history_and_existing_views_until_reactivation(tmp_path):
    path = tmp_path / "store.sqlite"
    store = HierarchyStore(path)
    sid = store.save(example("needle", "manual"))
    other = store.save(example("needle", "policy"))
    store.activate(sid)
    store.activate(other)
    view = store.prepare()
    assert store.deactivate("manual") is True
    assert store.deactivate("manual") is False
    assert store.get(sid)["document"]["text"] == "needle"
    assert {hit["source"] for hit in store.search("needle")} == {"policy"}
    assert {hit["source"] for hit in view.search("needle")} == {"manual", "policy"}
    store.activate(sid)
    assert len(store.search("needle")) == 2
    assert HierarchyStore(tmp_path / "missing.sqlite").deactivate("manual") is False
