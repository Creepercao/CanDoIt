import pytest

from backend.chunking import prepare_document
from backend.hierarchy import HierarchyStore


def test_inventory_exposes_real_ids_positions_and_explicitly_truncated_previews(tmp_path):
    store = HierarchyStore(tmp_path / "store.sqlite")
    snapshot = prepare_document("# 商品\n" + "原文" * 80, "manual", 30, 5)
    store.activate(store.save(snapshot))
    view = store.prepare()
    report = view.inventory(preview_chars=4)
    assert report == view.inventory(preview_chars=4)
    assert report["corpus_sha256"] == view.retrieval_identity()["corpus_sha256"]
    assert [chunk["id"] for chunk in report["chunks"]] == [chunk["id"] for chunk in snapshot["chunks"]]
    assert len(report["chunks"][0]["content_preview"]) == 4
    assert report["chunks"][0]["preview_truncated"] is True
    assert "text" not in report["chunks"][0]
    full = view.inventory(include_text=True)
    for chunk in full["chunks"]:
        assert snapshot["document"]["text"][chunk["start"]:chunk["end"]] == chunk["text"]
        assert chunk["parent_id"] in {section["id"] for section in report["sections"]}
    full["chunks"][0]["path"].append("changed")
    assert view.inventory()["chunks"][0]["path"] == ["商品"]


@pytest.mark.parametrize("preview,full", [(0, False), (True, False), (1.5, False), (10, "true")])
def test_inventory_options_rejected(tmp_path, preview, full):
    with pytest.raises(ValueError):
        HierarchyStore(tmp_path / "missing.sqlite").prepare().inventory(preview, include_text=full)
